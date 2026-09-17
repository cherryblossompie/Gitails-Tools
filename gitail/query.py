"""Part 5 — Query interface over index.sqlite (read-only, stdlib sqlite3)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

COLS = ["element_id", "commit_sha", "commit_date", "author", "commit_message",
        "drawing", "project", "type", "layer", "material", "part", "value", "unit",
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


# Correlated "current snapshot" predicate: the row's commit is its own
# drawing's latest. A global IN-list is WRONG here — every commit snapshots
# every drawing, so another drawing's latest SHA also carries this drawing's
# rows and would duplicate them.
_LATEST_PER_DRAWING = (
    "commit_sha=(SELECT commit_sha FROM element_state AS _l "
    "WHERE _l.drawing=element_state.drawing ORDER BY _l.rowid DESC LIMIT 1)"
)


def distinct(db: Path, col: str, limit: int = 500) -> list[str]:
    """Distinct values for autocomplete datalists."""
    if col not in ("material", "part", "drawing", "project", "text_raw", "value"):
        raise ValueError(col)
    con = _con(db)
    if col in ("project", "part") and col not in _cols(con):
        con.close()
        if col == "project":
            con = _con(db)
            vals = sorted({(r["drawing"] or "").split("/")[0] for r in
                           con.execute("SELECT DISTINCT drawing FROM element_state")
                           if "/" in (r["drawing"] or "")})
            con.close()
            return vals
        return []
    if col == "text_raw":
        rows = con.execute("SELECT DISTINCT text_raw FROM element_state WHERE text_raw IS NOT NULL LIMIT ?", (limit,)).fetchall()
    elif col == "value":
        rows = con.execute("SELECT DISTINCT value FROM element_state WHERE value IS NOT NULL ORDER BY value LIMIT ?", (limit,)).fetchall()
    else:
        rows = con.execute(f"SELECT DISTINCT {col} FROM element_state WHERE {col} IS NOT NULL AND {col}!='' ORDER BY {col} LIMIT ?", (limit,)).fetchall()
    out = [str(r[0]) for r in rows if r[0] not in (None, "")]
    con.close()
    return out


CHIP_KINDS = ("material", "part", "project", "drawing", "value", "text")


def _chip_condition(kind: str, alias: str, have_project: bool) -> str:
    """SQL predicate on one element_state row (value bound separately)."""
    if kind == "material":
        return f"LOWER({alias}.material)=LOWER(?)"
    if kind == "part":
        return f"LOWER({alias}.part)=LOWER(?)"
    if kind == "project":
        if have_project:
            return f"LOWER({alias}.project)=LOWER(?)"
        return f"LOWER({alias}.drawing) LIKE LOWER(?) || '/%'"
    if kind == "drawing":
        return f"LOWER({alias}.drawing)=LOWER(?)"
    if kind == "value":
        return f"{alias}.value=?"
    if kind == "text":
        return f"LOWER(COALESCE({alias}.text_raw,'')) LIKE '%' || LOWER(?) || '%'"
    raise ValueError(f"unknown chip kind: {kind}")


def _row_matches(row: dict, kind: str, value: str) -> bool:
    """Python-side chip test for highlight flags (mirrors _chip_condition)."""
    v = (value or "").lower()
    if kind == "material":
        return (row.get("material") or "").lower() == v
    if kind == "part":
        return (row.get("part") or "").lower() == v
    if kind == "project":
        return _project_of(row).lower() == v
    if kind == "drawing":
        return (row.get("drawing") or "").lower() == v
    if kind == "value":
        return str(row.get("value") if row.get("value") is not None else "") == value
    if kind == "text":
        return v in (row.get("text_raw") or "").lower()
    return False


def parse_chip(s: str) -> tuple[str, str]:
    """'material:concrete' -> ('material', 'concrete'). Bare text -> ('text', s)."""
    if ":" in s:
        kind, _, val = s.partition(":")
        kind, val = kind.strip().lower(), val.strip()
        if kind in CHIP_KINDS and val:
            return kind, val
    if s.strip():
        return "text", s.strip()
    raise ValueError(f"empty chip: {s!r}")


def _attribution_map(con, pairs: list[tuple[str, str]]) -> dict:
    """Attribution rows keyed (element_id, commit_sha), chunked for SQLite limits."""
    out: dict = {}
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "attribution" not in names:
        return out
    cols = ["element_id", "commit_sha", "drawing", "material", "part", "value",
            "unit", "qualifier", "confidence", "chain", "conflict"]
    pairs = [(e, c) for e, c in pairs if e and c]
    for i in range(0, len(pairs), 400):
        chunk = pairs[i:i + 400]
        q = (f"SELECT {','.join(cols)} FROM attribution WHERE "
             + " OR ".join(["(element_id=? AND commit_sha=?)"] * len(chunk)))
        args = [x for p in chunk for x in p]
        for r in con.execute(q, args):
            d = dict(r)
            out.setdefault((d["element_id"], d["commit_sha"]), []).append(d)
    return out


_CONF_RANK = {"high": 0, "medium": 1, "low": 2}


def _attrib_satisfies(a: dict, kind: str, value: str, tolerance: float,
                      include_low: bool, include_unattr: bool) -> bool:
    """Does one attribution row satisfy a material/value chip (7.8)?"""
    conf_ok = (a.get("confidence") in ("high", "medium")
               or (include_low and a.get("confidence") == "low"))
    if kind == "material":
        return (a.get("material") or "").lower() == (value or "").lower() and conf_ok
    if kind == "value":
        try:
            num = float(value)
        except (TypeError, ValueError):
            return False
        v = a.get("value")
        if v is None or abs(float(v) - num) > tolerance:
            return False
        if a.get("material") is None:  # loose unattributed number
            return bool(include_unattr)
        return conf_ok
    return False


def _best_attribution(attribs: list, chips: list, tolerance: float,
                      include_low: bool, include_unattr: bool):
    """The attribution to display for a row: prefer chip-satisfying, then rank."""
    cands = [a for a in attribs
             for (k, v) in chips if k in ("material", "value")
             if _attrib_satisfies(a, k, v, tolerance, include_low, include_unattr)]
    pool = cands or attribs
    if not pool:
        return None
    return min(pool, key=lambda a: (_CONF_RANK.get(a.get("confidence"), 3),
                                    a.get("element_id") or ""))


def _chip_hit(row: dict, attribs: list, kind: str, value: str, tolerance: float,
              include_low: bool, include_unattr: bool) -> bool:
    if kind in ("material", "value"):
        if attribs:
            return any(_attrib_satisfies(a, kind, value, tolerance, include_low, include_unattr)
                       for a in attribs)
        # pre-attribution index (no rows for this element): row fields
        return _row_matches(row, kind, value)
    return _row_matches(row, kind, value)


CONF_OK = ("high", "medium")


def _conf_list(include_low: bool) -> str:
    return "('high','medium','low')" if include_low else "('high','medium')"


def _attr_latest(alias_outer: str = "e") -> str:
    """Latest attribution commit for the outer row's drawing."""
    return (f"(SELECT commit_sha FROM attribution AS _a WHERE _a.drawing={alias_outer}.drawing"
            " ORDER BY _a.rowid DESC LIMIT 1)")


def _attr_exists(kind: str, val: str, current_only: bool, tolerance: float,
                 include_low: bool, include_unattr: bool, outer: str = "e") -> tuple[str, list]:
    """EXISTS attribution for a material/value chip (7.8 bound-only).

    A material-plus-value criterion matches only values BOUND to that material
    at medium+ confidence — never loose numbers pooled at drawing level.
    """
    conf = _conf_list(include_low)
    if kind == "material":
        core = f"a2.drawing={outer}.drawing AND LOWER(a2.material)=LOWER(?) AND a2.confidence IN {conf}"
        args = [val]
    elif kind == "value":
        try:
            num = float(val)
        except (TypeError, ValueError):
            return "1=0", []
        lo, hi = num - tolerance, num + tolerance
        core = (f"a2.drawing={outer}.drawing AND a2.value BETWEEN ? AND ?"
                f" AND a2.confidence IN {conf}")
        args = [lo, hi]
        if include_unattr:
            core = (f"a2.drawing={outer}.drawing AND ((a2.value BETWEEN ? AND ?"
                    f" AND a2.confidence IN {conf})"
                    " OR (a2.material IS NULL AND a2.value BETWEEN ? AND ?"
                    " AND a2.chain='unattributed'))")
            args = [lo, hi, lo, hi]
    else:
        raise ValueError(kind)
    if current_only:
        core += f" AND a2.commit_sha={_attr_latest(outer)}"
    return f"EXISTS (SELECT 1 FROM attribution a2 WHERE {core})", args

def search_stacked(db: Path, chips: list[tuple[str, str]], ever: bool = False,
                   tolerance: float = 0.0, include_low: bool = False,
                   include_unattr: bool = False) -> dict:
    """Stacked single-bar search. Every chip is a must-contain condition on the
    DRAWING (AND). Returns {'drawings': [...], 'rows': [...]} where rows are the
    WHOLE current element sets of qualifying drawings; matched rows carry
    matched=True for highlight. Drawings qualifying only via history carry
    via_history=True.
    """
    for kind, _ in chips:
        if kind not in CHIP_KINDS:
            raise ValueError(f"unknown chip kind: {kind}")
    con = _con(db)
    sel = _select(con)
    have_project = "project" in _cols(con)
    have_part = "part" in _cols(con)
    args: list = []


    # qualifying drawings: every chip satisfied (current, or any history if ever).
    # material/value chips match BOUND attributions (7.8); the rest match rows.
    def exists_for(kind: str, val: str, current_only: bool) -> str:
        if kind in ("material", "value"):
            ex, a = _attr_exists(kind, val, current_only, tolerance,
                                 include_low, include_unattr)
            return ex, a
        if kind == "part" and not have_part:
            # pre-part index: fall back to annotation substring
            cond = "LOWER(COALESCE(e2.text_raw,'')) LIKE '%' || LOWER(?) || '%'"
        else:
            cond = _chip_condition(kind, "e2", have_project)
        q = ("EXISTS (SELECT 1 FROM element_state e2 WHERE e2.drawing=e.drawing "
             f"AND {cond}")
        a = [val]
        if current_only:
            q += (" AND e2.commit_sha=(SELECT commit_sha FROM element_state "
                  "WHERE drawing=e.drawing ORDER BY rowid DESC LIMIT 1)"
                  " AND e2.status!='deleted'")
        return q + ")", a

    q = "SELECT DISTINCT e.drawing FROM element_state e WHERE 1=1"
    for kind, val in chips:
        ex, a = exists_for(kind, val, current_only=not ever)
        q += f" AND {ex}"
        args.extend(a)
    drawings = sorted(r[0] for r in con.execute(q, args))

    # whole current rows of qualifying drawings (or all drawings when no chips)
    rows: list[dict] = []
    if drawings or not chips:
        rq = f"SELECT {sel} FROM element_state WHERE {_LATEST_PER_DRAWING} AND status!='deleted'"
        rq_args: list = []
        if drawings:
            rq += f" AND drawing IN ({','.join('?' for _ in drawings)})"
            rq_args.extend(drawings)
        rq += " ORDER BY drawing, element_id"
        rows = [dict(r) for r in con.execute(rq, rq_args)]
        amap = _attribution_map(con, [(r.get("element_id"), r.get("commit_sha")) for r in rows])
        for r in rows:
            r.setdefault("project", _project_of(r))
            attribs = amap.get((r.get("element_id"), r.get("commit_sha")), [])
            if chips:
                r["matched"] = any(
                    _chip_hit(r, attribs, k, v, tolerance, include_low, include_unattr)
                    for (k, v) in chips)
            else:
                r["matched"] = True
            best = _best_attribution(attribs, chips, tolerance, include_low, include_unattr)
            if best is not None:
                r["confidence"] = best.get("confidence")
                r["attribution_chain"] = best.get("chain")
                r["exact_value"] = best.get("value")
                if best.get("qualifier") is not None:
                    r["qualifier"] = best.get("qualifier")
                if best.get("qualifier") is not None:
                    r["qualifier"] = best.get("qualifier")
    by_drawing: dict[str, list[dict]] = {}
    for r in rows:
        by_drawing.setdefault(r["drawing"], []).append(r)
    # latest-commit deletions per qualifying drawing — shown struck-through so
    # removed elements never silently vanish (e.g. unsupported-entity uploads).
    # Drawings surviving ONLY as deletions still list (deleted_only) when they
    # match the chips (or unfiltered), so nothing disappears without a trace.
    deleted: list[dict] = []
    del_by_drawing: dict[str, list[dict]] = {}
    dzq = (f"SELECT {sel} FROM element_state WHERE status='deleted' AND {_LATEST_PER_DRAWING}"
           " ORDER BY drawing, element_id")
    # per drawing, note whether any deleted row matches (for del-only inclusion)
    seen_match: dict[str, bool] = {}
    dz = [dict(x) for x in con.execute(dzq)]
    dzmap = _attribution_map(con, [(r.get("element_id"), r.get("commit_sha")) for r in dz])
    for r in dz:
        r.setdefault("project", _project_of(r))
        attribs = dzmap.get((r.get("element_id"), r.get("commit_sha")), [])
        if chips:
            r["matched"] = any(
                _chip_hit(r, attribs, k, v, tolerance, include_low, include_unattr)
                for (k, v) in chips)
        else:
            r["matched"] = True
        if r["matched"]:
            seen_match[r["drawing"]] = True
        del_by_drawing.setdefault(r["drawing"], []).append(r)
    for d in sorted(del_by_drawing):
        # keep the drawing's FULL deleted set when it is listed live, when
        # unfiltered, or when at least one deletion matches the chips
        if d in drawings or not chips or seen_match.get(d):
            deleted.extend(del_by_drawing[d])
    info = []
    listed = set()
    for d in (drawings or sorted(by_drawing)):
        dr = by_drawing.get(d, [])
        info.append({"drawing": d,
                     "project": dr[0].get("project", "") if dr else (d.split("/")[0] if "/" in d else ""),
                     "elements": len(dr),
                     "matched": sum(1 for x in dr if x.get("matched")),
                     "via_history": bool(ever and chips and not any(x.get("matched") for x in dr))})
        listed.add(d)
    for d in sorted(del_by_drawing):
        if d in listed:
            continue
        dd = del_by_drawing[d]
        if chips and not any(x.get("matched") for x in dd):
            continue
        info.append({"drawing": d, "project": dd[0].get("project", ""),
                     "elements": 0, "matched": 0, "deleted_only": True, "via_history": False})
    info.sort(key=lambda e: e["drawing"])
    con.close()
    return {"drawings": info, "rows": rows, "deleted": deleted}


def find(db: Path, material=None, part=None, value=None, text=None, drawing=None, project=None,
         ever: bool = False, tolerance: float = 0.0,
         include_unattr: bool = False, include_low_confidence: bool = False) -> list[dict]:
    """Element search. Material/value criteria match only BOUND attributions
    (7.8): the value must be bound to the material at medium+ confidence —
    loose numbers never match. Tolerance widens numeric matching (±mm).
    """
    con = _con(db)
    sel = _select(con)
    have_project = "project" in _cols(con)
    have_part = "part" in _cols(con)
    q = f"SELECT {sel} FROM element_state WHERE 1=1"
    args: list = []
    if material or value is not None:
        conf = _conf_list(include_low_confidence)
        conds, cargs = [], []
        if material:
            conds.append(f"LOWER(a1.material)=LOWER(?)")
            cargs.append(material)
        if value is not None:
            try:
                num = float(value)
            except (TypeError, ValueError):
                con.close()
                return []
            tol = tolerance or 0.0
            if material or not include_unattr:
                conds.append(f"a1.value BETWEEN ? AND ? AND a1.confidence IN {conf}")
                cargs += [num - tol, num + tol]
            else:
                # value-only + include-unattributed: bound values or loose numbers
                conds.append(f"((a1.value BETWEEN ? AND ? AND a1.confidence IN {conf})"
                             " OR (a1.material IS NULL AND a1.value BETWEEN ? AND ?"
                             " AND a1.chain='unattributed'))")
                cargs += [num - tol, num + tol, num - tol, num + tol]
        q += (" AND EXISTS (SELECT 1 FROM attribution a1 WHERE a1.element_id=element_state.element_id"
              f" AND a1.commit_sha=element_state.commit_sha AND {' AND '.join(conds)})")
        args.extend(cargs)
    if part:
        if have_part:
            q += " AND LOWER(part)=LOWER(?)"
            args.append(part)
        else:
            q += " AND LOWER(COALESCE(text_raw,'')) LIKE '%' || LOWER(?) || '%'"
            args.append(part)
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
        q += f" AND {_LATEST_PER_DRAWING} AND status!='deleted'"
    q += " ORDER BY drawing, commit_date DESC, element_id"
    rows = [dict(r) for r in con.execute(q, args)]
    if material or value is not None:
        amap = _attribution_map(con, [(r.get("element_id"), r.get("commit_sha")) for r in rows])
        for r in rows:
            best = _best_attribution(amap.get((r.get("element_id"), r.get("commit_sha")), []),
                                     [x for x in (("material", material), ("value", value)) if x[1] not in (None, "")],
                                     tolerance or 0.0, include_low_confidence, include_unattr)
            if best is not None:
                r["confidence"] = best.get("confidence")
                r["attribution_chain"] = best.get("chain")
                r["exact_value"] = best.get("value")
                if best.get("qualifier") is not None:
                    r["qualifier"] = best.get("qualifier")
    con.close()
    for r in rows:
        r.setdefault("project", _project_of(r))
    return rows


def drawing_rows(db: Path, drawing: str, chips: list[tuple[str, str]] = (),
                 tolerance: float = 0.0,
                 include_low: bool = False, include_unattr: bool = False) -> dict:
    """Full row set for ONE drawing (no caps): live current + latest deletions,
    with matched flags and display attributions. Backs lazy group expansion.
    Display is always the current snapshot; `ever` only affects qualification
    (see search_stacked), flagged per drawing as via_history."""
    con = _con(db)
    sel = _select(con)
    rq = (f"SELECT {sel} FROM element_state WHERE drawing=? AND {_LATEST_PER_DRAWING}"
          " AND status!='deleted' ORDER BY element_id")
    rows = [dict(r) for r in con.execute(rq, (drawing,))]
    amap = _attribution_map(con, [(r.get("element_id"), r.get("commit_sha")) for r in rows])
    for r in rows:
        r.setdefault("project", _project_of(r))
        attribs = amap.get((r.get("element_id"), r.get("commit_sha")), [])
        r["matched"] = any(_chip_hit(r, attribs, k, v, tolerance, include_low, include_unattr)
                           for (k, v) in chips) if chips else True
        best = _best_attribution(attribs, chips, tolerance, include_low, include_unattr)
        if best is not None:
            r["confidence"] = best.get("confidence")
            r["attribution_chain"] = best.get("chain")
            r["exact_value"] = best.get("value")
            if best.get("qualifier") is not None:
                r["qualifier"] = best.get("qualifier")
    dzq = (f"SELECT {sel} FROM element_state WHERE drawing=? AND status='deleted'"
           f" AND {_LATEST_PER_DRAWING} ORDER BY element_id")
    dz = [dict(x) for x in con.execute(dzq, (drawing,))]
    dzmap = _attribution_map(con, [(r.get("element_id"), r.get("commit_sha")) for r in dz])
    deleted = []
    for r in dz:
        r.setdefault("project", _project_of(r))
        attribs = dzmap.get((r.get("element_id"), r.get("commit_sha")), [])
        if chips:
            r["matched"] = any(_chip_hit(r, attribs, k, v, tolerance, include_low, include_unattr)
                               for (k, v) in chips)
        else:
            r["matched"] = True
        deleted.append(r)
    con.close()
    return {"rows": rows, "deleted": deleted}


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
            rows = con.execute(
                f"SELECT {sel} FROM element_state WHERE {_LATEST_PER_DRAWING}").fetchall()
            return {(x["drawing"], x["element_id"]): dict(x) for x in rows}
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


def drawing_history(db: Path, drawing: str) -> list[dict]:
    """Revision list for one drawing: one entry per commit snapshotting it,
    oldest first, with per-status counts. Revision N = position in this list.
    """
    con = _con(db)
    revs: dict[str, dict] = {}
    order: list[str] = []
    for r in con.execute(
            "SELECT commit_sha, MIN(commit_date) AS d, MIN(author) AS a, MIN(commit_message) AS m,"
            " status, COUNT(*) AS n FROM element_state WHERE drawing=? GROUP BY commit_sha, status"
            " ORDER BY MIN(rowid)",
            (drawing,)):
        sha = r["commit_sha"]
        if sha not in revs:
            revs[sha] = {"commit_sha": sha, "commit_date": r["d"], "author": r["a"],
                         "commit_message": r["m"], "counts": {}, "changed": 0}
            order.append(sha)
        revs[sha]["counts"][r["status"]] = r["n"]
        if r["status"] != "unchanged":
            revs[sha]["changed"] += r["n"]
    con.close()
    out = []
    for i, sha in enumerate(order, 1):
        e = revs[sha]
        e["revision"] = i
        out.append(e)
    return out
