"""CLI — drafters: extract. Everyone else: index/find/history/at/changed/report."""
from __future__ import annotations

import json
from pathlib import Path

import click

from .extract import dxf_version, extract_state, version_supported
from .identity import DEFAULT_TOLERANCE_MM, resolve
from .semantics import load_config_dir


def _load_jsonl(path: Path) -> list[dict]:
    rows = []
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    return rows


def _load_idmap(path: Path, drawing: str) -> dict:
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if "elements" in data:
                return data
    return {"drawing": drawing, "elements": {}}


def _taxonomy_dir_opt(f):
    return click.option("--taxonomy-dir", default=None,
                        help="Owned taxonomy/ (default: ./taxonomy when present, else bundled seed)")(f)


def _strip_visuals(payload: dict) -> dict:
    """Volatile render fields (thumbnail/phash/near_duplicates/crop) are
    existence-checked, never byte-compared — strip them before the
    determinism comparison (PNG bytes vary by matplotlib version)."""
    import copy
    payload = copy.deepcopy(payload or {})
    for d in payload.get("details", []) or []:
        for key in ("thumbnail", "phash", "near_duplicates"):
            d.pop(key, None)
    for cand in payload.get("quarantine_candidates", []) or []:
        cand.pop("crop", None)
    for rv in payload.get("dim_review", []) or []:
        rv.pop("crop", None)
    return payload


@click.group()
def cli():
    pass


@cli.command()
@click.argument("dxf_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--state-dir", default="state")
@click.option("--config-dir", default="config")
@click.option("--drawings-dir", default="drawings",
              help="Drawings root — used to derive project/drawing ids (drawings/<project>/X.dxf)")
@click.option("--details-dir", default="details",
              help="Detail records root — details/<project>/X.json, committed")
@click.option("--taxonomy-dir", default=None,
              help="Owned taxonomy/ (default: ./taxonomy when present, else bundled seed)")
@click.option("--thumbs-dir", default="thumbs",
              help="Detail thumbnails root — thumbs/<project>/X.<detail>.png, committed")
@click.option("--crops-dir", default="crops",
              help="Quarantine evidence crops — crops/<project>/X.<element>.png, committed")
@click.option("--tolerance", default=DEFAULT_TOLERANCE_MM, type=float)
@click.option("--check", is_flag=True,
              help="CI mode: fail if committed state/*.jsonl or details/*.json is out of sync (no writes)")
def extract(dxf_path, state_dir, config_dir, drawings_dir, details_dir,
            taxonomy_dir, thumbs_dir, crops_dir, tolerance, check):
    """Extract DXF canonical state, resolve identity, write state + details files."""
    from .segment import build_sheet_details, write_details_file
    from .attribute import load_geometry_config
    from .render import finalize_visuals
    dxf_path = Path(dxf_path)
    state_dir = Path(state_dir)
    config_dir = Path(config_dir)
    # drawing id: path relative to drawings/ without suffix, posix.
    # drawings/D-101.dxf -> 'D-101'; drawings/StageC/D-101.dxf -> 'StageC/D-101'.
    try:
        rel = dxf_path.resolve().relative_to(Path(drawings_dir).resolve())
        drawing = rel.with_suffix("").as_posix()
    except ValueError:
        drawing = dxf_path.stem
    state_jsonl = state_dir / (drawing + ".jsonl")
    state_idmap = state_dir / (drawing + ".idmap.json")

    # DWG companion: never parsed (brief), but warn when it is newer than the
    # DXF — the export step was likely forgotten. PDF companions are viewing
    # artifacts and need no warning.
    for dwg_suffix in (".dwg", ".DWG"):
        sib = dxf_path.with_suffix(dwg_suffix)
        if sib.exists():
            try:
                if sib.stat().st_mtime > dxf_path.stat().st_mtime:
                    click.echo(f"WARNING: {sib.name} is newer than {dxf_path.name} — "
                               f"re-export DWG->DXF (ASCII R2018+) before extract", err=True)
            except OSError:
                pass
            break

    materials_cfg, cfg_source = load_config_dir(config_dir)
    if not materials_cfg:
        click.echo("WARNING: no materials config found — annotations will not parse", err=True)
    ver = dxf_version(dxf_path)
    if ver and not version_supported(ver):
        click.echo(f"WARNING: {dxf_path.name} is DXF {ver} (< R2018 AC1032) — "
                   f"re-export as ASCII R2018+ for reliable history", err=True)
    current_raw = extract_state(dxf_path, materials_cfg)
    if not current_raw:
        click.echo(f"WARNING: {dxf_path.name} yielded 0 extractable entities "
                   f"(supported: LINE, LWPOLYLINE incl. legacy POLYLINE, ARC, CIRCLE, "
                   f"HATCH, TEXT, MTEXT, DIMENSION, MULTILEADER)", err=True)
    prev = _load_jsonl(state_jsonl) if state_jsonl.exists() else []
    idmap = _load_idmap(state_idmap, drawing) if state_idmap.exists() else {"drawing": drawing, "elements": {}}
    resolved, new_idmap, event, fuzzy = resolve(current_raw, prev, idmap, tolerance=tolerance)

    EPHEMERAL = ("status", "match_tier", "match_confidence")
    storable = [{k: v for k, v in r.items() if k not in EPHEMERAL and not k.startswith("_")}
                for r in resolved]
    storable.sort(key=lambda r: (r["layer"], r["type"], r["geom"]["x"], r["geom"]["y"], r.get("text_raw") or ""))
    new_text = "".join(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n" for r in storable)

    # 7.1/7.5 detail regions: same single command, committed alongside state.
    # The open DXF doc feeds attribution Chains A/C; without it (unreadable
    # file) leader-text evidence still builds the record.
    doc = None
    try:
        import ezdxf
        doc = ezdxf.readfile(str(dxf_path))
    except Exception:
        doc = None
    gpath = config_dir / "geometry.yaml"
    try:
        geometry_cfg = load_geometry_config(str(gpath) if gpath.exists() else None)
    except Exception:
        geometry_cfg = {}
    details_payload = build_sheet_details(drawing, resolved, materials_cfg, geometry_cfg, doc,
                                            taxonomy_dir=taxonomy_dir)
    # 7.6/8.7.2 visuals: thumbnails + evidence crops render from the same DXF
    # and patch back into the payload (thumbnail/phash/near_duplicates/crop).
    # Rendering never blocks: failures degrade to null fields with a warning.
    records_by_eid = {r.get("element_id"): r for r in resolved if r.get("element_id")}
    visual_files: list = []
    try:
        vis = finalize_visuals(dxf_path, details_payload, records_by_eid,
                               Path(thumbs_dir), Path(crops_dir), drawing,
                               dry_run=check)
        if not check:
            details_payload = vis["payload"]
        visual_files = vis["files"]
    except Exception as ex:
        click.echo(f"WARNING: visuals skipped ({ex})", err=True)
    details_path = Path(details_dir) / (drawing + ".json")
    import io as _io
    _buf = _io.StringIO()
    json.dump(details_payload, _buf, sort_keys=True, indent=2, ensure_ascii=False)
    new_details = _buf.getvalue() + "\n"

    if check:
        old = state_jsonl.read_text(encoding="utf-8") if state_jsonl.exists() else ""
        if old != new_text:
            click.echo(f"OUT OF SYNC: {drawing}.dxf changed without re-running extract", err=True)
            raise SystemExit(1)
        old_d = details_path.read_text(encoding="utf-8") if details_path.exists() else ""
        try:
            old_payload = json.loads(old_d) if old_d else {}
        except ValueError:
            old_payload = {}
        if json.dumps(_strip_visuals(details_payload), sort_keys=True) != \
                json.dumps(_strip_visuals(old_payload), sort_keys=True):
            click.echo(f"OUT OF SYNC: details/{drawing}.json changed without re-running extract", err=True)
            raise SystemExit(1)
        # visuals: existence only — PNG bytes vary by matplotlib version, so
        # byte comparison would fail across machines (never compare renders).
        missing = [str(p) for p in visual_files if not p.exists()]
        if missing:
            click.echo(f"OUT OF SYNC: {len(missing)} visual(s) missing "
                       f"(e.g. {missing[0]}) — re-run extract (or render)", err=True)
            raise SystemExit(1)
        click.echo(f"{drawing}: in sync ({len(storable)} elements, "
                   f"{len(details_payload['details'])} details)")
        return

    state_jsonl.parent.mkdir(parents=True, exist_ok=True)
    with open(state_jsonl, "w", encoding="utf-8", newline="\n") as f:
        f.write(new_text)
    with open(state_idmap, "w", encoding="utf-8", newline="\n") as f:
        json.dump(new_idmap, f, sort_keys=True, indent=2, ensure_ascii=False)
        f.write("\n")
    write_details_file(Path(details_dir), drawing, details_payload)

    from collections import Counter
    counts = Counter(r.get("status", "?") for r in resolved)
    click.echo(f"{drawing}: {len(resolved)} elements {dict(counts)}")
    seg = details_payload.get("segmentation")
    tags = ", ".join(d["detail_tag"] for d in details_payload["details"])
    click.echo(f"details: {len(details_payload['details'])} [{tags}] segmentation={seg}")
    if seg == "uncertain":
        click.echo(f"WARNING: segmentation uncertain ({details_payload.get('uncertain_reason')}) "
                   f"— whole sheet indexed as one record, flag for review", err=True)
    if event is not None:
        click.echo(f"translation: dx={event['dx']} dy={event['dy']} "
                   f"({event['matched']}/{event['total']} share offset) — normalised")
    for fr in fuzzy:
        click.echo(f"REVIEW fuzzy: {fr['element_id']} h={fr['dxf_handle']} "
                   f"conf={fr['confidence']} prev={fr['prev_text']!r} curr={fr['curr_text']!r}")


@cli.command("index")
@click.option("--repo", default=".", help="Drawing repository root (with .git + state/)")
@click.option("--db", default="index.sqlite", help="Output SQLite path")
@click.option("--state-dir", default="state")
@click.option("--drawings-dir", default="drawings")
@click.option("--details-dir", default="details")
@_taxonomy_dir_opt
def index_cmd(repo, db, state_dir, drawings_dir, details_dir, taxonomy_dir):
    """Walk git log of the drawing repo and (incrementally) build index.sqlite."""
    from .index import build_index
    res = build_index(Path(repo), Path(db), state_dir,
                      drawings_dir=drawings_dir, details_dir=details_dir,
                      taxonomy_dir=taxonomy_dir)
    click.echo(f"indexed {res['commits_processed']} new commits, +{res['rows_inserted']} rows, total {res['total_rows']} -> {db}")


@cli.command("segment")
@click.argument("dxf_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--state-dir", default="state", help="Committed state root (element ids come from here)")
@click.option("--details-dir", default="details", help="Detail records root (committed)")
@click.option("--config-dir", default="config")
@click.option("--drawings-dir", default="drawings")
@_taxonomy_dir_opt
@click.option("--thumbs-dir", default="thumbs")
@click.option("--crops-dir", default="crops")
@click.option("--check", is_flag=True, help="CI mode: fail if details/*.json is out of sync (no writes)")
def segment_cmd(dxf_path, state_dir, details_dir, config_dir, drawings_dir,
                taxonomy_dir, thumbs_dir, crops_dir, check):
    """(Re)build details/<drawing>.json for one sheet from committed state + DXF.

    Backfill path: run extract once per sheet, or segment when only the
    segmentation logic changed and element state is untouched.
    """
    from .segment import build_sheet_details, write_details_file
    from .attribute import load_geometry_config
    from .render import finalize_visuals
    dxf_path = Path(dxf_path)
    try:
        rel = dxf_path.resolve().relative_to(Path(drawings_dir).resolve())
        drawing = rel.with_suffix("").as_posix()
    except ValueError:
        drawing = dxf_path.stem
    state_jsonl = Path(state_dir) / (drawing + ".jsonl")
    if not state_jsonl.exists():
        click.echo(f"no state for {drawing} — run gitail extract first", err=True)
        raise SystemExit(1)
    rows = _load_jsonl(state_jsonl)
    materials_cfg, _ = load_config_dir(config_dir)
    gpath = Path(config_dir) / "geometry.yaml"
    try:
        geometry_cfg = load_geometry_config(str(gpath) if gpath.exists() else None)
    except Exception:
        geometry_cfg = {}
    try:
        import ezdxf
        doc = ezdxf.readfile(str(dxf_path))
    except Exception:
        doc = None
    payload = build_sheet_details(drawing, rows, materials_cfg, geometry_cfg, doc,
                                    taxonomy_dir=taxonomy_dir)
    records_by_eid = {r.get("element_id"): r for r in rows if r.get("element_id")}
    try:
        vis = finalize_visuals(dxf_path, payload, records_by_eid,
                               Path(thumbs_dir), Path(crops_dir), drawing,
                               dry_run=check)
        if not check:
            payload = vis["payload"]
    except Exception as ex:
        click.echo(f"WARNING: visuals skipped ({ex})", err=True)
    import io as _io
    _buf = _io.StringIO()
    json.dump(_strip_visuals(payload) if check else payload, _buf,
              sort_keys=True, indent=2, ensure_ascii=False)
    new_text = _buf.getvalue() + "\n"
    out = Path(details_dir) / (drawing + ".json")
    if check:
        old = out.read_text(encoding="utf-8") if out.exists() else ""
        try:
            old_payload = json.loads(old) if old else {}
        except ValueError:
            old_payload = {}
        if json.dumps(_strip_visuals(old_payload), sort_keys=True) != \
                json.dumps(_strip_visuals(payload), sort_keys=True):
            click.echo(f"OUT OF SYNC: details/{drawing}.json — re-run gitail segment", err=True)
            raise SystemExit(1)
        click.echo(f"{drawing}: details in sync ({len(payload['details'])} regions)")
        return
    write_details_file(Path(details_dir), drawing, payload)
    tags = ", ".join(d["detail_tag"] for d in payload["details"])
    click.echo(f"{drawing}: {len(payload['details'])} details [{tags}] "
               f"segmentation={payload.get('segmentation')}")
    if payload.get("segmentation") == "uncertain":
        click.echo(f"WARNING: {payload.get('uncertain_reason')} — flag for review", err=True)


@cli.group("details", invoke_without_command=True)
@click.pass_context
@click.option("--drawing", "drawing", default=None, help="One sheet (e.g. StageC/D-102)")
@click.option("--json", "as_json", is_flag=True)
@click.option("--db", default="index.sqlite")
def details_cmd(ctx, drawing, as_json, db):
    """List indexed detail regions (7.1/7.5): one row per detail, current commit."""
    if ctx.invoked_subcommand is not None:
        return
    import sqlite3
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    try:
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "detail" not in names:
            click.echo("no detail table — re-run gitail index")
            return
        q = ("SELECT detail_id, drawing, detail_tag, title, scale, segmentation,"
             " entity_count, depends_on, wet_area, commit_sha FROM detail WHERE 1=1")
        args: list = []
        if drawing:
            q += " AND drawing=?"
            args.append(drawing)
        # current snapshot per drawing: latest commit carrying that drawing
        q += (" AND commit_sha IN (SELECT commit_sha FROM detail AS _l WHERE _l.drawing=detail.drawing"
              " ORDER BY _l.rowid DESC LIMIT 1) ORDER BY drawing, detail_tag")
        rows = [dict(r) for r in con.execute(q, args)]
    finally:
        con.close()
    if as_json:
        click.echo(json.dumps(rows, indent=2, ensure_ascii=False))
    elif not rows:
        click.echo("no details indexed")
    else:
        for r in rows:
            wet = " wet-area" if r.get("wet_area") else ""
            click.echo(f"{r['detail_id']} {r['drawing']} [{r['detail_tag']}] {r['title']!r} "
                       f"scale={r['scale']} seg={r['segmentation']} n={r['entity_count']}"
                       f" depends={r['depends_on']}{wet}")


@details_cmd.command("confirm")
@click.argument("detail_id")
@click.option("--junction-type", "junction_type", default=None)
@click.option("--assembly", default=None)
@click.option("--context", default=None)
@click.option("--projection", default=None)
@click.option("--repo", default=".", help="Drawings repo root (edits committed detail files)")
@_taxonomy_dir_opt
def details_confirm_cmd(detail_id, junction_type, assembly, context,
                        projection, repo, taxonomy_dir):
    """Uploader confirm/correct for 7.7 pre-filled facets (7.7): re-derives
    facet nodes + classification prefixes, stamps classified_by human."""
    from .classify import refacet
    from .taxonomy import TaxonomyError, load_taxonomy_cached
    try:
        tax = load_taxonomy_cached(taxonomy_dir)
    except TaxonomyError as ex:
        click.echo(f"FAILED: {ex}", err=True)
        raise SystemExit(1)
    given = {"junction_type": junction_type, "assembly": assembly,
             "context": context, "projection": projection,
             "classified_by": "human"}
    given = {k: v for k, v in given.items() if v is not None}
    if not given:
        click.echo("nothing to confirm — pass at least one facet flag", err=True)
        raise SystemExit(1)
    base = Path(repo, "details")
    found = None
    for path in sorted(base.rglob("*.json")) if base.is_dir() else []:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for d in data.get("details", []) or []:
            if d.get("detail_id") == detail_id:
                found = (path, data, d)
                break
        if found:
            break
    if found is None:
        click.echo(f"unknown detail: {detail_id}", err=True)
        raise SystemExit(1)
    path, data, record = found
    try:
        updated = refacet(tax, record, given)
    except ValueError as ex:
        click.echo(f"FAILED: {ex}", err=True)
        raise SystemExit(1)
    data["details"] = [updated if d.get("detail_id") == detail_id else d
                       for d in data.get("details", [])]
    path.write_text(json.dumps(data, sort_keys=True, indent=2,
                               ensure_ascii=False) + "\n", encoding="utf-8")
    click.echo(f"{detail_id}: junction={updated.get('junction_type')} "
               f"assembly={updated.get('assembly')} context={updated.get('context')} "
               f"projection={updated.get('projection')} "
               f"nodes={','.join(updated.get('facet_nodes', []))} — commit the file")


def _short(v):
    v = str(v or "")
    return v[:7] if len(v) == 40 and all(ch in "0123456789abcdef" for ch in v.lower()) else v


def _resolve_ctx(taxonomy_dir, config_dir):
    """Shared context for quarantine/tree commands: cached taxonomy + settings."""
    from .taxonomy import TaxonomyError, load_settings, load_taxonomy_cached
    try:
        tax = load_taxonomy_cached(taxonomy_dir)
    except TaxonomyError as ex:
        click.echo(f"FAILED: {ex}", err=True)
        raise SystemExit(1)
    return tax, load_settings(config_dir)


@cli.group("unresolved", invoke_without_command=True)
@click.pass_context
@click.option("--drawing", default=None)
@click.option("--json", "as_json", is_flag=True)
@click.option("--db", default="index.sqlite")
@_taxonomy_dir_opt
@click.option("--config-dir", default="config")
def unresolved_grp(ctx, drawing, as_json, db, taxonomy_dir, config_dir):
    """Review queue: quarantined clusters pending classification (8.7)."""
    if ctx.invoked_subcommand is not None:
        return
    from .resolve import clusters
    tax, _settings = _resolve_ctx(taxonomy_dir, config_dir)
    items = clusters(Path(db), tax, drawing)
    if as_json:
        click.echo(json.dumps(items, indent=2, ensure_ascii=False))
    elif not items:
        click.echo("queue clear — nothing unclassified")
    else:
        click.echo(f"Unclassified ({len(items)} cluster{'s' if len(items) != 1 else ''})")
        for c in items:
            click.echo(f"  {c['cluster_id']} [{c['level_guess']}] {c['label']!r} "
                       f"x{c['occurrence_count']} — {len(c['details'])} detail(s)")


@unresolved_grp.command("show")
@click.argument("key")
@click.option("--db", default="index.sqlite")
@_taxonomy_dir_opt
@click.option("--config-dir", default="config")
def unresolved_show_cmd(key, db, taxonomy_dir, config_dir):
    """Evidence for one cluster (or occurrence): crop, occurrences, suggestions."""
    from .resolve import _find_cluster, load_occurrences, still_pending
    tax, _settings = _resolve_ctx(taxonomy_dir, config_dir)
    cluster = _find_cluster(Path(db), tax, key)
    if cluster is None:
        # maybe an occurrence uid — show it raw
        occs = [o for o in load_occurrences(Path(db), latest_only=False)
                if o["uid"] == key]
        if not occs:
            click.echo(f"unknown cluster or occurrence: {key}", err=True)
            raise SystemExit(1)
        click.echo(json.dumps(occs[0], indent=2, ensure_ascii=False))
        return
    click.echo(f"{cluster['cluster_id']} [{cluster['level_guess']}] {cluster['label']!r}")
    click.echo(f"  occurrences: {cluster['occurrence_count']}  "
               f"variants: {len(cluster['variants'])}")
    for v in cluster["variants"]:
        click.echo(f"    ~ {v!r}")
    for d in cluster["details"]:
        click.echo(f"    @ {d['drawing']} / {d['detail_id']} — {d.get('title')}")
    if cluster.get("crop"):
        click.echo(f"  crop: {cluster['crop']}")
    ev0 = (cluster.get("sibling_context") or [])
    if ev0:
        click.echo(f"  sibling context: {', '.join(ev0)}")
    click.echo("  suggestions:")
    for s in cluster["suggestions"]:
        click.echo(f"    {int(s['score'] * 100):3d}%  {s['node']} ({s['label']}) — {s['why']}")
    click.echo(f"\n  gitail resolve {cluster['cluster_id']} --assign <node>  "
               f"| --unsure --assign <node>  | --ignore  | --new  | --only <detail>")


@unresolved_grp.command("dismiss")
@click.argument("key")
@click.option("--actor", default="local")
@click.option("--db", default="index.sqlite")
def unresolved_dismiss_cmd(key, actor, db):
    """Remind-me-later: never re-prompt ACTOR about this cluster (anti-nag)."""
    from .resolve import dismiss_cluster
    out = dismiss_cluster(Path(db), key, actor)
    click.echo(f"dismissed {out['dismissed']} for {actor}")


@cli.command("resolve")
@click.argument("key")
@click.option("--assign", "assign_to", default=None, help="Existing node id to fold into")
@click.option("--unsure", is_flag=True, help="Assign but flag review_needed (low confidence)")
@click.option("--only", "only_detail", default=None, help="This detail only (default: whole cluster)")
@click.option("--ignore", "ignore", is_flag=True, help="Not an element — permanent dismissal")
@click.option("--new", "new", is_flag=True, help="Create a new branch (pre-filled proposal)")
@click.option("--parents", multiple=True, help="Explicit parents for --new (default: sibling context)")
@click.option("--actor", default="local")
@click.option("--repo", default=".", help="Drawings repo root (patches committed detail files)")
@click.option("--db", default="index.sqlite")
@_taxonomy_dir_opt
@click.option("--config-dir", default="config")
def resolve_cmd(key, assign_to, unsure, only_detail, ignore, new, parents,
                actor, repo, db, taxonomy_dir, config_dir):
    """Resolve one quarantine cluster: assign / unsure / ignore / new (8.7.4)."""
    from .taxonomy import TaxonomyError
    from .resolve import assign, ignore_cluster, new_from_cluster
    tax, _settings = _resolve_ctx(taxonomy_dir, config_dir)
    ops = sum([bool(assign_to), bool(ignore), bool(new)])
    if ops != 1:
        click.echo("choose exactly one of --assign / --ignore / --new", err=True)
        raise SystemExit(1)
    try:
        if assign_to:
            out = assign(Path(db), taxonomy_dir, key, assign_to, actor,
                         only_detail, unsure, repo)
            click.echo(f"{out['rid']}: {out['reclassified']} occurrence(s) -> "
                       f"{out['into']} in {len(out['details'])} detail(s)")
            if out["synonyms_added"]:
                click.echo(f"  synonyms: {', '.join(out['synonyms_added'])}")
        elif ignore:
            out = ignore_cluster(Path(db), taxonomy_dir, key, actor, repo)
            click.echo(f"{out['rid']}: ignored {len(out['ignored'])} string(s)")
        else:
            out = new_from_cluster(Path(db), taxonomy_dir, key, actor,
                                   list(parents) or None)
            click.echo(f"proposal {out['slug']} pre-filled ({out['id']}) — "
                       f"write the one-sentence definition into {out['path']}, "
                       f"then `gitail proposals approve {out['slug']}`")
    except TaxonomyError as ex:
        click.echo(f"FAILED: {ex}", err=True)
        raise SystemExit(1)


@cli.group("resolutions", invoke_without_command=True)
@click.pass_context
@click.option("--db", default="index.sqlite")
def resolutions_grp(ctx, db):
    """Reviewable vocabulary-decision log (8.7.5)."""
    if ctx.invoked_subcommand is not None:
        return
    from .resolve import resolutions_recent
    rows = resolutions_recent(Path(db))
    if not rows:
        click.echo("no resolutions yet")
        return
    for r in rows:
        flag = " (undone)" if r.get("undone") else ""
        click.echo(f"{r['rid']} {r['created_at'][:10]} {r['actor']} {r['action']} "
                   f"{r.get('target') or ''} [{r.get('cluster_key') or ''}]{flag}")


@resolutions_grp.command("undo")
@click.argument("rid")
@click.option("--actor", default="local")
@click.option("--repo", default=".")
@click.option("--db", default="index.sqlite")
@_taxonomy_dir_opt
def resolutions_undo_cmd(rid, actor, repo, db, taxonomy_dir):
    """Revert a resolution: drawings back to quarantine, synonym removed."""
    from .taxonomy import TaxonomyError
    from .resolve import undo_resolution
    try:
        out = undo_resolution(Path(db), taxonomy_dir, rid, actor, repo)
    except TaxonomyError as ex:
        click.echo(f"FAILED: {ex}", err=True)
        raise SystemExit(1)
    click.echo(f"{out['rid']}: reverted {out['reverts']} "
               f"({out['occurrences_reopened']} occurrence(s) reopened)")


@cli.command("digest")
@click.option("--days", default=7, type=int)
@click.option("--db", default="index.sqlite")
@_taxonomy_dir_opt
@click.option("--config-dir", default="config")
def digest_cmd(days, db, taxonomy_dir, config_dir):
    """Weekly taxonomy-owner summary: new clusters, pending, high-impact (8.7.3C)."""
    from .resolve import digest
    tax, settings = _resolve_ctx(taxonomy_dir, config_dir)
    d = digest(Path(db), tax, days,
               int(settings.get("digest_min_occurrences", 10)))
    click.echo(f"quarantine: {d['pending_total']} clusters "
               f"({d['pending_occurrences']} occurrences), "
               f"oldest {d['oldest_pending'][:10] if d['oldest_pending'] else '—'}")
    if d["new_clusters"]:
        click.echo(f"new in {days}d:")
        for c in d["new_clusters"]:
            click.echo(f"  {c['cluster_id']} {c['label']!r} x{c['occurrence_count']}")
    if d["high_impact"]:
        click.echo("high impact:")
        for c in d["high_impact"]:
            click.echo(f"  {c['cluster_id']} {c['label']!r} x{c['occurrence_count']}")


@cli.command("dim-confirm")
@click.argument("uid")
@click.option("--actor", default="local")
@click.option("--repo", default=".")
@click.option("--db", default="index.sqlite")
@_taxonomy_dir_opt
def dim_confirm_cmd(uid, actor, repo, db, taxonomy_dir):
    """Confirm a Chain D dimension attribution: promotes it to medium/human
    (addendum B.3). Pending Chain D items list in the post-upload panel."""
    from .taxonomy import TaxonomyError
    from .resolve import confirm_dim, list_dim_pending
    if uid in ("--list", "list"):
        items = list_dim_pending(Path(db))
        if not items:
            click.echo("no pending Chain D dimensions")
            return
        for r in items:
            click.echo(f"{r['uid']} {r['drawing']} {r['label'] or r['value']}mm "
                       f"-> {r['material']} [{r['status']}]")
        return
    try:
        out = confirm_dim(Path(db), taxonomy_dir, uid, actor, repo)
    except TaxonomyError as ex:
        click.echo(f"FAILED: {ex}", err=True)
        raise SystemExit(1)
    if out.get("already"):
        click.echo(f"{uid}: already confirmed")
    else:
        click.echo(f"{out['rid']}: {uid} -> {out['material']} "
                   f"{out['value']}mm (medium, human)")


@cli.command("search")
@click.argument("query", required=False)
@click.option("--path", "path", default=None,
              help="Slash path, e.g. component.curtain_wall/part.capping/attr.material/value.aluminium")
@click.option("--expand", "expand", default=None, help="Detail id for the full tree")
@click.option("--json", "as_json", is_flag=True)
@click.option("--db", default="index.sqlite")
@_taxonomy_dir_opt
def search_cmd(query, path, expand, as_json, db, taxonomy_dir):
    """Tree-view search: pruned root-to-match trees per drawing (8.8)."""
    from .taxonomy import TaxonomyError
    from .treeview import expand_detail, search_tree
    tax, _s = _resolve_ctx(taxonomy_dir, None)
    if expand:
        try:
            out = expand_detail(Path(db), tax, expand)
        except KeyError as ex:
            click.echo(f"FAILED: {ex}", err=True)
            raise SystemExit(1)
        if as_json:
            click.echo(json.dumps(out, indent=2, ensure_ascii=False))
        else:
            _show_forest(out["full_tree"])
        return
    node_id = None
    if path:
        segs = [s for s in path.split("/") if s]
        if not segs:
            click.echo("empty --path", err=True)
            raise SystemExit(1)
        for s in segs:
            if tax.resolve(s) not in tax.nodes:
                click.echo(f"FAILED: unknown path segment {s}", err=True)
                raise SystemExit(1)
        node_id = tax.resolve(segs[-1])
    if not query and node_id is None:
        click.echo("give a query, --path, or --expand", err=True)
        raise SystemExit(1)
    try:
        res = search_tree(Path(db), tax, query, node_id)
    except TaxonomyError as ex:
        click.echo(f"FAILED: {ex}", err=True)
        raise SystemExit(1)
    if as_json:
        click.echo(json.dumps(res, indent=2, ensure_ascii=False))
        return
    if res["query"]["node"] is None:
        click.echo(f"no taxonomy match for {query!r} — unclassified hits only")
    else:
        click.echo(f"# {res['query']['node']}")
    for r in res["results"]:
        click.echo(f"\n▸ {r['title']}  ·  {r['source_sheet']}  [{r['confidence']}]")
        _show_pruned(r["pruned_tree"])
        cc = r["collapsed_counts"]
        if cc["sibling_parts"] or cc["total_classifications"]:
            click.echo(f"  (+ {cc['sibling_parts']} other parts, "
                       f"{cc['total_classifications']} classifications)")
    if res["facet_counts"]:
        click.echo("\nfacets: " + ", ".join(f"{k}={v}"
                                            for k, v in res["facet_counts"].items()))
    if res["unclassified"]:
        click.echo("\nUnclassified:")
        for u in res["unclassified"]:
            click.echo(f"  {u['detail_id']} ({u['drawing']}) — "
                       f"{'; '.join(u['matches'][:3])}")


def _show_forest(entries, depth=0):
    for e in entries:
        if e.get("collapsed"):
            click.echo(f"{'  ' * depth}(+ {e.get('parts', 0)} parts, "
                       f"{e.get('classifications', 0)} classifications)")
            continue
        mark = "  ← match" if e.get("matched") else ""
        click.echo(f"{'  ' * depth}└─ {e.get('label') or e['id']}{mark}")
        for cl in e.get("classifications", []) or []:
            bits = [str(cl.get(k)) for k in ("exact_value", "qualifier")
                    if cl.get(k) is not None]
            click.echo(f"{'  ' * (depth + 1)}· "
                       f"{cl.get('element_id')} [{cl.get('confidence')}] "
                       f"{' '.join(bits)}".rstrip())
        _show_forest(e.get("children", []) or [], depth + 1)


def _show_pruned(entries, depth=1):
    for e in entries:
        if e.get("collapsed"):
            continue  # counted at the result level
        mark = "  ← match" if e.get("matched") else ""
        click.echo(f"{'  ' * depth}└─ {e.get('label') or e['id']}{mark}")
        _show_pruned(e.get("children", []) or [], depth + 1)


@cli.command("node-merge")
@click.argument("loser")
@click.option("--into", "winner", required=True)
@click.option("--actor", default="local")
@click.option("--repo", default=".", help="Drawings repo root (rewrites classifications)")
@_taxonomy_dir_opt
def node_merge_cmd(loser, winner, actor, repo, taxonomy_dir):
    """Deprecate LOSER into WINNER: alias + rewritten parents AND every
    referencing classification in repo details/*.json (8.9 #10)."""
    from .taxonomy import TaxonomyError, merge_node
    try:
        out = merge_node(_owned_dir(taxonomy_dir), loser, winner, actor)
    except TaxonomyError as ex:
        click.echo(f"FAILED: {ex}", err=True)
        raise SystemExit(1)
    touched = _rewrite_classification_paths(Path(repo), loser, winner)
    click.echo(f"deprecated {out['deprecated']} into {out['into']}")
    if touched:
        click.echo(f"rewrote {len(touched)} detail file(s): "
                   f"{', '.join(str(p) for p in touched[:5])}"
                   f"{' …' if len(touched) > 5 else ''}")
        click.echo("review + commit the rewrites")


def _owned_dir(taxonomy_dir):
    from .taxonomy import TaxonomyError, resolve_dir
    directory = resolve_dir(taxonomy_dir)
    if directory is None:
        raise TaxonomyError("merges need an owned taxonomy/ — "
                            "run `gitail taxonomy-init` first")
    return directory


def _rewrite_classification_paths(repo: Path, loser: str, winner: str) -> list[Path]:
    """Replace loser ids inside committed classification paths (merge support).
    Additive-safe: only path elements equal to loser change."""
    touched = []
    base = Path(repo, "details")
    if not base.is_dir():
        return touched
    for path in sorted(base.rglob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        changed = False
        for d in data.get("details", []) or []:
            for c in d.get("classifications", []) or []:
                new_path = [winner if e == loser else e for e in (c.get("path") or [])]
                if new_path != c.get("path"):
                    c["path"] = new_path
                    changed = True
        if changed:
            path.write_text(json.dumps(data, sort_keys=True, indent=2,
                                       ensure_ascii=False) + "\n", encoding="utf-8")
            touched.append(path)
    return touched


def _taxonomy_dir_opt(f):
    return click.option("--taxonomy-dir", default=None,
                        help="Owned taxonomy/ (default: ./taxonomy when present, else bundled seed)")(f)


@cli.command("taxonomy-init")
@click.option("--taxonomy-dir", default="taxonomy",
              help="Target directory for the repo-owned vocabulary copy")
def taxonomy_init_cmd(taxonomy_dir):
    """Copy the bundled seed taxonomy into taxonomy/ so vocabulary changes
    become version-controlled pull requests."""
    from .taxonomy import TaxonomyError, init_taxonomy
    try:
        out = init_taxonomy(taxonomy_dir)
    except TaxonomyError as ex:
        click.echo(f"FAILED: {ex}", err=True)
        raise SystemExit(1)
    click.echo(f"taxonomy initialised at {out} — commit it, then register/propose")


@cli.command("tree")
@click.option("--node", "node", default=None, help="Subtree root, e.g. component.window")
@click.option("--search", "search_q", default=None, help="Match labels, synonyms and definitions")
@_taxonomy_dir_opt
@click.option("--json", "as_json", is_flag=True)
def tree_cmd(node, search_q, taxonomy_dir, as_json):
    """Render the taxonomy tree (a view over the DAG — multi-parent nodes
    appear in every path)."""
    from .taxonomy import TaxonomyError, load_taxonomy
    try:
        tax = load_taxonomy(taxonomy_dir)
    except TaxonomyError as ex:
        click.echo(f"FAILED: {ex}", err=True)
        raise SystemExit(1)
    if search_q:
        hits = tax.search(search_q)
        if as_json:
            click.echo(json.dumps([{"id": n["id"], "label": n["label"],
                                    "level": n["level"], "score": s,
                                    "synonyms": n.get("synonyms") or []}
                                   for n, s in hits], indent=2, ensure_ascii=False))
        elif not hits:
            click.echo("no matches")
        else:
            for n, s in hits:
                click.echo(f"{n['id']}  {n['label']}  ({int(s * 100)}%)")
        return
    try:
        forest = tax.render(node)
    except TaxonomyError as ex:
        click.echo(f"FAILED: {ex}", err=True)
        raise SystemExit(1)

    def show(entries, depth=0):
        for e in entries:
            if as_json:
                continue
            flag = f" -> {e['deprecated_by']}" if e.get("deprecated_by") else ""
            click.echo(f"{'  ' * depth}{e['label']} [{e['id']}]"
                       + (f"  (aka {', '.join(e['synonyms'][:4])})" if e["synonyms"] else "")
                       + flag)
            show(e["children"], depth + 1)

    if as_json:
        click.echo(json.dumps(forest, indent=2, ensure_ascii=False))
    else:
        click.echo(f"# taxonomy ({tax.source})")
        show(forest)


@cli.command("register")
@click.option("--level", required=True, type=click.Choice(["component", "part", "attribute", "value"]))
@click.option("--label", required=True, help="Human-readable name")
@click.option("--parents", "parents", multiple=True, required=True,
              help="One or more existing node ids (repeatable; multi-parent is normal)")
@click.option("--synonyms", default="", help="Comma-separated; critical for search recall")
@click.option("--definition", required=True, help="One sentence — undefined nodes are rejected")
@click.option("--value-type", "value_type", default=None,
              type=click.Choice(["enum", "numeric", "structured", "free_text"]),
              help="Attributes only")
@click.option("--unit", default=None, help="Numeric attributes only (default mm)")
@click.option("--enum-members", "enum_members", default="",
              help="Enum attributes only: comma-separated initial values")
@click.option("--example-detail", "example_detail", required=True,
              help="A drawing demonstrating it — no node without evidence")
@click.option("--submitted-by", "submitted_by", default="cli")
@_taxonomy_dir_opt
@click.option("--config-dir", default="config")
def register_cmd(level, label, parents, synonyms, definition, value_type, unit,
                 enum_members, example_detail, submitted_by, taxonomy_dir, config_dir):
    """Propose a new taxonomy node. Near-duplicates are BLOCKED with the
    existing match offered — the main defence against taxonomy rot."""
    from .taxonomy import TaxonomyError, load_settings
    from .register import propose
    settings = load_settings(config_dir)
    try:
        res = propose(taxonomy_dir, level, label.strip(), list(parents), definition,
                      example_detail, [s for s in (synonyms or "").split(",") if s.strip()],
                      value_type, unit,
                      [s for s in (enum_members or "").split(",") if s.strip()],
                      submitted_by,
                      threshold=float(settings.get("similarity_block_threshold", 0.75)))
    except TaxonomyError as ex:
        click.echo(f"BLOCKED: {ex}", err=True)
        raise SystemExit(1)
    click.echo(f"proposed {res['id']} -> {res['path']} (status: pending)")


@cli.group("proposals", invoke_without_command=True)
@click.pass_context
@_taxonomy_dir_opt
def proposals_grp(ctx, taxonomy_dir):
    """Review pending taxonomy proposals."""
    if ctx.invoked_subcommand is None:
        from .taxonomy import TaxonomyError
        from .register import list_proposals
        try:
            items = list_proposals(taxonomy_dir)
        except TaxonomyError as ex:
            click.echo(f"FAILED: {ex}", err=True)
            raise SystemExit(1)
        if not items:
            click.echo("no pending proposals")
            return
        for p in items:
            click.echo(f"{p['slug']}  [{p['level']}] {p['label']}  "
                       f"parents={','.join(p['parents'])}  by {p.get('submitted_by')}")


@proposals_grp.command("list")
@_taxonomy_dir_opt
def proposals_list_cmd(taxonomy_dir):
    """List pending proposals."""
    from .taxonomy import TaxonomyError
    from .register import list_proposals
    try:
        items = list_proposals(taxonomy_dir)
    except TaxonomyError as ex:
        click.echo(f"FAILED: {ex}", err=True)
        raise SystemExit(1)
    if not items:
        click.echo("no pending proposals")
        return
    for p in items:
        click.echo(f"{p['slug']}  [{p['level']}] {p['label']}  "
                   f"parents={','.join(p['parents'])}  by {p.get('submitted_by')}")


@proposals_grp.command("approve")
@click.argument("slug")
@_taxonomy_dir_opt
@click.option("--config-dir", default="config")
def proposals_approve_cmd(slug, taxonomy_dir, config_dir):
    """Promote a proposal into taxonomy/nodes/."""
    from .taxonomy import TaxonomyError, load_settings
    from .register import approve
    settings = load_settings(config_dir)
    try:
        res = approve(taxonomy_dir, slug,
                      threshold=float(settings.get("similarity_block_threshold", 0.75)))
    except TaxonomyError as ex:
        click.echo(f"FAILED: {ex}", err=True)
        raise SystemExit(1)
    click.echo(f"approved {res['id']} -> {res['file']}"
               + (f" (enum {res['enum_extended']} extended)" if res.get("enum_extended") else ""))


@proposals_grp.command("merge")
@click.argument("slug")
@click.option("--into", "into_id", required=True, help="Existing node id to fold into")
@_taxonomy_dir_opt
def proposals_merge_cmd(slug, into_id, taxonomy_dir):
    """Fold a proposal into an existing node (label becomes a synonym)."""
    from .taxonomy import TaxonomyError
    from .register import merge_proposal
    try:
        res = merge_proposal(taxonomy_dir, slug, into_id)
    except TaxonomyError as ex:
        click.echo(f"FAILED: {ex}", err=True)
        raise SystemExit(1)
    click.echo(f"merged {res['merged']} into {res['into']}")


def _table(rows: list[dict], cols=("project", "drawing", "element_id", "material", "part", "value", "text_raw", "status", "commit_sha", "author")):
    extra = [c for c in ("confidence", "attribution_chain") if any(r.get(c) for r in rows)]
    cols = tuple(list(cols) + extra)
    disp = [{**r, "commit_sha": _short(r.get("commit_sha"))} for r in rows]
    widths = {c: max([len(c)] + [len(str(r.get(c) or "")) for r in disp]) for c in cols}
    click.echo("  ".join(c.ljust(widths[c]) for c in cols))
    for r in disp:
        click.echo("  ".join(str(r.get(c) or "").ljust(widths[c]) for c in cols))


@cli.command("find")
@click.option("--material", multiple=True, help="Repeatable; multiple values stack (drawings mode)")
@click.option("--part", multiple=True, help="Component noun, e.g. lining, door (repeatable)")
@click.option("--value", multiple=True, type=float)
@click.option("--text", "text_q", multiple=True)
@click.option("--drawing", multiple=True)
@click.option("--project", multiple=True, help="Project folder under drawings/ (e.g. StageC)")
@click.option("--ever", is_flag=True, help="Search all historical states, not just current")
@click.option("--match", "match", type=click.Choice(["elements", "drawings"]), default="elements",
              help="elements: rows matching all filters. drawings: drawings containing each filter (stacked).")
@click.option("--tolerance", default=0.0, type=float,
              help="Thickness tolerance ±mm around --value (exact by default)")
@click.option("--include-unattributed", is_flag=True,
              help="Include loose numbers bound to no material (off by default)")
@click.option("--include-low-confidence", is_flag=True,
              help="Include low-confidence/conflicting attributions (off by default)")
@click.option("--measure", default=None,
              help="Measure type: thickness (default for value queries), setdown, fall, setback, dimension")
@click.option("--json", "as_json", is_flag=True)
@click.option("--db", default="index.sqlite")
def find_cmd(material, part, value, text_q, drawing, project, match, ever, tolerance,
             include_unattributed, include_low_confidence, measure, as_json, db):
    """Search elements. Repeat a flag to stack it: drawings containing EACH value win."""
    from .query import find, search_stacked
    stacked = [(k, str(v)) for k, vals in
               (("material", material), ("part", part), ("value", value), ("text", text_q),
                ("drawing", drawing), ("project", project)) for v in vals]
    if match == "elements" and len(stacked) > 1:
        match = "drawings"  # one element can't be two materials; user means stacked
        click.echo("note: multiple filters -> matching DRAWINGS containing each", err=True)
    if match == "drawings":
        res = search_stacked(Path(db), stacked, ever=ever, tolerance=tolerance,
                             include_low=include_low_confidence,
                             include_unattr=include_unattributed)
        if as_json:
            click.echo(json.dumps(res, indent=2, ensure_ascii=False))
        elif not res["drawings"]:
            click.echo("no drawings match all filters")
        else:
            for d in res["drawings"]:
                flag = " (via history)" if d["via_history"] else ""
                click.echo(f"== {d['drawing']}  [{d['project'] or 'no project'}]  {d['elements']} elements{flag}")
            _table(res["rows"])
        return
    rows = find(Path(db),
                material=material[0] if material else None,
                part=part[0] if part else None,
                value=value[0] if value else None,
                text=text_q[0] if text_q else None,
                drawing=drawing[0] if drawing else None,
                project=project[0] if project else None, ever=ever,
                tolerance=tolerance, include_unattr=include_unattributed,
                include_low_confidence=include_low_confidence,
                measure=measure)
    if as_json:
        click.echo(json.dumps(rows, indent=2, ensure_ascii=False))
    elif not rows:
        click.echo("no matches")
    else:
        _table(rows)


@cli.command("history")
@click.argument("element_id")
@click.option("--json", "as_json", is_flag=True)
@click.option("--db", default="index.sqlite")
def history_cmd(element_id, as_json, db):
    """Full life of one element: value per commit with author/message."""
    from .query import history
    rows = history(Path(db), element_id)
    if as_json:
        click.echo(json.dumps(rows, indent=2, ensure_ascii=False))
    elif not rows:
        click.echo("unknown element")
    else:
        for r in rows:
            click.echo(f"{(r['commit_date'] or '')[:10]} {(r['commit_sha'] or '')[:7]} "
                       f"{r['status']:18} mat={r['material']} val={r['value']} "
                       f"text={r['text_raw']!r} by {r['author']} — {r['commit_message']}")


@cli.command("at")
@click.option("--commit", required=True)
@click.option("--drawing", required=True)
@click.option("--json", "as_json", is_flag=True)
@click.option("--db", default="index.sqlite")
def at_cmd(commit, drawing, as_json, db):
    """Snapshot of a drawing at a commit."""
    from .query import at
    rows = at(Path(db), commit, drawing)
    if as_json:
        click.echo(json.dumps(rows, indent=2, ensure_ascii=False))
    elif not rows:
        click.echo("no rows")
    else:
        _table(rows)


@cli.command("revisions")
@click.argument("drawing")
@click.option("--json", "as_json", is_flag=True)
@click.option("--db", default="index.sqlite")
def revisions_cmd(drawing, as_json, db):
    """Revision list for one drawing: is this upload iteration N, and what changed."""
    from .query import drawing_history
    revs = drawing_history(Path(db), drawing)
    if as_json:
        click.echo(json.dumps(revs, indent=2, ensure_ascii=False))
    elif not revs:
        click.echo("unknown drawing")
    else:
        for r in revs:
            click.echo(f"rev {r['revision']} {(r['commit_sha'] or '')[:7]} "
                       f"{(r['commit_date'] or '')[:10]} changed={r['changed']} "
                       f"{r['counts']} by {r['author']} — {r['commit_message']}")


@cli.command("changed")
@click.option("--from", "from_sha", required=True)
@click.option("--to", "to_sha", default="HEAD")
@click.option("--json", "as_json", is_flag=True)
@click.option("--db", default="index.sqlite")
def changed_cmd(from_sha, to_sha, as_json, db):
    """Changed elements between commits — before AND after values, not deltas."""
    from .query import changed
    rows = changed(Path(db), from_sha, to_sha)
    if as_json:
        click.echo(json.dumps(rows, indent=2, ensure_ascii=False))
    elif not rows:
        click.echo("no changes")
    else:
        for c in rows:
            b, a = c.get("before") or {}, c.get("after") or {}
            click.echo(f"{c['drawing']} {c['element_id']} [{c['change']}] "
                       f"before=({b.get('material')},{b.get('value')},{b.get('text_raw')!r}) "
                       f"after=({a.get('material')},{a.get('value')},{a.get('text_raw')!r})")


@cli.command("report")
@click.option("--db", default="index.sqlite")
@click.option("--html", "html_out", default="report.html")
@click.option("--markdown", "md_out", default=None)
@click.option("--pdf-dir", default="pdf", help="Committed PDF previews dir (for links + unindexed list)")
@click.option("--drawings-dir", default="drawings", help="Drawings root (for unindexed list)")
@click.option("--images-dir", default="images", help="View-only reference images (never parsed)")
@click.option("--github-base", default=None, help="Override GitHub drawings base URL")
def report_cmd(db, html_out, md_out, pdf_dir, drawings_dir, images_dir, github_base):
    """Static search interface (HTML with client-side filters) + optional markdown."""
    from .report import write_html, write_markdown
    h = write_html(Path(db), Path(html_out), pdf_dir=Path(pdf_dir),
                   drawings_dir=Path(drawings_dir), images_dir=Path(images_dir),
                   github_base=github_base)
    click.echo(f"wrote {h}")
    if md_out:
        m = write_markdown(Path(db), Path(md_out))
        click.echo(f"wrote {m}")


@cli.command("render")
@click.option("--drawings-dir", default="drawings", help="DXF inputs (recursive, incl. <project>/ subfolders)")
@click.option("--pdf-dir", default="pdf", help="PDF previews output (mirrors drawings/, committed)")
@click.option("--force", is_flag=True, help="Re-render even when PDF is newer than DXF")
@click.option("--check", is_flag=True, help="CI mode: fail if any PDF missing/outdated (no writes)")
def render_cmd(drawings_dir, pdf_dir, force, check):
    """Render every DXF to a viewable PDF (viewing only — parsing stays DXF)."""
    from .render import check_all, render_all
    if check:
        missing = check_all(Path(drawings_dir), Path(pdf_dir))
        if missing:
            click.echo(f"PDF previews missing/outdated for: {', '.join(missing)}", err=True)
            click.echo("Run: gitail render --drawings-dir drawings --pdf-dir pdf", err=True)
            raise SystemExit(1)
        click.echo(f"pdf previews in sync")
        return
    results = render_all(Path(drawings_dir), Path(pdf_dir), force=force)
    if not results:
        click.echo("no DXF drawings found")
        return
    for r in results:
        click.echo(f"{r['drawing']}: {r['action']}")
    failed = [r for r in results if r["action"] == "FAILED"]
    if failed:
        raise SystemExit(1)


@cli.command("ingest")
@click.argument("file", type=click.Path(exists=True, dir_okay=False))
@click.option("--project", default="", help="Project folder under drawings/ (created if needed)")
@click.option("--repo", default=".", help="Drawing repository root")
@click.option("--drawings-dir", default="drawings")
@click.option("--state-dir", default="state")
@click.option("--pdf-dir", default="pdf")
@click.option("--images-dir", default="images")
@click.option("--config-dir", default="config")
@click.option("--details-dir", default="details")
@_taxonomy_dir_opt
@click.option("--thumbs-dir", default="thumbs")
@click.option("--crops-dir", default="crops")
@click.option("--db", default="index.sqlite")
def ingest_cmd(file, project, repo, drawings_dir, state_dir, pdf_dir,
               images_dir, config_dir, details_dir, taxonomy_dir,
               thumbs_dir, crops_dir, db):
    """Ingest one file by format (7.6): DXF extracts + renders + commits;
    DWG converts via ODA File Converter when present (else instructs);
    vector PDF parses its text layer (reference-only); scanned PDF and
    images store as view-only references. Never blocks on quarantine."""
    import shutil
    import subprocess
    from pathlib import Path as _P
    repo_p = _P(repo).resolve()

    def _abs(p):
        pp = _P(p)
        return str((repo_p / pp).resolve() if not pp.is_absolute() else pp)

    src = _P(file)
    ext = src.suffix.lower().lstrip(".")
    if ext not in ("dxf", "dwg", "pdf", "png", "jpg", "jpeg"):
        click.echo("ingest takes .dxf/.dwg/.pdf/.png/.jpg", err=True)
        raise SystemExit(1)
    drawings = _P(_abs(drawings_dir))
    pdfs = _P(_abs(pdf_dir))
    images = _P(_abs(images_dir))
    sibling_dir = {"dxf": drawings, "dwg": drawings, "pdf": pdfs}.get(
        ext, images)
    dest_dir = sibling_dir / project if project else sibling_dir
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    if src.resolve() != dest.resolve():
        shutil.copy(src, dest)
    if ext == "dwg":
        # Brief 7.6: DWG converts via the ODA File Converter CLI, never parsed.
        oda = shutil.which("ODAFileConverter")
        if oda is None:
            click.echo(f"saved {dest} (never parsed). Export to ASCII DXF R2018+ "
                       f"via ODA File Converter or AutoCAD, then "
                       f"`gitail ingest {dest.with_suffix('.dxf')}`", err=True)
            return
        out = dest.with_suffix(".dxf")
        try:
            subprocess.run([oda, str(dest.parent), str(dest.parent),
                            "ACAD2018", "DXF", "0", "1", src.name],
                           check=True, capture_output=True)
        except subprocess.CalledProcessError as ex:
            click.echo(f"ODA conversion failed: {ex}", err=True)
            raise SystemExit(1)
        click.echo(f"converted -> {out}")
        dest = out
        ext = "dxf"
    ctx = {"repo": str(repo_p), "db": _abs(db),
           "drawings_dir": _abs(drawings_dir), "state_dir": _abs(state_dir),
           "pdf_dir": _abs(pdf_dir), "images_dir": _abs(images_dir),
           "config_dir": str(config_dir), "details_dir": _abs(details_dir),
           "taxonomy_dir": _abs(taxonomy_dir) if taxonomy_dir else None,
           "thumbs_dir": _abs(thumbs_dir), "crops_dir": _abs(crops_dir)}
    from .serve import _ingest_dxf, _ingest_pdf
    if ext == "dxf":
        summary = _ingest_dxf(ctx, dest)
        _echo_ingest_summary(summary)
    elif ext == "pdf":
        drawing = (f"{project}/{dest.stem}" if project else dest.stem)
        twin = drawings / (drawing + ".dxf")
        if twin.exists():
            click.echo(f"PDF stored as view-only companion of {drawing} "
                       f"(DXF twin is the parsed source).")
            return
        summary = _ingest_pdf(ctx, dest, drawing)
        if summary is None:
            click.echo("PDF has no text layer (scanned image?) — stored as "
                       "view-only reference (fidelity scanned, reusable no).")
            return
        _echo_ingest_summary(summary)
    else:
        target = dest  # already placed under images/<project>/
        click.echo(f"stored {target} as view-only reference (never parsed).")


def _echo_ingest_summary(summary: dict):
    drawing = summary.get("drawing", "?")
    counts = summary.get("statuses", {})
    click.echo(f"{drawing}: {summary.get('elements', 0)} elements {counts} "
               f"revision {summary.get('revision')}")
    note = summary.get("notification") or {}
    if note.get("clusters"):
        click.echo(f"  {len(note['clusters'])} new cluster(s) need review "
                   f"(+{note.get('more', 0)} more) — `gitail unresolved`")
    for w in summary.get("warnings", []) or []:
        click.echo(f"  WARNING: {w}", err=True)


@cli.command("serve")
@click.option("--repo", default=".", help="Drawing repository root")
@click.option("--db", default="index.sqlite")
@click.option("--drawings-dir", default="drawings")
@click.option("--state-dir", default="state")
@click.option("--pdf-dir", default="pdf")
@click.option("--images-dir", default="images", help="View-only reference images (png/jpg, never parsed)")
@click.option("--config-dir", default="config")
@click.option("--details-dir", default="details", help="Detail records root (committed)")
@_taxonomy_dir_opt
@click.option("--thumbs-dir", default="thumbs")
@click.option("--crops-dir", default="crops")
@click.option("--port", default=8000, type=int)
def serve_cmd(repo, db, drawings_dir, state_dir, pdf_dir, images_dir, config_dir, port,
              details_dir, taxonomy_dir, thumbs_dir, crops_dir):
    """Live search + direct upload (reads index.sqlite, writes drawings/)."""
    from .serve import run
    run(repo, db, drawings_dir, state_dir, pdf_dir, images_dir, config_dir, port,
        details_dir, taxonomy_dir, thumbs_dir, crops_dir)


def main():
    cli()


if __name__ == "__main__":
    main()
