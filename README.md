# Gitails-Tools — arcdiff (Parts 1–3)

Git-backed element-state history for AutoCAD detail drawings.
Brief: `../arcdiff-opencode-brief.md` (kept in parent `gitails/` folder).

## Scope now: Parts 1–3

* `arcdiff/extract.py` — DXF → canonical state (0.1mm rounding, deterministic sort)
* `arcdiff/identity.py` — 4-tier identity + global translation normalisation
* `arcdiff/semantics.py` — annotation + hatch parsing from `config/materials.yaml`
* `arcdiff/cli.py` — single drafter command

Parts 4–6 (index/query/report/Action) deferred.

## Drafter workflow (single command)

```bash
pip install -e .
arcdiff extract <drawings-repo>/drawings/D-101.dxf --state-dir <drawings-repo>/state --config-dir config
```

Commits `state/D-101.jsonl` + `state/D-101.idmap.json` alongside the DXF.
Never regenerate `.idmap.json` from scratch — it only grows.

## Dev

```bash
C:\AI\python.exe -m pytest -q
```

Python ≥3.11 (tested 3.12.7), ezdxf, click, pyyaml.
