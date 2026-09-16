"""CLI — drafters: extract. Everyone else: index/find/history/at/changed/report."""
from __future__ import annotations

import json
from pathlib import Path

import click

from .extract import dxf_version, extract_state, version_supported
from .identity import DEFAULT_TOLERANCE_MM, resolve
from .semantics import load_materials


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


@click.group()
def cli():
    pass


@cli.command()
@click.argument("dxf_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--state-dir", default="state")
@click.option("--config-dir", default="config")
@click.option("--drawings-dir", default="drawings",
              help="Drawings root — used to derive project/drawing ids (drawings/<project>/X.dxf)")
@click.option("--tolerance", default=DEFAULT_TOLERANCE_MM, type=float)
@click.option("--check", is_flag=True,
              help="CI mode: fail if committed state/*.jsonl is out of sync (no writes)")
def extract(dxf_path, state_dir, config_dir, drawings_dir, tolerance, check):
    """Extract DXF canonical state, resolve identity, write state files."""
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

    materials_cfg = load_materials(config_dir / "materials.yaml") if (config_dir / "materials.yaml").exists() else {}
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

    if check:
        old = state_jsonl.read_text(encoding="utf-8") if state_jsonl.exists() else ""
        if old != new_text:
            click.echo(f"OUT OF SYNC: {drawing}.dxf changed without re-running extract", err=True)
            raise SystemExit(1)
        click.echo(f"{drawing}: in sync ({len(storable)} elements)")
        return

    state_jsonl.parent.mkdir(parents=True, exist_ok=True)
    with open(state_jsonl, "w", encoding="utf-8", newline="\n") as f:
        f.write(new_text)
    with open(state_idmap, "w", encoding="utf-8", newline="\n") as f:
        json.dump(new_idmap, f, sort_keys=True, indent=2, ensure_ascii=False)
        f.write("\n")

    from collections import Counter
    counts = Counter(r.get("status", "?") for r in resolved)
    click.echo(f"{drawing}: {len(resolved)} elements {dict(counts)}")
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
def index_cmd(repo, db, state_dir):
    """Walk git log of the drawing repo and (incrementally) build index.sqlite."""
    from .index import build_index
    res = build_index(Path(repo), Path(db), state_dir)
    click.echo(f"indexed {res['commits_processed']} new commits, +{res['rows_inserted']} rows, total {res['total_rows']} -> {db}")


def _short(v):
    v = str(v or "")
    return v[:7] if len(v) == 40 and all(ch in "0123456789abcdef" for ch in v.lower()) else v


def _table(rows: list[dict], cols=("project", "drawing", "element_id", "material", "value", "text_raw", "status", "commit_sha", "author")):
    disp = [{**r, "commit_sha": _short(r.get("commit_sha"))} for r in rows]
    widths = {c: max([len(c)] + [len(str(r.get(c) or "")) for r in disp]) for c in cols}
    click.echo("  ".join(c.ljust(widths[c]) for c in cols))
    for r in disp:
        click.echo("  ".join(str(r.get(c) or "").ljust(widths[c]) for c in cols))


@cli.command("find")
@click.option("--material", multiple=True, help="Repeatable; multiple values stack (drawings mode)")
@click.option("--value", multiple=True, type=float)
@click.option("--text", "text_q", multiple=True)
@click.option("--drawing", multiple=True)
@click.option("--project", multiple=True, help="Project folder under drawings/ (e.g. StageC)")
@click.option("--ever", is_flag=True, help="Search all historical states, not just current")
@click.option("--match", "match", type=click.Choice(["elements", "drawings"]), default="elements",
              help="elements: rows matching all filters. drawings: drawings containing each filter (stacked).")
@click.option("--json", "as_json", is_flag=True)
@click.option("--db", default="index.sqlite")
def find_cmd(material, value, text_q, drawing, project, match, ever, as_json, db):
    """Search elements. Repeat a flag to stack it: drawings containing EACH value win."""
    from .query import find, search_stacked
    stacked = [(k, str(v)) for k, vals in
               (("material", material), ("value", value), ("text", text_q),
                ("drawing", drawing), ("project", project)) for v in vals]
    if match == "elements" and len(stacked) > 1:
        match = "drawings"  # one element can't be two materials; user means stacked
        click.echo("note: multiple filters -> matching DRAWINGS containing each", err=True)
    if match == "drawings":
        res = search_stacked(Path(db), stacked, ever=ever)
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
                value=value[0] if value else None,
                text=text_q[0] if text_q else None,
                drawing=drawing[0] if drawing else None,
                project=project[0] if project else None, ever=ever)
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
@click.option("--github-base", default=None, help="Override GitHub drawings base URL")
def report_cmd(db, html_out, md_out, pdf_dir, drawings_dir, github_base):
    """Static search interface (HTML with client-side filters) + optional markdown."""
    from .report import write_html, write_markdown
    h = write_html(Path(db), Path(html_out), pdf_dir=Path(pdf_dir),
                   drawings_dir=Path(drawings_dir), github_base=github_base)
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


@cli.command("serve")
@click.option("--repo", default=".", help="Drawing repository root")
@click.option("--db", default="index.sqlite")
@click.option("--drawings-dir", default="drawings")
@click.option("--state-dir", default="state")
@click.option("--pdf-dir", default="pdf")
@click.option("--config-dir", default="config")
@click.option("--port", default=8000, type=int)
def serve_cmd(repo, db, drawings_dir, state_dir, pdf_dir, config_dir, port):
    """Live search + direct upload (reads index.sqlite, writes drawings/)."""
    from .serve import run
    run(repo, db, drawings_dir, state_dir, pdf_dir, config_dir, port)


def main():
    cli()


if __name__ == "__main__":
    main()
