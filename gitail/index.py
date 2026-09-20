"""Part 4 — Index: git history of state/*.jsonl -> SQLite.

Plus Part 7: per-commit attribution (bound material/value facts with chains).
Derived artifact, never committed. Rebuildable from Git at any time.
Incremental: tracks indexed_commits, processes only new commits on re-run.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS element_state (
  element_id     TEXT,
  commit_sha     TEXT,
  commit_date    TEXT,
  author         TEXT,
  commit_message TEXT,
  drawing        TEXT,
  type           TEXT,
  layer          TEXT,
  material       TEXT,
  value          REAL,
  unit           TEXT,
  text_raw       TEXT,
  x REAL, y REAL,
  status         TEXT,
  match_tier     TEXT,
  match_confidence REAL,
  PRIMARY KEY (element_id, commit_sha)
);
CREATE TABLE IF NOT EXISTS indexed_commits (
  commit_sha TEXT PRIMARY KEY,
  indexed_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_material ON element_state(material);
CREATE INDEX IF NOT EXISTS idx_value ON element_state(value);
CREATE INDEX IF NOT EXISTS idx_text ON element_state(text_raw);
CREATE INDEX IF NOT EXISTS idx_drawing ON element_state(drawing);
CREATE INDEX IF NOT EXISTS idx_date ON element_state(commit_date);
"""

MIGRATE_PROJECT = "ALTER TABLE element_state ADD COLUMN project TEXT"
IDX_PROJECT = "CREATE INDEX IF NOT EXISTS idx_project ON element_state(project)"
MIGRATE_PART = "ALTER TABLE element_state ADD COLUMN part TEXT"
IDX_PART = "CREATE INDEX IF NOT EXISTS idx_part ON element_state(part)"

ATTRIBUTION_SCHEMA = """
CREATE TABLE IF NOT EXISTS attribution (
  id INTEGER PRIMARY KEY,
  element_id     TEXT,
  commit_sha     TEXT,
  drawing        TEXT,
  project        TEXT,
  material       TEXT,
  part           TEXT,
  value          REAL,
  unit           TEXT,
  qualifier      TEXT,
  measure        TEXT DEFAULT 'thickness',
  confidence     TEXT,
  chain          TEXT,
  conflict       INTEGER,
  UNIQUE(element_id, commit_sha, material, value)
);
CREATE INDEX IF NOT EXISTS idx_attr_matval ON attribution(material, value);
CREATE INDEX IF NOT EXISTS idx_attr_draw ON attribution(drawing);
CREATE INDEX IF NOT EXISTS idx_attr_conf ON attribution(confidence);
CREATE INDEX IF NOT EXISTS idx_attr_measure ON attribution(measure);
"""
MIGRATE_ATTR_MEASURE = "ALTER TABLE attribution ADD COLUMN measure TEXT DEFAULT 'thickness'"
MIGRATE_QUAR_MEASURE = "ALTER TABLE quarantine ADD COLUMN measure TEXT DEFAULT 'thickness'"

# Addendum B.3 — Chain D review ledger. One row per (drawing, element, value),
# stable across commits: a revision changing the value re-opens review, while
# reindexing the same state collides onto the existing row (status kept).
DIM_REVIEW_SCHEMA = """
CREATE TABLE IF NOT EXISTS dim_review (
  uid            TEXT PRIMARY KEY,
  drawing        TEXT,
  project        TEXT,
  detail_id      TEXT,
  element_id     TEXT,
  value          REAL,
  unit           TEXT,
  label          TEXT,
  anchor         TEXT,
  regions        TEXT,
  crop           TEXT,
  material       TEXT,
  region_id      TEXT,
  chain          TEXT,
  confidence     TEXT,
  status         TEXT DEFAULT 'pending',
  last_seen_commit TEXT,
  updated_at     TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_dimrev_drawing ON dim_review(drawing);
CREATE INDEX IF NOT EXISTS idx_dimrev_status ON dim_review(status);
"""

DETAIL_SCHEMA = """
CREATE TABLE IF NOT EXISTS detail (
  detail_id      TEXT,
  commit_sha     TEXT,
  commit_date    TEXT,
  author         TEXT,
  commit_message TEXT,
  drawing        TEXT,
  project        TEXT,
  detail_tag     TEXT,
  title          TEXT,
  scale          TEXT,
  segmentation   TEXT,
  entity_count   INTEGER,
  text_blob      TEXT,
  depends_on     TEXT,
  wet_area       INTEGER,
  record         TEXT,
  PRIMARY KEY (detail_id, commit_sha)
);
CREATE INDEX IF NOT EXISTS idx_detail_drawing ON detail(drawing);
CREATE INDEX IF NOT EXISTS idx_detail_tag ON detail(detail_tag);
"""


def _drawing_and_project(state_path: str, state_dir: str = "state") -> tuple[str, str]:
    """state/A/D-101.jsonl -> drawing 'A/D-101', project 'A'. Flat stays 'D-101'/''."""
    rel = Path(state_path)
    try:
        rel = rel.relative_to(state_dir)
    except ValueError:
        pass
    drawing = rel.with_suffix("").as_posix()
    project = drawing.split("/")[0] if "/" in drawing else ""
    return drawing, project


def _git(repo: Path, *args: str) -> str:
    # Decode as UTF-8 explicitly: state jsonl carries raw annotation strings
    # (em-dashes, diameter symbols), and the Windows locale codec (cp950)
    # cannot decode them — text=True would crash on any non-ASCII drawing.
    r = subprocess.run(["git", "-C", str(repo), *args],
                       capture_output=True, check=True)
    return r.stdout.decode("utf-8", errors="replace")


def _commits(repo: Path) -> list[dict]:
    try:
        out = _git(repo, "log", "--reverse", "--format=%H%x01%aI%x01%an%x01%s", "--date-order")
    except subprocess.CalledProcessError:
        return []
    commits = []
    for line in out.splitlines():
        if not line.strip():
            continue
        sha, date, author, msg = (line.split("\x01") + ["", "", ""])[:4]
        commits.append({"sha": sha, "date": date, "author": author, "message": msg})
    return commits


def _state_files_at(repo: Path, sha: str, state_dir: str = "state") -> list[str]:
    try:
        out = _git(repo, "ls-tree", "-r", "--name-only", sha, "--", state_dir + "/")
    except subprocess.CalledProcessError:
        return []
    return [l.strip() for l in out.splitlines()
            if l.strip().endswith(".jsonl")]


def _read_blob(repo: Path, sha: str, path: str) -> list[dict]:
    try:
        out = _git(repo, "show", f"{sha}:{path}")
    except subprocess.CalledProcessError:
        return []
    rows = []
    for line in out.splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _dxf_blob(repo: Path, sha: str, drawing: str, drawings_dir: str = "drawings"):
    """Raw DXF bytes for a drawing at a commit (None when never committed)."""
    for cand in (f"{drawings_dir}/{drawing}.dxf", f"{drawings_dir}/{Path(drawing).stem}.dxf"):
        try:
            r = subprocess.run(["git", "-C", str(repo), "show", f"{sha}:{cand}"],
                               capture_output=True, check=True)
            return r.stdout
        except subprocess.CalledProcessError:
            continue
    return None


def _status(curr: dict, prev: dict | None) -> str:
    if prev is None:
        return "new"
    # reuse Part-2 comparison so index agrees with extract-time display
    from .identity import compute_status
    try:
        return compute_status(curr, prev)
    except Exception:
        return "unchanged" if curr.get("fingerprint") == prev.get("fingerprint") else "value_changed"


def _default_configs():
    from .semantics import load_config_dir
    from .attribute import load_geometry_config
    try:
        cfg, _ = load_config_dir(Path(__file__).parent / "config")
    except Exception:
        cfg = {}
    return cfg, load_geometry_config(None)


def build_index(repo: Path, db_path: Path, state_dir: str = "state",
                materials_cfg: dict | None = None, geometry_cfg: dict | None = None,
                drawings_dir: str = "drawings", details_dir: str = "details",
                taxonomy_dir: str | None = None) -> dict:
    from .resolve import QUARANTINE_SCHEMA
    repo = Path(repo)
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    con.executescript(SCHEMA)
    con.executescript(ATTRIBUTION_SCHEMA)
    con.executescript(DETAIL_SCHEMA)
    con.executescript(QUARANTINE_SCHEMA)
    con.executescript(DIM_REVIEW_SCHEMA)
    try:
        con.execute(MIGRATE_PROJECT)
    except sqlite3.OperationalError:
        pass  # column already exists on re-run
    con.execute(IDX_PROJECT)
    cols = [r[1] for r in con.execute("PRAGMA table_info(element_state)").fetchall()]
    if "part" not in cols:
        # schema upgrade: part did not exist when old rows were indexed.
        # The index is derived — rebuild it whole rather than mixing.
        con.execute(MIGRATE_PART)
        con.execute("DELETE FROM element_state")
        con.execute("DELETE FROM attribution")
        con.execute("DELETE FROM indexed_commits")
    con.execute(IDX_PART)
    con.executescript(ATTRIBUTION_SCHEMA)
    try:
        con.execute(MIGRATE_ATTR_MEASURE)
    except sqlite3.OperationalError:
        pass  # column already exists on re-run
    con.execute("CREATE INDEX IF NOT EXISTS idx_attr_measure ON attribution(measure)")
    try:
        con.execute(MIGRATE_QUAR_MEASURE)
    except sqlite3.OperationalError:
        pass  # column already exists, or table fresh with it already
    if materials_cfg is None or geometry_cfg is None:
        _mcfg, _gcfg = _default_configs()
        materials_cfg = _mcfg if materials_cfg is None else materials_cfg
        geometry_cfg = _gcfg if geometry_cfg is None else geometry_cfg
    done = {r[0] for r in con.execute("SELECT commit_sha FROM indexed_commits")}
    commits = _commits(repo)
    # drop rows for commits no longer in history (rewrite/force-push safety)
    live = {c["sha"] for c in commits}
    if live:
        con.execute(f"DELETE FROM element_state WHERE commit_sha NOT IN ({','.join('?' for _ in live)})", tuple(live))
        con.execute(f"DELETE FROM attribution WHERE commit_sha NOT IN ({','.join('?' for _ in live)})", tuple(live))
        con.execute(f"DELETE FROM detail WHERE commit_sha NOT IN ({','.join('?' for _ in live)})", tuple(live))
        con.execute(f"DELETE FROM quarantine WHERE commit_sha NOT IN ({','.join('?' for _ in live)})", tuple(live))
        con.execute(f"DELETE FROM indexed_commits WHERE commit_sha NOT IN ({','.join('?' for _ in live)})", tuple(live))
    # previous snapshot per drawing for status diff (need full chain even if
    # some commits were already indexed, so walk all, insert only new)
    prev_by_drawing: dict[str, dict[str, dict]] = {}
    # preload previous snapshots for already-indexed prefix to keep statuses
    # correct without reinserting: walk in order, fill prev, skip insert if done
    inserted = 0
    processed = 0
    for c in commits:
        sha = c["sha"]
        files = _state_files_at(repo, sha, state_dir)
        # load current snapshots for status chaining regardless of indexed state
        cur_by_drawing: dict[str, dict[str, dict]] = {}
        for f in files:
            drawing, _proj = _drawing_and_project(f, state_dir)
            rows = _read_blob(repo, sha, f)
            cur_by_drawing[drawing] = {r.get("element_id"): r for r in rows if r.get("element_id")}
        if sha in done:
            prev_by_drawing = cur_by_drawing
            continue
        # insert this commit
        for drawing, cur in cur_by_drawing.items():
            project = drawing.split("/")[0] if "/" in drawing else ""
            prev = prev_by_drawing.get(drawing, {})
            # live rows
            for eid, rec in cur.items():
                st = _status(rec, prev.get(eid))
                parsed = rec.get("parsed") or {}
                con.execute(
                    "INSERT OR REPLACE INTO element_state "
                    "(element_id,commit_sha,commit_date,author,commit_message,drawing,project,"
                    " type,layer,material,part,value,unit,text_raw,x,y,status,match_tier,match_confidence)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (eid, sha, c["date"], c["author"], c["message"], drawing, project,
                     rec.get("type"), rec.get("layer"),
                     parsed.get("material"), parsed.get("part"), parsed.get("value"), parsed.get("unit"),
                     rec.get("text_raw"),
                     (rec.get("geom") or {}).get("x"), (rec.get("geom") or {}).get("y"),
                     st,
                     rec.get("match_tier") or ("new" if st == "new" else "committed"),
                     rec.get("match_confidence", 1.0)))
                inserted += 1
            # deleted rows: in prev but not cur — carry last values with deleted status
            for eid, prec in prev.items():
                if eid not in cur:
                    p = prec.get("parsed") or {}
                    con.execute(
                        "INSERT OR REPLACE INTO element_state "
                        "(element_id,commit_sha,commit_date,author,commit_message,drawing,project,"
                        " type,layer,material,part,value,unit,text_raw,x,y,status,match_tier,match_confidence)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (eid, sha, c["date"], c["author"], c["message"], drawing, project,
                         prec.get("type"), prec.get("layer"),
                         p.get("material"), p.get("part"), p.get("value"), p.get("unit"),
                         prec.get("text_raw"),
                         (prec.get("geom") or {}).get("x"), (prec.get("geom") or {}).get("y"),
                         "deleted", "committed", 1.0))
                    inserted += 1
        # drawings that vanished entirely: all prev ids -> deleted
        for drawing, prev in prev_by_drawing.items():
            if drawing not in cur_by_drawing:
                project = drawing.split("/")[0] if "/" in drawing else ""
                for eid, prec in prev.items():
                    p = prec.get("parsed") or {}
                    con.execute(
                        "INSERT OR REPLACE INTO element_state "
                        "(element_id,commit_sha,commit_date,author,commit_message,drawing,project,"
                        " type,layer,material,part,value,unit,text_raw,x,y,status,match_tier,match_confidence)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (eid, sha, c["date"], c["author"], c["message"], drawing, project,
                         prec.get("type"), prec.get("layer"),
                         p.get("material"), p.get("part"), p.get("value"), p.get("unit"),
                         prec.get("text_raw"),
                         (prec.get("geom") or {}).get("x"), (prec.get("geom") or {}).get("y"),
                         "deleted", "committed", 1.0))
                    inserted += 1
        con.execute("INSERT OR REPLACE INTO indexed_commits (commit_sha) VALUES (?)", (sha,))
        _index_attributions(con, repo, sha, c, cur_by_drawing, materials_cfg, geometry_cfg,
                            state_dir, drawings_dir)
        _index_details(con, repo, sha, c, cur_by_drawing, materials_cfg, geometry_cfg,
                       drawings_dir, details_dir, taxonomy_dir)
        prev_by_drawing = cur_by_drawing
        processed += 1
    # backfill: commits indexed before the attribution table existed
    missing = [r[0] for r in con.execute(
        "SELECT DISTINCT commit_sha FROM element_state WHERE commit_sha NOT IN "
        "(SELECT DISTINCT commit_sha FROM attribution)")]
    for sha in missing:
        cinfo = next((c for c in commits if c["sha"] == sha), None)
        if cinfo is None:
            continue
        cur_by_drawing = {}
        for f in _state_files_at(repo, sha, state_dir):
            drawing, _proj = _drawing_and_project(f, state_dir)
            rows = _read_blob(repo, sha, f)
            cur_by_drawing[drawing] = {r.get("element_id"): r for r in rows if r.get("element_id")}
        _index_attributions(con, repo, sha, cinfo, cur_by_drawing, materials_cfg, geometry_cfg,
                            state_dir, drawings_dir)
    # backfill: commits indexed before the detail table existed
    missing_d = [r[0] for r in con.execute(
        "SELECT DISTINCT commit_sha FROM element_state WHERE commit_sha NOT IN "
        "(SELECT DISTINCT commit_sha FROM detail)")]
    for sha in missing_d:
        cinfo = next((c for c in commits if c["sha"] == sha), None)
        if cinfo is None:
            continue
        cur_by_drawing = {}
        for f in _state_files_at(repo, sha, state_dir):
            drawing, _proj = _drawing_and_project(f, state_dir)
            rows = _read_blob(repo, sha, f)
            cur_by_drawing[drawing] = {r.get("element_id"): r for r in rows if r.get("element_id")}
        _index_details(con, repo, sha, cinfo, cur_by_drawing, materials_cfg, geometry_cfg,
                       drawings_dir, details_dir, taxonomy_dir)
    # backfill: commits indexed before quarantine existed (old rows predate
    # candidates; live-build them — statuses of existing rows are preserved).
    missing_q = [r[0] for r in con.execute(
        "SELECT DISTINCT commit_sha FROM element_state WHERE commit_sha NOT IN "
        "(SELECT DISTINCT commit_sha FROM quarantine)")]
    for sha in missing_q:
        cinfo = next((c for c in commits if c["sha"] == sha), None)
        if cinfo is None:
            continue
        cur_by_drawing = {}
        for f in _state_files_at(repo, sha, state_dir):
            drawing, _proj = _drawing_and_project(f, state_dir)
            rows = _read_blob(repo, sha, f)
            cur_by_drawing[drawing] = {r.get("element_id"): r for r in rows if r.get("element_id")}
        _index_quarantine_only(con, repo, sha, cinfo, cur_by_drawing,
                               materials_cfg, geometry_cfg,
                               drawings_dir, details_dir, taxonomy_dir)
    con.commit()
    total = con.execute("SELECT COUNT(*) FROM element_state").fetchone()[0]
    con.close()
    return {"commits_processed": processed, "rows_inserted": inserted, "total_rows": total}


def _details_blob(repo: Path, sha: str, drawing: str, details_dir: str = "details") -> dict | None:
    """Committed details/<drawing>.json at a commit (None when absent/invalid).

    The committed file wins over live segmentation: future human facet edits
    (7.7) must survive reindexing. Validated lightly — wrong-drawing or
    detail-less payloads fall back to live segmentation.
    """
    try:
        r = subprocess.run(["git", "-C", str(repo), "show", f"{sha}:{details_dir}/{drawing}.json"],
                           capture_output=True, check=True)
        payload = json.loads(r.stdout.decode("utf-8"))
    except (subprocess.CalledProcessError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("drawing") != drawing:
        return None
    if not isinstance(payload.get("details"), list):
        return None
    return payload


def _insert_detail_rows(con, sha: str, c: dict, drawing: str, project: str, payload: dict) -> int:
    n = 0
    for d in payload.get("details", []):
        if not isinstance(d, dict) or not d.get("detail_id"):
            continue
        depends = d.get("depends_on") or []
        try:
            con.execute(
                "INSERT OR REPLACE INTO detail "
                "(detail_id,commit_sha,commit_date,author,commit_message,drawing,project,"
                " detail_tag,title,scale,segmentation,entity_count,text_blob,depends_on,"
                " wet_area,record)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (d.get("detail_id"), sha, c.get("date"), c.get("author"), c.get("message"),
                 drawing, project, d.get("detail_tag"), d.get("title"), d.get("scale"),
                 d.get("segmentation") or payload.get("segmentation"),
                 d.get("entity_count", len(d.get("element_ids") or [])),
                 d.get("text_blob"), json.dumps(depends, ensure_ascii=False),
                 1 if (d.get("performance") or {}).get("wet_area") else 0,
                 json.dumps(d, sort_keys=True, ensure_ascii=False)))
            n += 1
        except Exception:
            continue
    return n


def _crop_exists(repo: Path, sha: str, relpath: str | None) -> str | None:
    """Committed crop/thumbnail check: a path recorded at extract time may be
    absent at this commit (older history). Returns the path or None."""
    import subprocess
    if not relpath:
        return None
    try:
        subprocess.run(["git", "-C", str(repo), "cat-file", "-e",
                        f"{sha}:{relpath}"],
                       check=True, capture_output=True)
        return relpath
    except (subprocess.CalledProcessError, OSError):
        return None


def _index_details(con, repo: Path, sha: str, c: dict,
                   cur_by_drawing: dict, materials_cfg: dict, geometry_cfg: dict,
                   drawings_dir: str, details_dir: str,
                   taxonomy_dir: str | None = None) -> int:
    """7.1/7.5: one detail-region row per drawing per commit.

    Prefers the committed details/*.json (human-editable facets survive);
    otherwise segments the committed state rows live (+ DXF blob for Chain A/C
    attribution when present, leader-only when absent). Quarantine candidates
    from either source become occurrence rows (8.7).
    """
    from .resolve import insert_occurrences
    from .segment import build_sheet_details
    n = 0
    for drawing, cur in cur_by_drawing.items():
        project = drawing.split("/")[0] if "/" in drawing else ""
        committed = _details_blob(repo, sha, drawing, details_dir)
        payload = None
        if committed is not None:
            n += _insert_detail_rows(con, sha, c, drawing, project, committed)
            payload = committed
        else:
            doc = None
            blob = _dxf_blob(repo, sha, drawing, drawings_dir)
            if blob:
                try:
                    import ezdxf
                    import tempfile
                    with tempfile.NamedTemporaryFile(suffix=".dxf", delete=False) as f:
                        f.write(blob)
                        tmp = f.name
                    try:
                        doc = ezdxf.readfile(tmp)
                    finally:
                        try:
                            Path(tmp).unlink()
                        except OSError:
                            pass
                except Exception:
                    doc = None
            try:
                payload = build_sheet_details(drawing, list(cur.values()), materials_cfg,
                                              geometry_cfg, doc,
                                              taxonomy_dir=taxonomy_dir)
            except Exception:
                continue
            n += _insert_detail_rows(con, sha, c, drawing, project, payload)
        if payload is not None:
            # verify crops against this commit (older history predates them)
            for cand in payload.get("quarantine_candidates", []) or []:
                cand["crop"] = _crop_exists(repo, sha, cand.get("crop"))
            try:
                insert_occurrences(con, payload, drawing, project, sha, c.get("date"))
            except Exception:
                continue
    return n


def _index_quarantine_only(con, repo: Path, sha: str, c: dict,
                           cur_by_drawing: dict, materials_cfg: dict,
                           geometry_cfg: dict, drawings_dir: str,
                           details_dir: str,
                           taxonomy_dir: str | None = None) -> int:
    """Quarantine backfill for commits indexed before candidates existed:
    same payload resolution as _index_details, occurrences only."""
    from .resolve import insert_occurrences
    from .segment import build_sheet_details
    n = 0
    for drawing, cur in cur_by_drawing.items():
        project = drawing.split("/")[0] if "/" in drawing else ""
        payload = _details_blob(repo, sha, drawing, details_dir)
        if payload is None:
            try:
                payload = build_sheet_details(drawing, list(cur.values()),
                                              materials_cfg, geometry_cfg, None,
                                              taxonomy_dir=taxonomy_dir)
            except Exception:
                continue
        for cand in payload.get("quarantine_candidates", []) or []:
            cand["crop"] = _crop_exists(repo, sha, cand.get("crop"))
        try:
            n += insert_occurrences(con, payload, drawing, project, sha, c.get("date"))
        except Exception:
            continue
    return n


def _confirmed_dim_overrides(con) -> dict:
    """dim_review confirmations keyed (element_id, value): a confirmed Chain D
    attribution promotes to medium/human wherever it re-indexes."""
    try:
        rows = con.execute(
            "SELECT element_id, value, material, region_id FROM dim_review "
            "WHERE status='confirmed'").fetchall()
    except sqlite3.OperationalError:
        return {}
    out = {}
    for r in rows:
        try:
            out[(r[0], round(float(r[1]), 2))] = {
                "material": r[2], "region_id": r[3]}
        except (TypeError, ValueError):
            continue
    return out


def _index_attributions(con, repo: Path, sha: str, c: dict,
                        cur_by_drawing: dict, materials_cfg: dict, geometry_cfg: dict,
                        state_dir: str, drawings_dir: str) -> int:
    """Part 7: bound material/value facts per commit.

    Committed state rows already carry element_ids; the DXF blob at the same
    commit supplies hatch boundaries and leader endpoints for Chains A/C.
    Without a DXF blob, leader-text (Chain B) attributions still index.
    Chain D runs disabled here (no adapter at index time): unresolvable
    dimensions land in dim_review as pending rows for the panel.
    """
    from .attribute import analyze_records, dim_review_uid
    n = 0
    confirmed = _confirmed_dim_overrides(con)
    for drawing, cur in cur_by_drawing.items():
        project = drawing.split("/")[0] if "/" in drawing else ""
        doc = None
        blob = _dxf_blob(repo, sha, drawing, drawings_dir)
        if blob:
            try:
                import ezdxf
                import tempfile
                with tempfile.NamedTemporaryFile(suffix=".dxf", delete=False) as f:
                    f.write(blob)
                    tmp = f.name
                try:
                    doc = ezdxf.readfile(tmp)
                finally:
                    try:
                        Path(tmp).unlink()
                    except OSError:
                        pass
            except Exception:
                doc = None
        try:
            summary = analyze_records(list(cur.values()), doc, materials_cfg, geometry_cfg,
                                      drawing=drawing)
        except Exception:
            continue
        for a in summary.get("attributions", []):
            # Measure-only facts (setdown/fall, no material word) index
            # under their measure (addendum A.5, test 84).
            if not a.get("element_id") or (not a.get("material")
                    and a.get("value") is None):
                continue
            conf, chain = a.get("confidence"), a.get("attribution_chain")
            try:
                key = (a.get("element_id"), round(float(a.get("value")), 2)) \
                    if a.get("value") is not None else None
            except (TypeError, ValueError):
                key = None
            ov = confirmed.get(key) if key else None
            if ov is not None and (chain or "").startswith("vision"):
                conf, chain = "medium", "vision+human"
            try:
                con.execute(
                    "INSERT OR REPLACE INTO attribution "
                    "(element_id,commit_sha,drawing,project,material,part,value,unit,"
                    " qualifier,measure,confidence,chain,conflict)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (a.get("element_id"), sha, drawing, project,
                     a.get("material"), a.get("part"), a.get("value"), a.get("unit"),
                     a.get("qualifier"), a.get("measure") or "thickness",
                     conf, chain,
                     1 if a.get("conflict") else 0))
                n += 1
            except Exception:
                continue
        for u in summary.get("unattributed", []):
            if not u.get("element_id"):
                continue
            try:
                con.execute(
                    "INSERT OR REPLACE INTO attribution "
                    "(element_id,commit_sha,drawing,project,material,part,value,unit,"
                    " qualifier,measure,confidence,chain,conflict)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (u.get("element_id"), sha, drawing, project,
                     None, None, u.get("raw_value"), u.get("unit"),
                     None, "thickness", None, "unattributed", 0))
                n += 1
            except Exception:
                continue
        for rv in summary.get("dim_review", []) or []:
            uid = rv.get("uid") or dim_review_uid(
                drawing, rv.get("element_id"), rv.get("value") or 0)
            try:
                import json as _json
                crop = _crop_exists(repo, sha, rv.get("crop"))
                con.execute(
                    "INSERT INTO dim_review (uid,drawing,project,detail_id,"
                    " element_id,value,unit,label,anchor,regions,crop,material,"
                    " region_id,chain,confidence,status,last_seen_commit)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(uid) DO UPDATE SET last_seen_commit=excluded.last_seen_commit,"
                    " crop=COALESCE(excluded.crop, dim_review.crop)",
                    (uid, drawing, project, rv.get("detail_id"),
                     rv.get("element_id"), rv.get("value"), rv.get("unit"),
                     rv.get("label"),
                     _json.dumps(rv.get("anchor") or []),
                     _json.dumps(rv.get("regions") or []),
                     crop, rv.get("material"), rv.get("region_id"),
                     rv.get("chain", "vision"), rv.get("confidence", "low"),
                     "pending", sha))
                n += 1
            except Exception:
                continue
    return n
