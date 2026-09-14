"""CLI — drafters run a single command: arcdiff extract <drawing.dxf>."""
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
@click.option("--state-dir", default="state", help="Directory for *.jsonl + *.idmap.json")
@click.option("--config-dir", default="config", help="Directory with materials.yaml")
@click.option("--tolerance", default=DEFAULT_TOLERANCE_MM, type=float,
              help="Fuzzy-match centroid tolerance in mm (default 25)")
def extract(dxf_path, state_dir, config_dir, tolerance):
    """Extract DXF canonical state, resolve identity, write state files."""
    dxf_path = Path(dxf_path)
    state_dir = Path(state_dir)
    config_dir = Path(config_dir)
    drawing = dxf_path.stem
    state_dir.mkdir(parents=True, exist_ok=True)

    materials_cfg = load_materials(config_dir / "materials.yaml") if (config_dir / "materials.yaml").exists() else {}

    current_raw = extract_state(dxf_path, materials_cfg)
    prev = _load_jsonl(state_dir / f"{drawing}.jsonl")
    idmap = _load_idmap(state_dir / f"{drawing}.idmap.json", drawing)

    resolved, new_idmap, event, fuzzy = resolve(current_raw, prev, idmap, tolerance=tolerance)

    # Committed canonical state holds element_id + DXF facts only.
    # status / match_tier / match_confidence are ephemeral (index-time views);
    # stripping them keeps re-extracts byte-identical (acceptance test 1).
    EPHEMERAL = ("status", "match_tier", "match_confidence")
    storable = []
    for r in resolved:
        s = {k: v for k, v in r.items() if k not in EPHEMERAL and not k.startswith("_")}
        storable.append(s)
    storable.sort(key=lambda r: (r["layer"], r["type"], r["geom"]["x"], r["geom"]["y"], r.get("text_raw") or ""))

    # Deterministic byte-stable output: sorted keys, compact-ish, LF only.
    out_path = state_dir / f"{drawing}.jsonl"
    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        for r in storable:
            f.write(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n")
    with open(state_dir / f"{drawing}.idmap.json", "w", encoding="utf-8", newline="\n") as f:
        json.dump(new_idmap, f, sort_keys=True, indent=2, ensure_ascii=False)
        f.write("\n")

    from collections import Counter
    counts = Counter(r.get("status", "?") for r in resolved)
    click.echo(f"{drawing}: {len(resolved)} elements {dict(counts)}")
    if event is not None:
        click.echo(f"translation: dx={event['dx']} dy={event['dy']} "
                   f"({event['matched']}/{event['total']} share offset) — normalised, zero element changes from shift")
    for fr in fuzzy:
        click.echo(f"REVIEW fuzzy: {fr['element_id']} h={fr['dxf_handle']} "
                   f"conf={fr['confidence']} prev={fr['prev_text']!r} curr={fr['curr_text']!r}")


def main():
    cli()


if __name__ == "__main__":
    main()
