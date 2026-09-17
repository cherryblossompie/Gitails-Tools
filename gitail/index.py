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
  confidence     TEXT,
  chain          TEXT,
  conflict       INTEGER,
  UNIQUE(element_id, commit_sha, material, value)
);
CREATE INDEX IF NOT EXISTS idx_attr_matval ON attribution(material, value);
CREATE INDEX IF NOT EXISTS idx_attr_draw ON attribution(drawing);
CREATE INDEX IF NOT EXISTS idx_attr_conf ON attribution(confidence);
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
    r = subprocess.run(["git", "-C", str(repo), *args],
                       capture_output=True, text=True, check=True)
    return r.stdout


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
                drawings_dir: str = "drawings") -> dict:
    repo = Path(repo)
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    con.executescript(SCHEMA)
    con.executescript(ATTRIBUTION_SCHEMA)
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
    con.commit()
    total = con.execute("SELECT COUNT(*) FROM element_state").fetchone()[0]
    con.close()
    return {"commits_processed": processed, "rows_inserted": inserted, "total_rows": total}


def _index_attributions(con, repo: Path, sha: str, c: dict,
                        cur_by_drawing: dict, materials_cfg: dict, geometry_cfg: dict,
                        state_dir: str, drawings_dir: str) -> int:
    """Part 7: bound material/value facts per commit.

    Committed state rows already carry element_ids; the DXF blob at the same
    commit supplies hatch boundaries and leader endpoints for Chains A/C.
    Without a DXF blob, leader-text (Chain B) attributions still index.
    """
    from .attribute import analyze_records
    n = 0
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
            summary = analyze_records(list(cur.values()), doc, materials_cfg, geometry_cfg)
        except Exception:
            continue
        for a in summary.get("attributions", []):
            if not a.get("element_id") or not a.get("material"):
                continue
            try:
                con.execute(
                    "INSERT OR REPLACE INTO attribution "
                    "(element_id,commit_sha,drawing,project,material,part,value,unit,"
                    " qualifier,confidence,chain,conflict)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (a.get("element_id"), sha, drawing, project,
                     a.get("material"), a.get("part"), a.get("value"), a.get("unit"),
                     a.get("qualifier"), a.get("confidence"), a.get("attribution_chain"),
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
                    " qualifier,confidence,chain,conflict)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (u.get("element_id"), sha, drawing, project,
                     None, None, u.get("raw_value"), u.get("unit"),
                     None, None, "unattributed", 0))
                n += 1
            except Exception:
                continue
    return n
