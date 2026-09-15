# Gitails-Tools — arcdiff (Parts 1–6)

Git-backed element-state history for AutoCAD detail drawings.
Brief: `../arcdiff-opencode-brief.md` (kept in parent `gitails/` folder).

## Modules

* `arcdiff/extract.py` (Part 1) — DXF → canonical state (0.1mm rounding, deterministic sort)
* `arcdiff/identity.py` (Part 2) — 4-tier identity + global translation normalisation
* `arcdiff/semantics.py` (Part 3) — annotation + hatch parsing from `config/materials.yaml`
* `arcdiff/index.py` (Part 4) — git log of `state/*.jsonl` → `index.sqlite` (incremental)
* `arcdiff/query.py` + CLI (Part 5) — `find` / `history` / `at` / `changed`
* `arcdiff/report.py` — static HTML search page (no server) + markdown
* `arcdiff/render.py` — DXF → PDF previews for viewing (never parsed)
* Part 6 — `.github/workflows/ci.yml` here; PR review workflow lives in Gitails-DRAWINGS

## Drafter workflow (single command, runs locally)

```bash
pip install -e .
arcdiff extract <drawings-repo>/drawings/D-101.dxf --state-dir <drawings-repo>/state --config-dir config
```

Commits `state/D-101.jsonl` + `state/D-101.idmap.json` alongside the DXF.
Never regenerate `.idmap.json` from scratch — it only grows.

## Search interface

No server, no web app. Two surfaces:

```bash
# the brief's query — every drawing ever glazed 3mm, with revision + new value:
arcdiff index --repo <drawings-repo> --db index.sqlite
arcdiff find --db index.sqlite --material glass --value 3 --ever
arcdiff history e_9125db --db index.sqlite
arcdiff changed --from <shaA> --to <shaB> --db index.sqlite
arcdiff find --db index.sqlite --text TOUGHENED --json

# static page — open in a browser, filter locally (material/value/text/drawing + ever):
arcdiff report --db index.sqlite --html report.html
# viewable PDFs — browsers show DXF as text, so render previews (viewing only):
arcdiff render --drawings-dir <drawings-repo>/drawings --pdf-dir <drawings-repo>/pdf
```

`index.sqlite` is derived, never committed. `report.html` is generated on demand
and in CI (PR artifact).

## Dev

```bash
C:\AI\python.exe -m pytest -q   # 16 tests (Parts 1-5)
```

Python ≥3.11 (tested 3.12.7), ezdxf, click, pyyaml, matplotlib (PDF previews).

## Inputs: DXF / DWG / PDF

Parsing always comes from ASCII DXF R2018+ (brief constraint — never DWG, never PDF content).
`.dwg` may sit beside the `.dxf`; `extract` warns if the DWG is newer than the DXF
(re-export first via ODA File Converter or AutoCAD). A lone `.pdf` with no `.dxf`
is view-only: linked in the report, but not searchable (no element state).
