"""Part 2 — Identity resolution (the critical component).

Four tiers: handle -> fingerprint -> fuzzy -> new. Plus global
translation normalisation and tombstone resurrection via the idmap.
The idmap only ever grows — never regenerate from scratch.
"""
from __future__ import annotations

import difflib
import math
import secrets
from collections import Counter

DEFAULT_TOLERANCE_MM = 25.0
TRANSLATION_SHARE = 0.8
MOVE_EPS = 0.15  # above 0.1mm rounding


def mint_id(existing: set[str]) -> str:
    while True:
        eid = "e_" + secrets.token_hex(3)
        if eid not in existing:
            return eid


def centroid_dist(a: dict, b: dict) -> float:
    return math.hypot(float(a["geom"]["x"]) - float(b["geom"]["x"]),
                      float(a["geom"]["y"]) - float(b["geom"]["y"]))


def text_sim(a: str | None, b: str | None) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def detect_translation(current: list[dict], previous: list[dict]):
    """Modal offset vector. Returns (dx, dy, event) or (0,0,None).

    Uses handle+type+layer pairs. If >80% share the same rounded
    offset, the whole drawing moved.
    """
    prev_by_key = {(p.get("dxf_handle"), p.get("type"), p.get("layer")): p for p in previous}
    offsets: list[tuple[float, float]] = []
    for c in current:
        key = (c.get("dxf_handle"), c.get("type"), c.get("layer"))
        p = prev_by_key.get(key)
        if p is None:
            continue
        dx = round(float(c["geom"]["x"]) - float(p["geom"]["x"]), 1)
        dy = round(float(c["geom"]["y"]) - float(p["geom"]["y"]), 1)
        offsets.append((dx, dy))
    if len(offsets) < 3:
        return 0.0, 0.0, None
    common, count = Counter(offsets).most_common(1)[0]
    if common == (0.0, 0.0):
        return 0.0, 0.0, None
    if count / len(offsets) >= TRANSLATION_SHARE:
        dx, dy = common
        event = {"dx": dx, "dy": dy, "matched": count, "total": len(offsets)}
        return dx, dy, event
    return 0.0, 0.0, None


def _shifted_copy(rec: dict, dx: float, dy: float) -> dict:
    """Copy with global translation subtracted (for matching only)."""
    c = dict(rec)
    g = dict(rec.get("geom", {}))
    bb = list(g.get("bbox", [0, 0, 0, 0]))
    g["x"] = round(float(g.get("x", 0.0)) - dx, 1)
    g["y"] = round(float(g.get("y", 0.0)) - dy, 1)
    g["bbox"] = [round(float(bb[0]) - dx, 1), round(float(bb[1]) - dy, 1),
                 round(float(bb[2]) - dx, 1), round(float(bb[3]) - dy, 1)]
    c["geom"] = g
    # Normalised fingerprint so tier-2 can fire even under a global move
    # when handles also changed.
    try:
        from .extract import fingerprint_for
        c["_norm_fingerprint"] = fingerprint_for(
            c.get("type", ""), c.get("layer", ""), g,
            c.get("text_raw"), c.get("hatch_pattern"),
            c.get("dim_measurement"), c.get("dim_override"))
    except Exception:
        c["_norm_fingerprint"] = c.get("fingerprint", "")
    return c


def _parsed_equal(a, b) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    keys = {"material", "value", "unit", "qualifier", "profile", "dims", "r_value"}
    for k in keys:
        if a.get(k) != b.get(k):
            return False
    return True


def compute_status(curr: dict, prev: dict) -> str:
    moved = centroid_dist(curr, prev) > MOVE_EPS
    # bbox change also counts as moved (e.g. stretched rectangle, same centroid)
    try:
        bb_c, bb_p = curr["geom"]["bbox"], prev["geom"]["bbox"]
        if any(abs(float(x) - float(y)) > 0.15 for x, y in zip(bb_c, bb_p)):
            moved = True
    except Exception:
        pass
    value_changed = (
        curr.get("text_raw") != prev.get("text_raw")
        or not _parsed_equal(curr.get("parsed"), prev.get("parsed"))
        or curr.get("hatch_pattern") != prev.get("hatch_pattern")
        or curr.get("dim_measurement") != prev.get("dim_measurement")
        or curr.get("dim_override") != prev.get("dim_override")
    )
    if moved and value_changed:
        return "moved+value_changed"
    if moved:
        return "moved"
    if value_changed:
        return "value_changed"
    return "unchanged"


def _tombstone_records(idmap: dict) -> list[dict]:
    recs = []
    for eid, info in (idmap.get("elements") or {}).items():
        if info.get("deleted"):
            recs.append({
                "element_id": eid,
                "dxf_handle": info.get("dxf_handle", ""),
                "type": info.get("type", ""),
                "layer": info.get("layer", ""),
                "geom": {"x": info.get("x", 0.0), "y": info.get("y", 0.0),
                         "bbox": info.get("bbox", [0, 0, 0, 0])},
                "text_raw": info.get("text_raw"),
                "parsed": info.get("parsed"),
                "hatch_pattern": info.get("hatch_pattern"),
                "dim_measurement": info.get("dim_measurement"),
                "dim_override": info.get("dim_override"),
                "fingerprint": info.get("fingerprint", ""),
                "_tombstone": True,
            })
    return recs


def resolve(current_raw: list[dict], previous_live: list[dict], idmap: dict,
            tolerance: float = DEFAULT_TOLERANCE_MM):
    """Match current entities to previous IDs.

    Returns (resolved, updated_idmap, translation_event, fuzzy_reviews).
    resolved carries element_id + ephemeral status/match_tier/match_confidence
    for display and future indexing. Callers must STRIP those three keys
    before writing state/*.jsonl — the committed canonical state holds
    only element_id + DXF facts, so re-extracting an unchanged drawing is
    byte-identical (acceptance test 1).
    Stored geom/fingerprint are always the true as-drawn values; the
    global translation is subtracted only for matching/status.
    """
    idmap = {"drawing": idmap.get("drawing", ""), "elements": dict(idmap.get("elements") or {})}
    existing_ids = set(idmap.get("elements", {}).keys()) | {p.get("element_id") for p in previous_live if p.get("element_id")}

    # 0. global translation normalisation — matching/status use shifted
    # copies; stored records keep true as-drawn geom.
    dx, dy, event = detect_translation(current_raw, previous_live)
    if event is not None:
        norm_current = [_shifted_copy(c, dx, dy) for c in current_raw]
    else:
        norm_current = [dict(c) for c in current_raw]

    prev_by_handle = {(p.get("dxf_handle"), p.get("type"), p.get("layer")): p for p in previous_live if p.get("element_id")}
    prev_by_fp: dict[str, list[dict]] = {}
    for p in previous_live:
        prev_by_fp.setdefault(p.get("fingerprint", ""), []).append(p)
    for t in _tombstone_records(idmap):
        if t.get("fingerprint"):
            prev_by_fp.setdefault(t["fingerprint"], []).append(t)

    used_prev: set[str] = set()
    resolved: list[dict] = []
    fuzzy_reviews: list[dict] = []

    # candidate pool for fuzzy: live previous not yet used + tombstones
    tombstones = _tombstone_records(idmap)

    for orig, n in zip(current_raw, norm_current):
        c = dict(orig)  # stored record keeps true as-drawn geom
        cn = n  # normalised copy used for matching/status only
        matched = None
        tier = None
        conf = 1.0

        # Tier 1 — handle
        key = (cn.get("dxf_handle"), cn.get("type"), cn.get("layer"))
        p1 = prev_by_handle.get(key)
        if p1 is not None and p1.get("element_id") not in used_prev:
            matched = p1
            tier = "handle"

        # Tier 2 — fingerprint (normalised; includes tombstones for resurrection)
        if matched is None:
            fp_norm = cn.get("_norm_fingerprint") or cn.get("fingerprint", "")
            seen: set[str] = set()
            cands: list[dict] = []
            for key in (cn.get("fingerprint", ""), fp_norm):
                for cand in prev_by_fp.get(key or "", []):
                    if cand.get("element_id") not in seen:
                        seen.add(cand.get("element_id", ""))
                        cands.append(cand)
            for cand in cands:
                if cand.get("element_id") not in used_prev:
                    # fingerprint tier requires same type+layer (sha already has them, double-check)
                    if cand.get("type") == cn.get("type") and cand.get("layer") == cn.get("layer"):
                        matched = cand
                        tier = "fingerprint"
                        break

        # Tier 3 — fuzzy (on normalised centroids)
        if matched is None:
            best = None
            best_score = -1.0
            pool = [p for p in previous_live if p.get("element_id") not in used_prev
                    and p.get("type") == cn.get("type") and p.get("layer") == cn.get("layer")]
            pool += [t for t in tombstones if t.get("element_id") not in used_prev
                     and t.get("type") == cn.get("type") and t.get("layer") == cn.get("layer")]
            for cand in pool:
                d = centroid_dist(cn, cand)
                if d > tolerance:
                    continue
                is_text = cn.get("type") in ("TEXT", "MTEXT", "MULTILEADER")
                if is_text:
                    s = text_sim(cn.get("text_raw"), cand.get("text_raw"))
                    if s < 0.6:
                        continue
                    score = s - (d / tolerance) * 0.2
                    this_conf = round(s, 3)
                else:
                    score = 1.0 - (d / tolerance)
                    this_conf = round(1.0 - (d / tolerance), 3)
                if score > best_score:
                    best_score = score
                    best = (cand, this_conf)
            if best is not None:
                matched, conf = best
                tier = "fuzzy"

        if matched is not None:
            eid = matched["element_id"]
            used_prev.add(eid)
            c["element_id"] = eid
            c["match_tier"] = tier
            c["match_confidence"] = 1.0 if tier in ("handle", "fingerprint") else conf
            # status vs the live previous record (tombstone resurrection compares too)
            # — computed on normalised geom so a pure global shift reads unchanged.
            live_prev = next((p for p in previous_live if p.get("element_id") == eid), matched)
            c["status"] = compute_status(cn, live_prev)
            # If resurrected from tombstone and geometry identical, status may read
            # unchanged — surface as value-cleared new sighting but keep ID.
            if matched.get("_tombstone") and c["status"] == "unchanged":
                c["status"] = "unchanged"
            if tier == "fuzzy":
                fuzzy_reviews.append({
                    "element_id": eid,
                    "dxf_handle": c.get("dxf_handle"),
                    "tier": tier,
                    "confidence": c["match_confidence"],
                    "prev_text": live_prev.get("text_raw"),
                    "curr_text": c.get("text_raw"),
                })
        else:
            eid = mint_id(existing_ids)
            existing_ids.add(eid)
            c["element_id"] = eid
            c["match_tier"] = "new"
            c["match_confidence"] = 1.0
            c["status"] = "new"

        resolved.append(c)

    # Deleted: live previous IDs with no match -> tombstones in idmap
    matched_ids = {r["element_id"] for r in resolved}
    for p in previous_live:
        eid = p.get("element_id")
        if eid and eid not in matched_ids:
            info = idmap["elements"].get(eid, {})
            info.update({
                "dxf_handle": p.get("dxf_handle", ""),
                "fingerprint": p.get("fingerprint", ""),
                "type": p.get("type", ""),
                "layer": p.get("layer", ""),
                "x": p.get("geom", {}).get("x", 0.0),
                "y": p.get("geom", {}).get("y", 0.0),
                "bbox": p.get("geom", {}).get("bbox", [0, 0, 0, 0]),
                "text_raw": p.get("text_raw"),
                "parsed": p.get("parsed"),
                "hatch_pattern": p.get("hatch_pattern"),
                "dim_measurement": p.get("dim_measurement"),
                "dim_override": p.get("dim_override"),
                "deleted": True,
            })
            idmap["elements"][eid] = info

    # Upsert live entries (only grows)
    for r in resolved:
        idmap["elements"][r["element_id"]] = {
            "dxf_handle": r.get("dxf_handle", ""),
            "fingerprint": r.get("fingerprint", ""),
            "type": r.get("type", ""),
            "layer": r.get("layer", ""),
            "x": r.get("geom", {}).get("x", 0.0),
            "y": r.get("geom", {}).get("y", 0.0),
            "bbox": r.get("geom", {}).get("bbox", [0, 0, 0, 0]),
            "text_raw": r.get("text_raw"),
            "parsed": r.get("parsed"),
            "hatch_pattern": r.get("hatch_pattern"),
            "dim_measurement": r.get("dim_measurement"),
            "dim_override": r.get("dim_override"),
            "deleted": False,
        }

    resolved.sort(key=lambda r: (r["layer"], r["type"], r["geom"]["x"], r["geom"]["y"], r.get("text_raw") or ""))
    return resolved, idmap, event, fuzzy_reviews
