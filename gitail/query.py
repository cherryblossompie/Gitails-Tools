"""Part 5 — Query interface over index.sqlite (read-only, stdlib sqlite3)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

COLS = ["element_id", "commit_sha", "commit_date", "author", "commit_message",
        "drawing", "project", "type", "layer", "material", "value", "unit",
        "text_raw", "x", "y", "status", "match_tier", "match_confidence"]


def _con(db: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    return con


def _cols(con) -> list[str]:
    return [r[1] for r in con.execute("PRAGMA table_info(element_state)").fetchall()]


def _select(con) -> str:
    have = set(_cols(con))
    use = [c for c in COLS if c in have]
    return ",".join(use)


def _project_of(row: dict) -> str:
    if row.get("project"):
        return row["project"]
    d = row.get("drawing") or ""
    return d.split("/")[0] if "/" in d else ""


def _latest_shas(con) -> list[str]:
    # Latest commit per drawing = greatest rowid (inserts walk git log oldest->newest).
    latest: list[str] = []
    for d in con.execute("SELECT DISTINCT drawing FROM element_state").fetchall():
        r = con.execute("SELECT commit_sha FROM element_state WHERE drawing=? ORDER BY rowid DESC LIMIT 1",
                        (d["drawing"],)).fetchone()
        if r:
            latest.append(r["commit_sha"])
    return latest


def distinct(db: Path, col: str, limit: int = 500) -> list[str]:
    """Distinct values for autocomplete datalists."""
    if col not in ("material", "drawing", "project", "text_raw", "value"):
        raise ValueError(col)
    con = _con(db)
    if col == "project" and "project" not in _cols(con):
        vals = sorted({(r["drawing"] or "").split("/")[0] for r in
                       con.execute("SELECT DISTINCT drawing FROM element_state")
                       if "/" in (r["drawing"] or "")})
        con.close()
        return vals
    if col == "text_raw":
        rows = con.execute("SELECT DISTINCT text_raw FROM element_state WHERE text_raw IS NOT NULL LIMIT ?", (limit,)).fetchall()
    elif col == "value":
        rows = con.execute("SELECT DISTINCT value FROM element_state WHERE value IS NOT NULL ORDER BY value LIMIT ?", (limit,)).fetchall()
    else:
        rows = con.execute(f"SELECT DISTINCT {col} FROM element_state WHERE {col} IS NOT NULL AND {col}!='' ORDER BY {col} LIMIT ?", (limit,)).fetchall()
    out = [str(r[0]) for r in rows if r[0] not in (None, "")]
    con.close()
    return out


def find(db: Path, material=None, value=None, text=None, drawing=None, project=None, ever: bool = False) -> list[dict]:
    con = _con(db)
    sel = _select(con)
    have_project = "project" in _cols(con)
    q = f"SELECT {sel} FROM element_state WHERE 1=1"
    args: list = []
    if material:
        q += " AND LOWER(material)=LOWER(?)"
        args.append(material)
    if value is not None:
        q += " AND value=?"
        args.append(value)
    if text:
        q += " AND LOWER(COALESCE(text_raw,'')) LIKE '%' || LOWER(?) || '%'"
        args.append(text)
    if drawing:
        q += " AND drawing=?"
        args.append(drawing)
    if project:
        if have_project:
            q += " AND project=?"
            args.append(project)
        else:
            q += " AND drawing LIKE ?"
            args.append(f"{project}/%")
    if not ever:
        latest = _latest_shas(con)
        if not latest:
            con.close()
            return []
        q += f" AND commit_sha IN ({','.join('?' for _ in latest)}) AND status!='deleted'"
        args.extend(latest)
    q += " ORDER BY drawing, commit_date DESC, element_id"
    rows = [dict(r) for r in con.execute(q, args)]
    con.close()
    for r in rows:
        r.setdefault("project", _project_of(r))
    return rows


def history(db: Path, element_id: str) -> list[dict]:
    """One row per commit where the element's state differed from before."""
    con = _con(db)
    sel = _select(con)
    rows = [dict(r) for r in con.execute(
        f"SELECT {sel} FROM element_state WHERE element_id=? ORDER BY rowid",
        (element_id,))]
    con.close()
    for r in rows:
        r.setdefault("project", _project_of(r))
    res = [rows[0]] + [r for r in rows[1:] if r["status"] != "unchanged"] if rows else []
    return res


def at(db: Path, commit: str, drawing: str) -> list[dict]:
    con = _con(db)
    sel = _select(con)
    row = con.execute("SELECT commit_sha FROM element_state WHERE commit_sha LIKE ? LIMIT 1",
                      (commit + "%",)).fetchone()
    sha = row["commit_sha"] if row else commit
    rows = [dict(r) for r in con.execute(
        f"SELECT {sel} FROM element_state WHERE commit_sha=? AND drawing=? AND status!='deleted'",
        (sha, drawing))]
    con.close()
    for r in rows:
        r.setdefault("project", _project_of(r))
    return rows


def changed(db: Path, from_sha: str, to_sha: str) -> list[dict]:
    """Elements whose state differs between two commits (before/after values)."""
    con = _con(db)
    sel = _select(con)

    def snap(prefix: str) -> dict:
        r = con.execute("SELECT commit_sha FROM element_state WHERE commit_sha LIKE ? LIMIT 1",
                        (prefix + "%",)).fetchone()
        sha = r["commit_sha"] if r else prefix
        rows = con.execute(f"SELECT {sel} FROM element_state WHERE commit_sha=?",
                           (sha,)).fetchall()
        if not rows and prefix.upper() in ("HEAD", "LATEST"):
            latest = _latest_shas(con)
            out = {}
            for s in latest:
                for x in con.execute(f"SELECT {sel} FROM element_state WHERE commit_sha=?", (s,)):
                    out[(x["drawing"], x["element_id"])] = dict(x)
            return out
        return {(x["drawing"], x["element_id"]): dict(x) for x in rows}
    a, b = snap(from_sha), snap(to_sha)
    keys = set(a) | set(b)
    out = []
    for k in sorted(keys):
        ra, rb = a.get(k), b.get(k)
        proj = (rb or ra or {}).get("project") or (k[0].split("/")[0] if "/" in k[0] else "")
        if ra is None:
            out.append({"drawing": k[0], "project": proj, "element_id": k[1], "change": "added",
                        "before": None, "after": rb})
        elif rb is None or rb.get("status") == "deleted":
            out.append({"drawing": k[0], "project": proj, "element_id": k[1], "change": "deleted",
                        "before": ra, "after": rb})
        elif (ra.get("material"), ra.get("value"), ra.get("unit"), ra.get("text_raw"),
              ra.get("x"), ra.get("y")) != \
             (rb.get("material"), rb.get("value"), rb.get("unit"), rb.get("text_raw"),
              rb.get("x"), rb.get("y")):
            out.append({"drawing": k[0], "project": proj, "element_id": k[1], "change": rb.get("status", "changed"),
                        "before": ra, "after": rb})
    con.close()
    return out
