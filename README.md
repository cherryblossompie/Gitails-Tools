# Gitails-Tools — gitail (Parts 1–6)

Git-backed element-state history for AutoCAD detail drawings.
Brief: `../arcdiff-opencode-brief.md` (kept in parent `gitails/` folder).

## Modules

* `gitail/extract.py` (Part 1) — DXF → canonical state (0.1mm rounding, deterministic sort)
* `gitail/identity.py` (Part 2) — 4-tier identity + global translation normalisation
* `gitail/semantics.py` (Part 3) — annotation + hatch parsing from `config/materials.yaml`
* `gitail/index.py` (Part 4) — git log of `state/*.jsonl` → `index.sqlite` (incremental)
* `gitail/query.py` + CLI (Part 5) — `find` / `history` / `at` / `changed`
* `gitail/report.py` — static HTML search page (no server) + markdown
* `gitail/render.py` — DXF → PDF previews for viewing (never parsed)
* `gitail/serve.py` — optional live server: reads index.sqlite directly, uploads save straight in
* Part 6 — `.github/workflows/ci.yml` here; PR review workflow lives in Gitails-DRAWINGS

## Drafter workflow (single command, runs locally)

```bash
pip install -e .
gitail extract <drawings-repo>/drawings/D-101.dxf --state-dir <drawings-repo>/state --config-dir config
```

Commits `state/D-101.jsonl` + `state/D-101.idmap.json` alongside the DXF.
Never regenerate `.idmap.json` from scratch — it only grows.

## Search interface

No server, no web app. Two surfaces:

```bash
# the brief's query — every drawing ever glazed 3mm, with revision + new value:
gitail index --repo <drawings-repo> --db index.sqlite
gitail find --db index.sqlite --material glass --value 3 --ever
# stacked: repeat flags — only drawings containing EACH win (whole drawings shown):
gitail find --db index.sqlite --material concrete --material steel
gitail history e_9125db --db index.sqlite
gitail changed --from <shaA> --to <shaB> --db index.sqlite
gitail find --db index.sqlite --text TOUGHENED --json

# static page — open in a browser, filter locally (material/value/text/drawing + ever):
gitail report --db index.sqlite --html report.html
# live page — reads index.sqlite directly; uploads save into drawings/<project>/ + reindex:
cd <drawings-repo> && gitail serve --port 8000   # open http://localhost:8000
# viewable PDFs — browsers show DXF as text, so render previews (viewing only):
gitail render --drawings-dir <drawings-repo>/drawings --pdf-dir <drawings-repo>/pdf
```

`index.sqlite` is derived, never committed. `report.html` is generated on demand
and in CI (PR artifact).

## Dev

```bash
C:\AI\python.exe -m pytest -q   # 19 tests
```

Python ≥3.11 (tested 3.12.7), ezdxf, click, pyyaml, matplotlib (PDF previews).

## Inputs: DXF / DWG / PDF

Parsing always comes from ASCII DXF R2018+ (brief constraint — never DWG, never PDF content).
`.dwg` may sit beside the `.dxf`; `extract` warns if the DWG is newer than the DXF
(re-export first via ODA File Converter or AutoCAD). A lone `.pdf` with no `.dxf`
is view-only: linked in the report, but not searchable (no element state).
