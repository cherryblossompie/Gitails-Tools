"""7.1 sheet segmentation + 7.5 detail-level records.

One sheet is not one detail: a sheet routinely carries several details, each
with its own title marker (CIRCLE + TEXT/MTEXT tag inside, adjacent title text
and Scale string). A user searching for a door jamb wants that detail, not the
sheet containing it among four others.

Pipeline:
  find_title_markers(records)  -> anchors (circle + tag + title + scale)
  assign_entities(records, markers) -> per-marker entity sets (nearest-marker
      Voronoi split; viewport/grid rectangles win as hard borders when present)
  segment_sheet(...) -> {"segmentation": certain|uncertain, "details": [...]}
  build_sheet_details(...) -> committed details/*.json payload: segmentation +
      per-region 7.5 detail records (classifications, build_up, text_blob,
      depends_on, wet_area) joined to the 7.2 attribution summary.

Uncertainty rule (brief 7.1): where segmentation confidence is low, index the
whole sheet as one record flagged segmentation=uncertain for review. Never
silently split a detail in half.

Operates on extract/state records only (no ezdxf handle needed): geom centroid
or insert point per record, CIRCLE centre from vertices[0] with radius from
length/(2*pi) falling back to bbox. Deterministic: markers sorted by (x, y),
details by tag, element lists sorted.
"""
from __future__ import annotations

import hashlib
import math
import re
from pathlib import Path

# Plausible title-marker bubble size (model mm). Grids/sections use similar
# bubbles, which is why a bare circle+tag without title/scale is NOT trusted.
MARKER_RADIUS_MIN = 3.0
MARKER_RADIUS_MAX = 25.0
# How far from a bubble to look for its title/scale companion (model mm).
MARKER_SEARCH_RADIUS = 150.0
# A region holding less than this share of entities means a marker is likely a
# stray bubble, not a detail anchor -> uncertain whole-sheet fallback.
MIN_REGION_SHARE = 0.05
# Content islands: entity bboxes gapped by more than this (model mm) belong to
# different details. Must bridge text-to-leader gaps (~0mm, they touch) while
# stopping at inter-detail whitespace (~90mm on the reference sheet).
ISLAND_GAP = 25.0
# Overlapping region bboxes beyond this IoU (of the smaller) -> uncertain.
MAX_REGION_OVERLAP = 0.5

SCALE_RE = re.compile(r"\b1\s*:\s*(\d+(?:\.\d+)?)\b")
NTS_RE = re.compile(r"\b(NTS|NOT\s+TO\s+SCALE|DO\s+NOT\s+SCALE)\b", re.IGNORECASE)
SCALE_ANY_RE = re.compile(r"\bscale\b", re.IGNORECASE)


def _defpoint_bbox(rec: dict) -> list[float] | None:
    """DIMENSION fallback: defpoints locate the measured geometry. Extract
    stores a degenerate [0,0,0,0] geom for dimensions (ezdxf extents fail on
    overridden text), and that geom must NOT change — it feeds fingerprints.
    So segmentation resolves dimension position locally, here only."""
    try:
        pts = [p for p in (rec.get("dim_defpoints") or []) if p]
        if len(pts) < 2:
            return None
        xs = [float(p[0]) for p in pts]
        ys = [float(p[1]) for p in pts]
        return [min(xs), min(ys), max(xs), max(ys)]
    except (TypeError, ValueError, IndexError):
        return None


def _anchor(rec: dict) -> tuple[float, float] | None:
    """Position used for clustering: insertion point, else dimension defpoint
    centroid, else geom centroid."""
    try:
        ix, iy = rec.get("insert_x"), rec.get("insert_y")
        if ix is not None and iy is not None:
            return (float(ix), float(iy))
    except (TypeError, ValueError):
        pass
    if rec.get("type") == "DIMENSION":
        bb = _defpoint_bbox(rec)
        if bb is not None:
            return ((bb[0] + bb[2]) / 2.0, (bb[1] + bb[3]) / 2.0)
    try:
        g = rec.get("geom") or {}
        return (float(g["x"]), float(g["y"]))
    except (TypeError, ValueError, KeyError):
        return None


def _circle_center_radius(rec: dict) -> tuple[tuple[float, float], float] | None:
    if rec.get("type") != "CIRCLE":
        return None
    center = None
    try:
        verts = rec.get("vertices") or []
        if verts:
            center = (float(verts[0][0]), float(verts[0][1]))
    except (TypeError, ValueError, IndexError):
        center = None
    if center is None:
        a = _anchor(rec)
        if a is None:
            return None
        center = a
    radius = 0.0
    try:
        if rec.get("length"):
            radius = float(rec["length"]) / (2.0 * math.pi)
    except (TypeError, ValueError):
        radius = 0.0
    if not radius:
        try:
            bb = (rec.get("geom") or {}).get("bbox") or [0, 0, 0, 0]
            radius = max(float(bb[2]) - float(bb[0]), float(bb[3]) - float(bb[1])) / 2.0
        except (TypeError, ValueError):
            return None
    return center, radius


def _norm_tag(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).upper()


def _parse_scale(text: str | None) -> str | None:
    if not text:
        return None
    m = SCALE_RE.search(text)
    if m:
        denom = m.group(1)
        return f"1:{denom}"
    if NTS_RE.search(text):
        return "NTS"
    return None


def _is_scale_text(text: str | None) -> bool:
    if not text:
        return False
    return bool(SCALE_ANY_RE.search(text) or _parse_scale(text))


def _dist(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def find_title_markers(records: list[dict]) -> list[dict]:
    """Locate detail title markers: CIRCLE + TEXT/MTEXT tag inside, paired with
    adjacent title text and Scale string. Sorted by (x, y)."""
    texts = [r for r in records if r.get("type") in ("TEXT", "MTEXT", "ANNOTATION") and (r.get("text_raw") or "").strip()]
    markers = []
    for rec in records:
        cr = _circle_center_radius(rec)
        if cr is None:
            continue
        center, radius = cr
        if not (MARKER_RADIUS_MIN <= radius <= MARKER_RADIUS_MAX):
            continue
        # tag: text anchored inside the bubble
        tag_rec, tag_d = None, radius * 1.5 + 2.0
        for t in texts:
            a = _anchor(t)
            if a is None:
                continue
            d = _dist(a, center)
            if d <= radius * 1.2 + 1.0 and d < tag_d:
                tag_rec, tag_d = t, d
        if tag_rec is None:
            continue  # bare circle (grid bubble, column mark) — not a detail anchor
        tag = _norm_tag(tag_rec.get("text_raw"))
        # scale: nearest scale-like string within search radius
        scale_rec, scale_d = None, MARKER_SEARCH_RADIUS
        for t in texts:
            if t is tag_rec or not _is_scale_text(t.get("text_raw")):
                continue
            a = _anchor(t)
            if a is None:
                continue
            d = _dist(a, center)
            if d <= MARKER_SEARCH_RADIUS and d < scale_d:
                scale_rec, scale_d = t, d
        # title: nearest other text within radius, preferring a substantial
        # string (sheet titles are shared; adjacency wins over length).
        title_rec, title_d = None, MARKER_SEARCH_RADIUS
        title_fallback, title_fallback_d = None, MARKER_SEARCH_RADIUS
        for t in texts:
            if t is tag_rec or t is scale_rec:
                continue
            a = _anchor(t)
            if a is None:
                continue
            d = _dist(a, center)
            if d > MARKER_SEARCH_RADIUS:
                continue
            body = (t.get("text_raw") or "").strip()
            if len(body) >= 10 and d < title_d:
                title_rec, title_d = t, d
            if d < title_fallback_d:
                title_fallback, title_fallback_d = t, d
        if title_rec is None:
            title_rec = title_fallback
        markers.append({
            "center": [round(center[0], 1), round(center[1], 1)],
            "radius": round(radius, 1),
            "tag": tag,
            "tag_element": tag_rec.get("element_id"),
            "title": (title_rec.get("text_raw") or "").strip() if title_rec is not None else None,
            "title_element": title_rec.get("element_id") if title_rec is not None else None,
            "title_anchor": list(_anchor(title_rec)) if title_rec is not None and _anchor(title_rec) else None,
            "scale": _parse_scale(scale_rec.get("text_raw")) if scale_rec is not None else None,
            "scale_element": scale_rec.get("element_id") if scale_rec is not None else None,
            "circle_element": rec.get("element_id"),
        })
    markers.sort(key=lambda m: (m["center"][0], m["center"][1]))
    return markers


def _hard_borders(records: list[dict]) -> list[dict]:
    """Viewport / grid rectangles that act as hard region borders.

    Closed LWPOLYLINE loops on a viewport-like layer. Preferred over the
    nearest-marker split when an entity falls inside one.
    """
    borders = []
    for r in records:
        if r.get("type") != "LWPOLYLINE" or not r.get("closed"):
            continue
        layer = str(r.get("layer") or "").upper()
        if not any(k in layer for k in ("VIEWPORT", "VPORT", "VIEW_PORT", "GRID", "SHEET")):
            continue
        try:
            pts = [(float(p[0]), float(p[1])) for p in (r.get("vertices") or [])]
        except (TypeError, ValueError):
            continue
        if len(pts) < 3:
            continue
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        area = abs(sum(xs[i] * ys[(i + 1) % len(ys)] - xs[(i + 1) % len(xs)] * ys[i]
                       for i in range(len(xs)))) / 2.0
        if area < 10000.0:
            continue
        borders.append({"polygon": pts, "bbox": [min(xs), min(ys), max(xs), max(ys)],
                        "layer": r.get("layer")})
    return borders


def _in_poly(x: float, y: float, poly: list) -> bool:
    inside = False
    n = len(poly)
    for i in range(n):
        x0, y0 = poly[i]
        x1, y1 = poly[(i + 1) % n]
        if ((y0 > y) != (y1 > y)) and (x < (x1 - x0) * (y - y0) / (y1 - y0) + x0):
            inside = not inside
    return inside


def _bbox_of(rec: dict) -> list[float] | None:
    try:
        bb = (rec.get("geom") or {}).get("bbox") or [0, 0, 0, 0]
        bb = [float(bb[0]), float(bb[1]), float(bb[2]), float(bb[3])]
        if bb == [0.0, 0.0, 0.0, 0.0] and rec.get("type") == "DIMENSION":
            return _defpoint_bbox(rec) or bb
        return bb
    except (TypeError, ValueError, IndexError):
        return None


def _bbox_gap(a: list[float], b: list[float]) -> float:
    dx = max(b[0] - a[2], a[0] - b[2], 0.0)
    dy = max(b[1] - a[3], a[1] - b[3], 0.0)
    return math.hypot(dx, dy)


def _content_islands(records: list[dict], pinned: set[int]) -> list[list[int]]:
    """Spatially coherent entity groups (union-find over bbox gaps).

    Leader lines touch their callout text and their region, so one detail's
    furniture joins into one island while inter-detail whitespace (~gutter)
    keeps islands apart. Marker-own entities are pinned, never clustered.
    """
    idxs = [n for n in range(len(records)) if n not in pinned]
    parent = {n: n for n in idxs}

    def find(n):
        while parent[n] != n:
            parent[n] = parent[parent[n]]
            n = parent[n]
        return n

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    boxes = {n: _bbox_of(records[n]) for n in idxs}
    order = sorted(idxs)
    for i, a in enumerate(order):
        if boxes[a] is None:
            continue
        for b in order[i + 1:]:
            if boxes[b] is None:
                continue
            if _bbox_gap(boxes[a], boxes[b]) <= ISLAND_GAP:
                union(a, b)
    groups: dict[int, list[int]] = {}
    for n in idxs:
        groups.setdefault(find(n), []).append(n)
    return [sorted(g) for g in groups.values()]


def _marker_anchor(m: dict) -> tuple[float, float]:
    """What an island measures its distance to: the title block position when
    known (titles sit with their detail's column), else the bubble centre."""
    for key in ("title_anchor", "center"):
        try:
            p = m.get(key)
            if p:
                return (float(p[0]), float(p[1]))
        except (TypeError, ValueError):
            continue
    return (0.0, 0.0)


def assign_entities(records: list[dict], markers: list[dict]) -> dict[int, list[int]]:
    """Record indices per marker. Marker-own entities pinned; viewport borders
    win; remaining entities cluster into content islands (whitespace-gutter
    bounded) and each island goes to the marker whose title sits nearest —
    so one connected detail is never split across regions."""
    owned: dict[str, int] = {}
    for i, m in enumerate(markers):
        for key in ("circle_element", "tag_element", "title_element", "scale_element"):
            eid = m.get(key)
            if eid:
                owned[eid] = i
    by_eid = {r.get("element_id"): n for n, r in enumerate(records) if r.get("element_id")}
    pinned_idx = {by_eid[e] for e in owned if e in by_eid}
    assignment: dict[int, list[int]] = {i: [] for i in range(len(markers))}
    for n in pinned_idx:
        assignment[owned[records[n].get("element_id")]].append(n)
    borders = _hard_borders(records)
    anchors = [_marker_anchor(m) for m in markers]

    def border_marker(a: tuple[float, float]) -> int | None:
        for b in borders:
            bb = b["bbox"]
            if bb[0] <= a[0] <= bb[2] and bb[1] <= a[1] <= bb[3] and _in_poly(a[0], a[1], b["polygon"]):
                cx, cy = (bb[0] + bb[2]) / 2.0, (bb[1] + bb[3]) / 2.0
                return min(range(len(markers)), key=lambda i: _dist((cx, cy), anchors[i]))
        return None

    for island in _content_islands(records, pinned_idx):
        # hard border containment is per-entity; islands split by borders first
        grouped: dict[int, list[int]] = {}
        unbordered: list[int] = []
        for n in island:
            a = _anchor(records[n])
            hit = border_marker(a) if a is not None else None
            if hit is None:
                unbordered.append(n)
            else:
                grouped.setdefault(hit, []).append(n)
        for mi, members in grouped.items():
            assignment[mi].extend(members)
        if unbordered:
            cx = sum((_anchor(records[n]) or (0.0, 0.0))[0] for n in unbordered) / len(unbordered)
            cy = sum((_anchor(records[n]) or (0.0, 0.0))[1] for n in unbordered) / len(unbordered)
            best = min(range(len(markers)), key=lambda i: _dist((cx, cy), anchors[i]))
            assignment[best].extend(unbordered)
    for mi in assignment:
        assignment[mi] = sorted(assignment[mi])
    return assignment


def _region_bbox(records: list[dict], idxs: list[int]) -> list[float]:
    xs, ys = [], []
    for n in idxs:
        bb = _bbox_of(records[n])
        if bb is None:
            continue
        xs += [bb[0], bb[2]]
        ys += [bb[1], bb[3]]
    if not xs:
        return [0.0, 0.0, 0.0, 0.0]
    return [round(min(xs), 1), round(min(ys), 1), round(max(xs), 1), round(max(ys), 1)]


def _bbox_iou_small(a: list[float], b: list[float]) -> float:
    ix0, iy0 = max(a[0], b[0]), max(a[1], b[1])
    ix1, iy1 = min(a[2], b[2]), min(a[3], b[3])
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    aa = max(a[2] - a[0], 0.0) * max(a[3] - a[1], 0.0)
    ab = max(b[2] - b[0], 0.0) * max(b[3] - b[1], 0.0)
    small = min(aa, ab)
    return (inter / small) if small > 0 else 0.0


def detail_id_for(drawing: str, tag: str) -> str:
    return "d_" + hashlib.sha1(f"{drawing}::{tag}".encode("utf-8")).hexdigest()[:4]


def segment_sheet(records: list[dict], drawing: str) -> dict:
    """Split a sheet's records into detail regions.

    Returns {"drawing", "segmentation": certain|uncertain, "markers",
    "regions": [{"tag", "title", "scale", "bbox", "element_ids",
    "record_indexes"}]}. Low confidence -> single whole-sheet region with
    segmentation=uncertain (never a silent half-split).
    """
    markers = find_title_markers(records)
    whole_bbox = _region_bbox(records, list(range(len(records))))
    all_ids = sorted(r.get("element_id") for r in records if r.get("element_id"))

    def whole(reason: str) -> dict:
        return {"drawing": drawing, "segmentation": "uncertain",
                "uncertain_reason": reason, "markers": markers,
                "regions": [{"tag": "SHEET", "title": None, "scale": None,
                             "bbox": whole_bbox, "element_ids": all_ids,
                             "record_indexes": list(range(len(records)))}]}

    if not markers:
        return whole("no title markers found")
    if len(markers) == 1 and markers[0].get("title") is None and markers[0].get("scale") is None:
        return whole("single bare bubble without title/scale")
    assignment = assign_entities(records, markers)
    total = max(len(records), 1)
    for i, idxs in assignment.items():
        if len(idxs) / total < MIN_REGION_SHARE:
            return whole(f"marker {markers[i]['tag']} holds only {len(idxs)}/{len(records)} entities")
    regions = []
    for i, m in enumerate(markers):
        idxs = sorted(assignment[i])
        eids = sorted(records[n].get("element_id") for n in idxs if records[n].get("element_id"))
        regions.append({"tag": m["tag"], "title": m.get("title"), "scale": m.get("scale"),
                        "bbox": _region_bbox(records, idxs), "element_ids": eids,
                        "record_indexes": idxs})
    for a in range(len(regions)):
        for b in range(a + 1, len(regions)):
            if _bbox_iou_small(regions[a]["bbox"], regions[b]["bbox"]) > MAX_REGION_OVERLAP:
                return whole("detail regions overlap — refusing to split")
    regions.sort(key=lambda r: r["tag"])
    return {"drawing": drawing, "segmentation": "certain", "uncertain_reason": None,
            "markers": markers, "regions": regions}


# --------------------------------------------------------------------------
# 7.5 detail-level records


def _region_texts(records: list[dict], idxs: list[int]) -> list[str]:
    out = []
    for n in idxs:
        t = records[n].get("text_raw")
        if t:
            out.append(t)
        d = records[n].get("dim_override")
        if d:
            out.append(str(d))
    return out


def build_sheet_details(drawing: str, records: list[dict],
                        materials_cfg: dict | None = None,
                        geometry_cfg: dict | None = None,
                        doc=None, analysis: dict | None = None,
                        tax=None, taxonomy_dir=None,
                        ai_ctx: dict | None = None, crop_fn=None) -> dict:
    """Full details/*.json payload for one sheet: segmentation + one 7.5 record
    per region, joined to 7.2 attribution evidence and classified against the
    taxonomy (Part 8). Unresolvable values are returned as
    quarantine_candidates — never into the tree, never blocking.

    `records` are resolved state rows (with element_ids). `analysis` is an
    optional attribute.analyze_records() summary; when omitted and
    materials_cfg is given it is computed here (doc may be None for PDFs).
    `tax` defaults to the bundled seed via a cached loader. ai_ctx/crop_fn
    enable Chain D review rows (disabled by default: pending, never called).
    """
    from .classify import (classify_attribution, facet_components,
                           facet_nodes, guess_unknown_part, matches_ignore,
                           prefill_facets)
    from .taxonomy import load_taxonomy_cached
    seg = segment_sheet(records, drawing)
    if tax is None:
        try:
            tax = load_taxonomy_cached(taxonomy_dir)
        except Exception:
            tax = None
    if analysis is None and materials_cfg is not None:
        try:
            from .attribute import analyze_records, load_geometry_config
            gcfg = dict(geometry_cfg or {})
            if not gcfg:
                try:
                    gcfg = load_geometry_config(None)
                except Exception:
                    gcfg = {}
            analysis = analyze_records(records, doc, materials_cfg, gcfg,
                                       ai_ctx=ai_ctx, crop_fn=crop_fn,
                                       drawing=drawing)
        except Exception:
            analysis = None
    attributions = (analysis or {}).get("attributions", []) if analysis else []
    regions = (analysis or {}).get("regions", []) if analysis else []
    by_element: dict[str, list[dict]] = {}
    for a in attributions:
        if a.get("element_id"):
            by_element.setdefault(a["element_id"], []).append(a)
    by_eid_record = {r.get("element_id"): r for r in records if r.get("element_id")}
    # marker furniture (bubble/tag/title/scale) is sheet furniture, never a
    # quarantine candidate — its identity is structural, not textual.
    marker_owned: set[str] = set()
    for m in seg.get("markers", []) or []:
        for key in ("circle_element", "tag_element", "title_element", "scale_element"):
            if m.get(key):
                marker_owned.add(m[key])

    def region_layer_at(eid: str | None) -> tuple[str | None, float | None]:
        """Hatch/loop region containing the element anchor (8.7.2 evidence)."""
        rec = by_eid_record.get(eid) if eid else None
        anchor = _anchor(rec) if rec else None
        if anchor is None:
            return None, None
        for reg in regions:
            poly = reg.get("polygon") or []
            if len(poly) >= 3 and _in_poly(anchor[0], anchor[1], poly):
                return reg.get("layer"), reg.get("thickness")
        return None, None

    details = []
    quarantine_candidates = []
    for region in seg["regions"]:
        in_region = set(region["element_ids"])
        region_attribs = [a for eid in in_region for a in by_element.get(eid, [])]
        texts = _region_texts(records, region["record_indexes"])
        try:
            from .attribute import extract_references, wet_area_flag
            depends = extract_references(" ".join(t for t in texts if t))
            wet = wet_area_flag(texts)
        except Exception:
            depends, wet = [], False
        tag = region["tag"]
        title = region.get("title") or ("Typical details" if tag == "SHEET" else tag)
        # 7.7 pre-fill (heuristic; uploader confirms via details confirm).
        facets = prefill_facets(title, texts)
        comps = facet_components(tax, facets) if tax is not None else []
        nodes = facet_nodes(tax, facets) if tax is not None else []
        classifications: list[dict] = []
        for a in region_attribs:
            if not a.get("material") and a.get("part") is None \
                    and a.get("value") is None:
                continue
            if tax is None:
                continue
            # unknown-noun guess: part tokens outside the config list surface
            # here (FLANGE 5MM ALUMINIUM), so they can reach quarantine.
            if a.get("part") is None and materials_cfg is not None:
                raw_text = (by_eid_record.get(a.get("element_id")) or {}).get(
                    "text_raw") or ""
                token = guess_unknown_part(raw_text, materials_cfg)
                if token:
                    a = {**a, "part": token}
            entries, cand = classify_attribution(tax, a, comps)
            classifications.extend(entries)
            if cand is not None:
                rec = by_eid_record.get(a.get("element_id"), {})
                rlayer, thick = region_layer_at(a.get("element_id"))
                quarantine_candidates.append({
                    "raw_string": rec.get("text_raw") or rec.get("dim_override") or "",
                    "level_guess": cand["level_guess"], "attribute": cand["attribute"],
                    "material": a.get("material"), "part": a.get("part"),
                    "value": a.get("value"), "qualifier": a.get("qualifier"),
                    "measure": a.get("measure") or "thickness",
                    "element_id": a.get("element_id"),
                    "detail_id": detail_id_for(drawing, tag),
                    "evidence": {"layer": rec.get("layer"),
                                 "hatch_pattern": rec.get("hatch_pattern"),
                                 "region_layer": rlayer,
                                 "measured_thickness": thick,
                                 "attribution_chain": a.get("attribution_chain"),
                                 "sibling_context": []},
                    "reason": cand["reason"]})
        # annotation texts without material attributions, handled uniformly:
        # a part token (parsed or previously-resolved synonym) classifies as a
        # bare part or quarantines as an unknown part; anything else unparsed
        # becomes a vocabulary candidate — unless marker furniture,
        # boilerplate, or bare numbers. Qualifier-only notes ("WALLTYPE
        # VARIES") are information, not vocabulary: full-text searchable,
        # out of quarantine.
        from .classify import classify_attribution as _classify
        from .classify import match_known_string as _known

        def _handle_token(rec, text, part_token, material, value_num,
                          qualifier, chain, measure=None):
            entries, cand = _classify(
                tax, {"material": material, "part": part_token,
                      "value": value_num, "qualifier": qualifier,
                      "measure": measure or "thickness",
                      "confidence": "medium", "attribution_chain": chain,
                      "element_id": rec.get("element_id")}, comps)
            classifications.extend(entries)
            # boilerplate ("REFER ...") is never vocabulary even when it
            # carries a guessed token — the ignore list gates candidates,
            # never the element index (still full-text searchable).
            if cand is not None and tax is not None and matches_ignore(tax, text):
                cand = None
            if cand is not None:
                rlayer, thick = region_layer_at(rec.get("element_id"))
                quarantine_candidates.append({
                    "raw_string": text, "level_guess": cand["level_guess"],
                    "attribute": cand["attribute"], "material": material,
                    "part": part_token, "value": value_num,
                    "qualifier": qualifier,
                    "measure": measure or "thickness",
                    "element_id": rec.get("element_id"),
                    "detail_id": detail_id_for(drawing, tag),
                    "evidence": {"layer": rec.get("layer"),
                                 "hatch_pattern": rec.get("hatch_pattern"),
                                 "region_layer": rlayer,
                                 "measured_thickness": thick,
                                 "sibling_context": []},
                    "reason": cand["reason"]})

        for n in region["record_indexes"]:
            rec = records[n]
            if rec.get("type") not in ("TEXT", "MTEXT", "MULTILEADER", "PDFTEXT", "ANNOTATION"):
                continue
            text = (rec.get("text_raw") or "").strip()
            if not text:
                continue
            if rec.get("element_id") in marker_owned:
                continue
            parsed = rec.get("parsed")
            if parsed is not None and (
                    parsed.get("material") or rec.get("element_id") in by_element):
                continue  # attribution loop already handled this element
            import re as _re
            if parsed is None:
                if _re.fullmatch(r"[\d\s.,x×:+\-°'\"]+", text):
                    continue
                if tax is not None and matches_ignore(tax, text):
                    continue
                if tax is None:
                    continue
                # a resolved synonym classifies silently (8.7.4A); an unknown
                # noun becomes a token-level candidate; anything else is a
                # genuinely new vocabulary candidate.
                hit = _known(tax, text)
                if hit is not None and tax.nodes[hit]["level"] == "part":
                    _handle_token(rec, text, hit.split(".", 1)[1].replace("_", " "),
                                  None, None, None, "synonym")
                elif hit is not None and tax.nodes[hit]["level"] == "value":
                    _handle_token(rec, text, None, hit.split(".", 1)[1],
                                  None, None, "synonym")
                else:
                    token = guess_unknown_part(text, materials_cfg) \
                        if materials_cfg is not None else None
                    if token:
                        _handle_token(rec, text, token, None, None, None,
                                      "leader")
                        continue
                    rlayer, thick = region_layer_at(rec.get("element_id"))
                    quarantine_candidates.append({
                        "raw_string": text, "level_guess": "part",
                        "attribute": None, "material": None, "part": None,
                        "value": None, "qualifier": None,
                        "element_id": rec.get("element_id"),
                        "detail_id": detail_id_for(drawing, tag),
                        "evidence": {"layer": rec.get("layer"),
                                     "hatch_pattern": rec.get("hatch_pattern"),
                                     "region_layer": rlayer,
                                     "measured_thickness": thick,
                                     "sibling_context": []},
                        "reason": "unparsed"})
                continue
            token = parsed.get("part")
            if not token or tax is None:
                continue
            _handle_token(rec, text, token, None, None,
                          parsed.get("qualifier"), "leader",
                          parsed.get("measure"))
        # sibling context: taxonomy nodes already resolved on this detail —
        # drives suggestion ranking and create-new parent defaults (8.7.2).
        sibs = set(nodes)
        for c in classifications:
            for el in c["path"]:
                if str(el).startswith(("component.", "part.")):
                    sibs.add(el)
        sibs = sorted(sibs)
        for cand in quarantine_candidates:
            if cand["detail_id"] == detail_id_for(drawing, tag) \
                    and not cand["evidence"]["sibling_context"]:
                cand["evidence"]["sibling_context"] = sibs
        classifications.sort(key=lambda c: (c["path"], c.get("element_id") or ""))
        # build_up: ordered stack outside-to-inside; proxy order is ascending x
        # (longest-axis ordering arrives with measured region stacks).
        def _x_of(a: dict) -> float:
            try:
                rec = next(r for r in records if r.get("element_id") == a.get("element_id"))
                return float((rec.get("geom") or {}).get("x", 0.0))
            except (StopIteration, TypeError, ValueError):
                return 0.0
        build_up = [{"material": a.get("material"), "thickness": a.get("value"),
                     "qualifier": a.get("qualifier"), "confidence": a.get("confidence"),
                     "element_id": a.get("element_id")}
                    for a in sorted(region_attribs, key=_x_of) if a.get("material")]
        # spec delegation (addendum A.5 test 86): a callout delegating its
        # spec ("REFER ENG. DWGS") marks the detail, it does not describe it.
        spec_delegated = None
        for n in region["record_indexes"]:
            parsed = (records[n].get("parsed") or {})
            if parsed.get("spec_delegated"):
                spec_delegated = {"to": parsed["spec_delegated"]["to"],
                                  "text": records[n].get("text_raw")}
                break
        details.append({
            "detail_id": detail_id_for(drawing, tag),
            "title": title,
            "detail_tag": tag,
            "source_sheet": drawing,
            "scale": region.get("scale"),
            "projection": facets.get("projection"),
            "junction_type": facets.get("junction_type"),
            "assembly": facets.get("assembly"),
            "context": facets.get("context"),
            "facet_nodes": nodes,
            "classifications": classifications,
            "classified_by": facets.get("classified_by", "none"),
            "build_up": build_up,
            "total_thickness": None,
            "components": [],
            "performance": {"fire_rating": None, "acoustic": None, "wet_area": wet},
            "standards": [],
            "depends_on": depends,
            "spec_delegated": spec_delegated,
            "provenance": {"project": None, "firm": None, "year": None, "status": "unknown"},
            "fidelity": "full",
            "reusable": True,
            "thumbnail": None,
            "phash": None,
            "near_duplicates": [],
            "text_blob": "\n".join(texts),
            "sheet_region_bbox": region["bbox"],
            "segmentation": seg["segmentation"],
            "entity_count": len(region["record_indexes"]),
            "element_ids": region["element_ids"],
        })
    # determinism: candidates sorted, normalised strings computed once.
    for cand in quarantine_candidates:
        import re as _re2
        cand["normalised"] = _re2.sub(r"\s+", " ",
                                      str(cand["raw_string"] or "").strip().casefold())
    quarantine_candidates.sort(key=lambda c: (c["detail_id"], c["normalised"],
                                              c.get("element_id") or ""))
    details.sort(key=lambda d: d["detail_tag"])
    # Chain D review rows inherit their detail from element membership, so the
    # panel and confirmations land on the right detail without re-reading.
    by_detail_elements: dict[str, set] = {}
    for region in seg["regions"]:
        by_detail_elements[detail_id_for(drawing, region["tag"])] = \
            set(region["element_ids"])
    dim_review = []
    for rv in ((analysis or {}).get("dim_review", []) if analysis else []):
        rv = dict(rv)
        for did, eids in by_detail_elements.items():
            if rv.get("element_id") in eids:
                rv["detail_id"] = did
                break
        dim_review.append(rv)
    return {"drawing": drawing, "segmentation": seg["segmentation"],
            "uncertain_reason": seg.get("uncertain_reason"), "details": details,
            "quarantine_candidates": quarantine_candidates,
            "dim_review": dim_review}


def details_path_for(details_dir: str | Path, drawing: str) -> Path:
    return Path(details_dir) / (drawing + ".json")


def write_details_file(details_dir: str | Path, drawing: str, payload: dict) -> Path:
    """Write details/<drawing>.json deterministically (sorted keys)."""
    import json
    out = details_path_for(details_dir, drawing)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, sort_keys=True, indent=2, ensure_ascii=False)
        f.write("\n")
    return out
