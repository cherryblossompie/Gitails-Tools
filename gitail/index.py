"""Part 4 — Index: git history of state/*.jsonl -> SQLite.

Derived artifact, never committed. Rebuildable from Git at any time.
Incremental: tracks indexed_commits, processes only new commits on re-run.
Status is computed by element_id diff between consecutive commits of the
same drawing (trusts committed IDs from the idmap workflow).
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


def _status(curr: dict, prev: dict | None) -> str:
    if prev is None:
        return "new"
    # reuse Part-2 comparison so index agrees with extract-time display
    from .identity import compute_status
    try:
        return compute_status(curr, prev)
    except Exception:
        return "unchanged" if curr.get("fingerprint") == prev.get("fingerprint") else "value_changed"


def build_index(repo: Path, db_path: Path, state_dir: str = "state") -> dict:
    repo = Path(repo)
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    con.executescript(SCHEMA)
    try:
        con.execute(MIGRATE_PROJECT)
    except sqlite3.OperationalError:
        pass  # column already exists on re-run
    con.execute(IDX_PROJECT)
    done = {r[0] for r in con.execute("SELECT commit_sha FROM indexed_commits")}
    commits = _commits(repo)
    # drop rows for commits no longer in history (rewrite/force-push safety)
    live = {c["sha"] for c in commits}
    if live:
        con.execute(f"DELETE FROM element_state WHERE commit_sha NOT IN ({','.join('?' for _ in live)})", tuple(live))
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
                    " type,layer,material,value,unit,text_raw,x,y,status,match_tier,match_confidence)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (eid, sha, c["date"], c["author"], c["message"], drawing, project,
                     rec.get("type"), rec.get("layer"),
                     parsed.get("material"), parsed.get("value"), parsed.get("unit"),
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
                        " type,layer,material,value,unit,text_raw,x,y,status,match_tier,match_confidence)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (eid, sha, c["date"], c["author"], c["message"], drawing, project,
                         prec.get("type"), prec.get("layer"),
                         p.get("material"), p.get("value"), p.get("unit"),
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
                        " type,layer,material,value,unit,text_raw,x,y,status,match_tier,match_confidence)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (eid, sha, c["date"], c["author"], c["message"], drawing, project,
                         prec.get("type"), prec.get("layer"),
                         p.get("material"), p.get("value"), p.get("unit"),
                         prec.get("text_raw"),
                         (prec.get("geom") or {}).get("x"), (prec.get("geom") or {}).get("y"),
                         "deleted", "committed", 1.0))
                    inserted += 1
        con.execute("INSERT OR REPLACE INTO indexed_commits (commit_sha) VALUES (?)", (sha,))
        prev_by_drawing = cur_by_drawing
        processed += 1
    con.commit()
    total = con.execute("SELECT COUNT(*) FROM element_state").fetchone()[0]
    con.close()
    return {"commits_processed": processed, "rows_inserted": inserted, "total_rows": total}
