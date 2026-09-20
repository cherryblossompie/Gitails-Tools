# Gitails-Tools — gitail (Parts 1–6)

Git-backed element-state history for AutoCAD detail drawings.
Brief: `../arcdiff-opencode-brief.md` (kept in parent `gitails/` folder).

## Modules

* `gitail/extract.py` (Part 1) — DXF → canonical state (0.1mm rounding, deterministic sort)
* `gitail/segment.py` (7.1/7.5) — sheet → detail regions (title markers, island clustering, uncertain fallback) + detail records (`details/<drawing>.json`, committed)
* `gitail/identity.py` (Part 2) — 4-tier identity + global translation normalisation
* `gitail/semantics.py` (Part 3) — annotation + hatch parsing from `config/materials.yaml`
* `gitail/index.py` (Part 4) — git log of `state/*.jsonl` → `index.sqlite` (incremental)
* `gitail/query.py` + CLI (Part 5) — `find` / `history` / `at` / `changed`
* `gitail/report.py` — static HTML search page (no server) + markdown
* `gitail/render.py` — DXF → PDF previews for viewing (never parsed)
* `gitail/serve.py` — optional live server: reads index.sqlite directly, uploads save straight in
* `gitail/taxonomy.py` (8.1) — vocabulary DAG (multi-parent parts, cycle rejection, synonym search, tree rendering) + `taxonomy_seed/` starter copy
* `gitail/register.py` (8.6) — node proposals with duplicate blocking, approve/merge lifecycle
* Part 6 — `.github/workflows/ci.yml` here; PR review workflow lives in Gitails-DRAWINGS

## Drafter workflow (single command, runs locally)

```bash
pip install -e .
gitail extract <drawings-repo>/drawings/D-101.dxf --state-dir <drawings-repo>/state --details-dir <drawings-repo>/details --config-dir config
```

Commits `state/D-101.jsonl` + `state/D-101.idmap.json` + `details/D-101.json` alongside the DXF.
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
# drawing revisions — is this upload iteration N, and what changed each time:
gitail revisions "StageC/D-102" --db index.sqlite
gitail changed --from <shaA> --to <shaB> --db index.sqlite
gitail find --db index.sqlite --text TOUGHENED --json

# static page — open in a browser, filter locally (material/value/text/drawing + ever):
gitail report --db index.sqlite --html report.html
# live page — reads index.sqlite directly; uploads save into drawings/<project>/ + reindex:
cd <drawings-repo> && gitail serve --port 8000   # open http://localhost:8000
# viewable PDFs — browsers show DXF as text, so render previews (viewing only):
gitail render --drawings-dir <drawings-repo>/drawings --pdf-dir <drawings-repo>/pdf
```

## Library: taxonomy, quarantine, tree search

```bash
gitail taxonomy-init --taxonomy-dir taxonomy   # repo-owned vocabulary (PR-reviewable)
gitail tree --search cill                      # synonyms + definitions match
gitail register --level part --label "Drip Groove" --parents component.window \
  --definition "Slot shedding water." --example-detail d_a41f
gitail proposals / approve / merge             # review queue lifecycle
gitail unresolved                              # quarantine clusters (badge counts these)
gitail unresolved show c_0a4                   # crop + occurrences + suggestions
gitail resolve c_0a4 --assign part.jamb_liner  # or --unsure / --ignore / --new / --only
gitail resolutions --recent / undo r_0001      # every decision reviewable, revertible
gitail digest                                  # weekly owner summary
gitail search capping                          # pruned root-to-match trees
gitail search --path component.curtain_wall/part.capping/attr.material/value.aluminium
gitail search --expand d_c91b                  # full tree on demand
gitail details confirm d_a41f --assembly door --context interior  # 7.7 confirm
gitail node-merge part.old --into part.new --repo <drawings-repo>  # 8.9 #10
```

## Taxonomy (Part 8 vocabulary)

```bash
gitail tree --search cill                    # synonym search (seed fallback when no taxonomy/ exists)
gitail taxonomy-init --taxonomy-dir taxonomy # repo-owned copy: vocabulary changes become pull requests
gitail register --level part --label "Drip Groove" --parents component.window \
  --definition "Slot shedding water." --example-detail d_a41f
gitail proposals                             # pending review queue
gitail proposals approve drip_groove
gitail proposals merge <slug> --into part.capping
```

Near-duplicates are blocked at submit (`Coping` → "did you mean `part.capping`?").
Merges deprecate + alias, never delete — drawings may still reference the old id.

`index.sqlite` is derived, never committed. `report.html` is generated on demand
and in CI (PR artifact).

## Dev

```bash
C:\AI\python.exe -m pytest -q   # 101 tests
```

Python ≥3.11 (tested 3.12.7), ezdxf, click, pyyaml, matplotlib (PDF previews).

## Inputs: DXF / DWG / PDF / PNG-JPG

Parsing always comes from ASCII DXF R2018+ (brief constraint — never DWG, never PDF content).
`.dwg` may sit beside the `.dxf`; `extract` warns if the DWG is newer than the DXF
(re-export first via ODA File Converter or AutoCAD).
`.pdf` with a same-stem `.dxf` twin is a view-only companion of that drawing.
`.pdf` with **no** DXF twin is parsed for its **text layer** into searchable element
state (`PDFTEXT` records, positions in sheet mm) — same materials config, same
history/identity pipeline. Scanned-image PDFs have no text layer and stay view-only.
`.png` / `.jpg` uploads land in `images/<project>/` as view-only references
(site photos, scanned markups): thumbnailed on their drawing, never parsed.
