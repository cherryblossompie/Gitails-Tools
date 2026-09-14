"""Part 5 — Query interface over index.sqlite (read-only, stdlib sqlite3)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

COLS = ["element_id", "commit_sha", "commit_date", "author", "commit_message",
        "drawing", "type", "layer", "material", "value", "unit",
        "text_raw", "x", "y", "status", "match_tier", "match_confidence"]


def _con(db: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    return con


def _latest_shas(con) -> list[str]:
    # Latest commit per drawing = greatest rowid (inserts walk git log oldest->newest).
    # Date ordering is unreliable when commits share a timestamp.
    latest: list[str] = []
    for d in con.execute("SELECT DISTINCT drawing FROM element_state").fetchall():
        r = con.execute("SELECT commit_sha FROM element_state WHERE drawing=? ORDER BY rowid DESC LIMIT 1",
                        (d["drawing"],)).fetchone()
        if r:
            latest.append(r["commit_sha"])
    return latest


def find(db: Path, material=None, value=None, text=None, drawing=None, ever: bool = False) -> list[dict]:
    con = _con(db)
    q = f"SELECT {','.join(COLS)} FROM element_state WHERE 1=1"
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
    return rows


def history(db: Path, element_id: str) -> list[dict]:
    """One row per commit where the element's state differed from before."""
    con = _con(db)
    rows = [dict(r) for r in con.execute(
        f"SELECT {','.join(COLS)} FROM element_state WHERE element_id=? ORDER BY commit_date",
        (element_id,))]
    con.close()
    out = []
    prev = None
    for r in rows:
        if prev is None or (r["material"], r["value"], r["unit"], r["text_raw"],
                             r["x"], r["y"], r["status"]) != \
                            (prev["material"], prev["value"], prev["unit"], prev["text_raw"],
                             prev["x"], prev["y"], prev["status"]) or r["status"] in ("new", "deleted"):
            # first sighting + every real transition (skip pure unchanged repeats)
            if prev is None or r["status"] != "unchanged" or r["status"] != prev["status"]:
                out.append(r)
        prev = r
    # collapse consecutive unchanged duplicates, keep transitions
    filt = [r for r in out if not (r["status"] == "unchanged" and out.index(r) > 0)]
    # simpler: return rows whose status != unchanged plus the first row
    res = [rows[0]] + [r for r in rows[1:] if r["status"] != "unchanged"] if rows else []
    return res


def at(db: Path, commit: str, drawing: str) -> list[dict]:
    con = _con(db)
    # resolve short sha
    row = con.execute("SELECT commit_sha FROM element_state WHERE commit_sha LIKE ? LIMIT 1",
                      (commit + "%",)).fetchone()
    sha = row["commit_sha"] if row else commit
    rows = [dict(r) for r in con.execute(
        f"SELECT {','.join(COLS)} FROM element_state WHERE commit_sha=? AND drawing=? AND status!='deleted'",
        (sha, drawing))]
    con.close()
    return rows


def changed(db: Path, from_sha: str, to_sha: str) -> list[dict]:
    """Elements whose state differs between two commits (before/after values)."""
    con = _con(db)
    def snap(prefix: str) -> dict:
        r = con.execute("SELECT commit_sha FROM element_state WHERE commit_sha LIKE ? LIMIT 1",
                        (prefix + "%",)).fetchone()
        sha = r["commit_sha"] if r else prefix
        rows = con.execute(f"SELECT {','.join(COLS)} FROM element_state WHERE commit_sha=?",
                           (sha,)).fetchall()
        # for HEAD-like alias: if no match, fall back to latest per drawing
        if not rows and prefix.upper() in ("HEAD", "LATEST"):
            latest = _latest_shas(con)
            out = {}
            for s in latest:
                for x in con.execute(f"SELECT {','.join(COLS)} FROM element_state WHERE commit_sha=?", (s,)):
                    out[(x["drawing"], x["element_id"])] = dict(x)
            return out
        return {(x["drawing"], x["element_id"]): dict(x) for x in rows}
    a, b = snap(from_sha), snap(to_sha)
    keys = set(a) | set(b)
    out = []
    for k in sorted(keys):
        ra, rb = a.get(k), b.get(k)
        if ra is None:
            out.append({"drawing": k[0], "element_id": k[1], "change": "added",
                        "before": None, "after": rb})
        elif rb is None or rb.get("status") == "deleted":
            out.append({"drawing": k[0], "element_id": k[1], "change": "deleted",
                        "before": ra, "after": rb})
        elif (ra.get("material"), ra.get("value"), ra.get("unit"), ra.get("text_raw"),
              ra.get("x"), ra.get("y")) != \
             (rb.get("material"), rb.get("value"), rb.get("unit"), rb.get("text_raw"),
              rb.get("x"), rb.get("y")):
            out.append({"drawing": k[0], "element_id": k[1], "change": rb.get("status", "changed"),
                        "before": ra, "after": rb})
    con.close()
    return out
