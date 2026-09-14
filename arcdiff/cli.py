"""CLI — drafters: extract. Everyone else: index/find/history/at/changed/report."""
from __future__ import annotations

import json
from pathlib import Path

import click

from .extract import extract_state
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
@click.option("--tolerance", default=DEFAULT_TOLERANCE_MM, type=float)
@click.option("--check", is_flag=True,
              help="CI mode: fail if committed state/*.jsonl is out of sync (no writes)")
def extract(dxf_path, state_dir, config_dir, tolerance, check):
    """Extract DXF canonical state, resolve identity, write state files."""
    dxf_path = Path(dxf_path)
    state_dir = Path(state_dir)
    config_dir = Path(config_dir)
    drawing = dxf_path.stem

    materials_cfg = load_materials(config_dir / "materials.yaml") if (config_dir / "materials.yaml").exists() else {}
    current_raw = extract_state(dxf_path, materials_cfg)
    prev = _load_jsonl(state_dir / f"{drawing}.jsonl") if state_dir.exists() else []
    idmap = _load_idmap(state_dir / f"{drawing}.idmap.json", drawing) if state_dir.exists() else {"drawing": drawing, "elements": {}}
    resolved, new_idmap, event, fuzzy = resolve(current_raw, prev, idmap, tolerance=tolerance)

    EPHEMERAL = ("status", "match_tier", "match_confidence")
    storable = [{k: v for k, v in r.items() if k not in EPHEMERAL and not k.startswith("_")}
                for r in resolved]
    storable.sort(key=lambda r: (r["layer"], r["type"], r["geom"]["x"], r["geom"]["y"], r.get("text_raw") or ""))
    new_text = "".join(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n" for r in storable)

    if check:
        old = (state_dir / f"{drawing}.jsonl").read_text(encoding="utf-8") if (state_dir / f"{drawing}.jsonl").exists() else ""
        if old != new_text:
            click.echo(f"OUT OF SYNC: {drawing}.dxf changed without re-running extract", err=True)
            raise SystemExit(1)
        click.echo(f"{drawing}: in sync ({len(storable)} elements)")
        return

    state_dir.mkdir(parents=True, exist_ok=True)
    with open(state_dir / f"{drawing}.jsonl", "w", encoding="utf-8", newline="\n") as f:
        f.write(new_text)
    with open(state_dir / f"{drawing}.idmap.json", "w", encoding="utf-8", newline="\n") as f:
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


def _table(rows: list[dict], cols=("drawing", "element_id", "material", "value", "text_raw", "status", "commit_sha", "author")):
    disp = [{**r, "commit_sha": _short(r.get("commit_sha"))} for r in rows]
    widths = {c: max([len(c)] + [len(str(r.get(c) or "")) for r in disp]) for c in cols}
    click.echo("  ".join(c.ljust(widths[c]) for c in cols))
    for r in disp:
        click.echo("  ".join(str(r.get(c) or "").ljust(widths[c]) for c in cols))


@cli.command("find")
@click.option("--material", default=None)
@click.option("--value", default=None, type=float)
@click.option("--text", "text_q", default=None)
@click.option("--drawing", default=None)
@click.option("--ever", is_flag=True, help="Search all historical states, not just current")
@click.option("--json", "as_json", is_flag=True)
@click.option("--db", default="index.sqlite")
def find_cmd(material, value, text_q, drawing, ever, as_json, db):
    """Search elements. Default: current state. --ever: full history."""
    from .query import find
    rows = find(Path(db), material=material, value=value, text=text_q, drawing=drawing, ever=ever)
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
def report_cmd(db, html_out, md_out):
    """Static search interface (HTML with client-side filters) + optional markdown."""
    from .report import write_html, write_markdown
    h = write_html(Path(db), Path(html_out))
    click.echo(f"wrote {h}")
    if md_out:
        m = write_markdown(Path(db), Path(md_out))
        click.echo(f"wrote {m}")


def main():
    cli()


if __name__ == "__main__":
    main()
