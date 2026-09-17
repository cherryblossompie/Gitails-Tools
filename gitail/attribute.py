"""7.2 attribution engine (+ 7.4 scale reconciliation, 7.3 references).

Binds numbers to materials through geometry — never by text proximity.
A dimension measures geometry, not the annotation sitting next to it.

Chains:
  A  dimensions: defpoints -> snapped geometry -> region(s) -> material
  B  leaders/callouts: arrow or TEXT+LINE terminal -> region + parsed text
  C  direct measurement: every closed hatched region gets measured anyway

Reconciliation table (confidence):
  leader + dimension + geometry agree ............ high
  leader value in own text (+ geometry agrees)... high
  leader material, geometry measured, no dim .... medium
  dimension resolves, hatch/layer material ...... medium
  leader value vs measured geometry disagree .... low + conflict
  number resolves to no region .................. unattributed
"""
from __future__ import annotations

import math
import re
from pathlib import Path

DEFAULT_GEOMETRY = {
    "region_snap_tolerance": 0.5,
    "region_close_gap_tolerance": 1.0,
    "region_min_area": 1.0,
    "thickness_angle_tolerance": 5.0,
    "conflict_tolerance": 1.0,
    "query_default_tolerance": 0.0,
}


def load_geometry_config(path: str | Path | None) -> dict:
    cfg = dict(DEFAULT_GEOMETRY)
    if not path:
        return cfg
    try:
        import yaml
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        for k in cfg:
            if data.get(k) is not None:
                cfg[k] = float(data[k])
    except Exception:
        pass
    return cfg


# --------------------------------------------------------------------------
# geometry helpers (model mm throughout)


def _poly_area(poly) -> float:
    a = 0.0
    n = len(poly)
    for i in range(n):
        x0, y0 = poly[i]
        x1, y1 = poly[(i + 1) % n]
        a += x0 * y1 - x1 * y0
    return abs(a) / 2.0


def _seg_dist(px, py, ax, ay, bx, by) -> float:
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _point_in_poly(x, y, poly) -> bool:
    inside = False
    n = len(poly)
    for i in range(n):
        x0, y0 = poly[i]
        x1, y1 = poly[(i + 1) % n]
        if ((y0 > y) != (y1 > y)) and (x < (x1 - x0) * (y - y0) / (y1 - y0) + x0):
            inside = not inside
    return inside


def _dist_to_boundary(x, y, poly) -> float:
    d = float("inf")
    n = len(poly)
    for i in range(n):
        x0, y0 = poly[i]
        x1, y1 = poly[(i + 1) % n]
        d = min(d, _seg_dist(x, y, x0, y0, x1, y1))
    return d


def _seg_intersects_poly(ax, ay, bx, by, poly) -> bool:
    def orient(px, py, qx, qy, rx, ry):
        return (qy - py) * (rx - qx) - (qx - px) * (ry - qy)

    def cross(ax, ay, bx, by, cx, cy, dx, dy):
        o1, o2, o3, o4 = orient(ax, ay, bx, by, cx, cy), orient(ax, ay, bx, by, dx, dy), \
            orient(cx, cy, dx, dy, ax, ay), orient(cx, cy, dx, dy, bx, by)
        return ((o1 > 0) != (o2 > 0)) and ((o3 > 0) != (o4 > 0))

    n = len(poly)
    for i in range(n):
        x0, y0 = poly[i]
        x1, y1 = poly[(i + 1) % n]
        if cross(ax, ay, bx, by, x0, y0, x1, y1):
            return True
    return False


def region_thickness(poly) -> float:
    """Minimum width perpendicular to the longest axis (model mm)."""
    best = (0.0, None)
    n = len(poly)
    for i in range(n):
        x0, y0 = poly[i]
        x1, y1 = poly[(i + 1) % n]
        ln = math.hypot(x1 - x0, y1 - y0)
        if ln > best[0]:
            best = (ln, (x1 - x0, y1 - y0))
    if not best[1]:
        return 0.0
    dx, dy = best[1]
    ln = math.hypot(dx, dy)
    nx, ny = -dy / ln, dx / ln
    projs = [x * nx + y * ny for x, y in poly]
    return round(max(projs) - min(projs), 1)


# --------------------------------------------------------------------------
# regions


def _hatch_polygons(doc) -> list[tuple[list, str, str | None]]:
    """(polygon, layer, pattern) from HATCH boundary paths. Never raises."""
    out = []
    try:
        for e in doc.modelspace():
            if e.dxftype() != "HATCH":
                continue
            try:
                layer = str(e.dxf.layer)
            except Exception:
                layer = "0"
            try:
                pattern = str(e.dxf.pattern_name) if e.dxf.pattern_name else None
            except Exception:
                pattern = None
            try:
                paths = list(e.paths)
            except Exception:
                continue
            for p in paths:
                try:
                    verts = [(float(v[0]), float(v[1])) for v in p.vertices]
                except Exception:
                    continue
                if len(verts) >= 3:
                    out.append((verts, layer, pattern))
    except Exception:
        pass
    return out


def _loop_polygons(records, gap_tol) -> list[tuple[list, str]]:
    """Closed loops of LINE/LWPOLYLINE segments grouped by layer."""
    segs_by_layer: dict[str, list] = {}
    for r in records:
        if r.get("type") not in ("LINE", "LWPOLYLINE"):
            continue
        verts = r.get("vertices") or []
        pts = [(float(p[0]), float(p[1])) for p in verts]
        if len(pts) < 2:
            continue
        layer = r.get("layer", "0")
        segs = [(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
        if r.get("type") == "LWPOLYLINE" and len(pts) > 2 and r.get("closed", True):
            segs.append((pts[-1], pts[0]))
        segs_by_layer.setdefault(layer, []).extend(segs)
    loops = []
    for layer, segs in segs_by_layer.items():
        unused = list(segs)
        while unused:
            a, b = unused.pop(0)
            poly = [a, b]
            closed = False
            guard = len(unused) + 1
            while guard > 0:
                guard -= 1
                best, best_i, best_d = None, -1, gap_tol
                for i, (c, d) in enumerate(unused):
                    for end, other in ((c, d), (d, c)):
                        dist = math.hypot(end[0] - poly[-1][0], end[1] - poly[-1][1])
                        if dist <= best_d:
                            best, best_i, best_d = (end, other), i, dist
                if best is None:
                    break
                unused.pop(best_i)
                if math.hypot(best[1][0] - poly[0][0], best[1][1] - poly[0][1]) <= gap_tol:
                    closed = True
                    break
                poly.append(best[0])
                poly.append(best[1])
            if closed and len(poly) >= 3 and _poly_area(poly) >= 0:
                loops.append((poly, layer))
    return loops


def find_regions(doc, records, materials_cfg, geom_cfg) -> list[dict]:
    """Closed regions from hatches, then line loops not already covered."""
    from .semantics import infer_material_from_layer, parse_hatch
    min_area = float(geom_cfg.get("region_min_area", 1.0))
    regions: list[dict] = []
    for poly, layer, pattern in _hatch_polygons(doc):
        if _poly_area(poly) < min_area:
            continue
        mat = parse_hatch(pattern, materials_cfg) or infer_material_from_layer(layer, materials_cfg)
        regions.append(_make_region(poly, layer, pattern, mat))
    for poly, layer in _loop_polygons(records, float(geom_cfg.get("region_close_gap_tolerance", 1.0))):
        if _poly_area(poly) < min_area:
            continue
        cx = sum(p[0] for p in poly) / len(poly)
        cy = sum(p[1] for p in poly) / len(poly)
        if any(_point_in_poly(cx, cy, r["polygon"]) for r in regions):
            continue  # hatch region already covers this loop
        from .semantics import infer_material_from_layer as _infer
        regions.append(_make_region(poly, layer, None, _infer(layer, materials_cfg)))
    regions.sort(key=lambda r: (round(r["centroid"][0], 1), round(r["centroid"][1], 1)))
    for i, r in enumerate(regions):
        r["id"] = f"r_{i:02d}"
    return regions


def _make_region(poly, layer, pattern, material):
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
    return {"id": "", "polygon": [(round(float(x), 1), round(float(y), 1)) for x, y in poly],
            "bbox": [round(min(xs), 1), round(min(ys), 1), round(max(xs), 1), round(max(ys), 1)],
            "centroid": [round(cx, 1), round(cy, 1)],
            "layer": layer, "hatch_pattern": pattern, "material": material,
            "thickness": region_thickness(poly), "source_element_id": None}


# --------------------------------------------------------------------------
# snapping + region resolution


def _record_segments(records, close_gap=None) -> list[tuple[tuple, tuple]]:
    segs = []
    for r in records:
        if r.get("type") not in ("LINE", "LWPOLYLINE"):
            continue
        pts = [(float(p[0]), float(p[1])) for p in (r.get("vertices") or [])]
        for i in range(len(pts) - 1):
            segs.append((pts[i], pts[i + 1]))
        if r.get("type") == "LWPOLYLINE" and len(pts) > 2:
            closed = r.get("closed")
            if closed is None:  # pre-flag state files: geometric fallback
                closed = close_gap is not None and math.hypot(
                    pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1]) <= close_gap
            if closed:
                segs.append((pts[-1], pts[0]))
    return segs


def snap_point(x, y, records, tol, close_gap=None) -> tuple[float, float] | None:
    """Nearest LINE endpoint / polyline vertex, else point on an edge."""
    best, best_d = None, tol
    for r in records:
        if r.get("type") not in ("LINE", "LWPOLYLINE"):
            continue
        for p in (r.get("vertices") or []):
            d = math.hypot(float(p[0]) - x, float(p[1]) - y)
            if d <= best_d:
                best, best_d = (float(p[0]), float(p[1])), d
    if best is not None:
        return best
    for (ax, ay), (bx, by) in _record_segments(records, close_gap):
        d = _seg_dist(x, y, ax, ay, bx, by)
        if d <= tol:
            dx, dy = bx - ax, by - ay
            t = 0.0 if (dx == 0 and dy == 0) else max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / (dx * dx + dy * dy)))
            return (round(ax + t * dx, 1), round(ay + t * dy, 1))
    return None


def resolve_region(x, y, regions, tol) -> dict | None:
    for r in regions:
        if _point_in_poly(x, y, r["polygon"]) or _dist_to_boundary(x, y, r["polygon"]) <= tol:
            return r
    return None


def regions_for_segment(a, b, regions, tol) -> list[dict]:
    """Regions the segment bounds or crosses (endpoint membership + crossings)."""
    hit = []
    for r in regions:
        ain = _point_in_poly(a[0], a[1], r["polygon"]) or _dist_to_boundary(a[0], a[1], r["polygon"]) <= tol
        bin_ = _point_in_poly(b[0], b[1], r["polygon"]) or _dist_to_boundary(b[0], b[1], r["polygon"]) <= tol
        if (ain and bin_) or _seg_intersects_poly(a[0], a[1], b[0], b[1], r["polygon"]):
            hit.append(r)
    return hit


# --------------------------------------------------------------------------
# 7.3 cross-references + wet-area flags


def extract_references(text: str | None) -> list[str]:
    """Drawing numbers like A.109 out of 'REFER. A.109 + A.110 ...'."""
    if not text:
        return []
    found = re.findall(r"\b([A-Z]{1,3}\.\d{2,4}(?:/\d{2,4})?)\b", str(text).upper())
    seen, out = set(), []
    for f in found:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


def wet_area_flag(texts: list[str]) -> bool:
    return any("wet area" in (t or "").lower() for t in texts)


# --------------------------------------------------------------------------
# 7.4 scale reconciliation


def reconcile_scale(dim_infos: list[dict]) -> tuple[float, bool]:
    """displayed-vs-measured consensus. (factor, conflict)."""
    ratios = []
    for d in dim_infos:
        displayed, measured = d.get("displayed"), d.get("measured")
        if displayed and measured:
            ratios.append(displayed / measured)
    if not ratios:
        return 1.0, False
    ratios.sort()
    med = ratios[len(ratios) // 2]
    if all(abs(r - med) <= max(0.01 * abs(med), 1e-9) for r in ratios):
        return (round(med, 4) if abs(med - 1.0) > 1e-9 else 1.0), False
    return 1.0, True


# --------------------------------------------------------------------------
# chains


def _num(text: str | None):
    if not text:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)", str(text).replace(",", ""))
    return float(m.group(1)) if m else None


def attribute_dimensions(records, regions, materials_cfg, geom_cfg, scale_factor=1.0) -> tuple[list, list, list]:
    """Chain A. Returns (bound, assembly, unattributed)."""
    tol = float(geom_cfg.get("region_snap_tolerance", 0.5))
    gap = float(geom_cfg.get("region_close_gap_tolerance", 1.0))
    bound, assembly, unattributed = [], [], []
    for r in records:
        if r.get("type") != "DIMENSION":
            continue
        displayed = _num(r.get("dim_override")) if r.get("dim_override") else None
        if displayed is None and r.get("dim_measurement") is not None:
            displayed = float(r["dim_measurement"])
        if displayed is None:
            continue
        value = round(displayed * scale_factor, 2)
        pts = [p for p in (r.get("dim_defpoints") or []) if p]
        # measured pair first (defpoint2, defpoint3), defpoint last
        ordered = ([pts[1], pts[2]] if len(pts) >= 3 else []) + pts
        snapped = []
        for p in ordered:
            s = snap_point(float(p[0]), float(p[1]), records, tol, gap)
            if s is not None and s not in snapped:
                snapped.append(s)
            if len(snapped) == 2:
                break
        if len(snapped) < 2:
            unattributed.append({"element_id": r.get("element_id"), "dxf_handle": r.get("dxf_handle"),
                                 "raw_value": value, "unit": "mm", "reason": "defpoints snap to no drawn geometry"})
            continue
        a, b = snapped
        involved = regions_for_segment(a, b, regions, tol)
        if len(involved) == 1:
            reg = involved[0]
            if reg.get("material"):
                bound.append({"element_id": r.get("element_id"), "region_id": reg["id"],
                              "material": reg["material"], "part": None, "value": value, "unit": "mm",
                              "qualifier": None, "confidence": "medium", "attribution_chain": "dimension",
                              "conflict": False, "exact_value": value})
            else:
                unattributed.append({"element_id": r.get("element_id"), "dxf_handle": r.get("dxf_handle"),
                                     "raw_value": value, "unit": "mm", "reason": "region has no material"})
        elif len(involved) > 1:
            assembly.append({"element_id": r.get("element_id"), "value": value, "unit": "mm",
                             "regions": [g["id"] for g in involved], "kind": "assembly"})
        else:
            unattributed.append({"element_id": r.get("element_id"), "dxf_handle": r.get("dxf_handle"),
                                 "raw_value": value, "unit": "mm", "reason": "defpoints resolve to no region"})
    return bound, assembly, unattributed


def _leader_terminal(rec, doc, records, regions, tol, geom_cfg=None):
    """MULTILEADER arrow endpoint, else TEXT+LINE pair terminal vertex.

    A LINE only counts as this text's leader when it starts near the text
    insertion — otherwise any line in the detail would capture any callout,
    the exact proximity error attribution exists to prevent.
    """
    pair_tol = float((geom_cfg or {}).get("leader_pair_tolerance", 25.0))
    if rec.get("type") == "MULTILEADER" and doc is not None:
        try:
            for e in doc.modelspace():
                if e.dxftype() == "MULTILEADER" and str(e.dxf.handle) == rec.get("dxf_handle"):
                    ctx = e.context
                    leaders = getattr(ctx, "leaders", None) or []
                    if leaders:
                        pts = getattr(leaders[0], "vertices", None) or []
                        if pts:
                            return (round(float(pts[-1].x), 1), round(float(pts[-1].y), 1))
        except Exception:
            pass
    if rec.get("type") in ("TEXT", "MTEXT"):
        ins = (rec.get("insert_x"), rec.get("insert_y"))
        best, best_d = None, pair_tol
        for lr in records:
            if lr.get("type") != "LINE" or lr is rec:
                continue
            verts = [(float(p[0]), float(p[1])) for p in (lr.get("vertices") or [])]
            if len(verts) < 2:
                continue
            for i, end in enumerate((verts[0], verts[-1])):
                if ins[0] is None:
                    continue
                # the leader must start at the text; the other end is terminal
                if math.hypot(end[0] - (ins[0] or 0), end[1] - (ins[1] or 0)) > pair_tol:
                    continue
                terminal = verts[-1] if i == 0 else verts[0]
                d = math.hypot(end[0] - (ins[0] or 0), end[1] - (ins[1] or 0))
                if d < best_d:
                    best, best_d = terminal, d
        return best
    return None


def attribute_leaders(records, doc, regions, materials_cfg, geom_cfg) -> list:
    """Chain B. Value-in-own-text binds high; material-only binds medium."""
    tol = float(geom_cfg.get("region_snap_tolerance", 0.5))
    ctol = float(geom_cfg.get("conflict_tolerance", 1.0))
    out = []
    for r in records:
        if r.get("type") not in ("TEXT", "MTEXT", "MULTILEADER", "PDFTEXT"):
            continue
        parsed = r.get("parsed") or {}
        if not parsed.get("material"):
            continue
        end = _leader_terminal(r, doc, records, regions, tol, geom_cfg)
        reg = resolve_region(end[0], end[1], regions, tol) if end else None
        base = {"element_id": r.get("element_id"), "region_id": reg["id"] if reg else None,
                "material": parsed["material"], "part": parsed.get("part"),
                "unit": parsed.get("unit", "mm"), "qualifier": parsed.get("qualifier")}
        if parsed.get("value") is not None:
            v = float(parsed["value"])
            if reg and abs(reg["thickness"] - v) <= ctol:
                base.update(value=v, exact_value=v, confidence="high",
                            attribution_chain="leader+geometry", conflict=False)
            elif reg:
                base.update(value=v, exact_value=v, confidence="low",
                            attribution_chain="leader+geometry", conflict=True)
            else:
                base.update(value=v, exact_value=v, confidence="high",
                            attribution_chain="leader", conflict=False)
            out.append(base)
        else:
            if reg and reg.get("thickness"):
                base.update(value=reg["thickness"], exact_value=reg["thickness"], confidence="medium",
                            attribution_chain="leader+geometry", conflict=False)
            else:
                base.update(value=None, exact_value=None, confidence="medium",
                            attribution_chain="leader", conflict=False)
            out.append(base)
    return out


def measure_regions(regions) -> list:
    """Chain C: region thickness + hatch/layer material binds at medium."""
    out = []
    for reg in regions:
        if reg.get("material") and reg.get("thickness"):
            out.append({"element_id": reg.get("source_element_id"), "region_id": reg["id"],
                        "material": reg["material"], "part": None,
                        "value": reg["thickness"], "unit": "mm", "qualifier": None,
                        "confidence": "medium", "attribution_chain": "geometry",
                        "conflict": False, "exact_value": reg["thickness"]})
    return out


# --------------------------------------------------------------------------
# reconcile + entry points


def _chain_rank(chain: str) -> int:
    order = {"leader+dimension+geometry": 0, "leader+geometry": 1, "leader": 2,
             "dimension": 3, "geometry": 4}
    return order.get(chain, 9)


def reconcile(evidence: list) -> list:
    """Merge per (region, material). Own-text values win; conflicts downgrade."""
    grouped: dict[tuple, list] = {}
    for e in evidence:
        # Region-less leader evidence is per-element: two callouts naming the
        # same material must not collapse into one row.
        key = (e.get("region_id"), e.get("material")) if e.get("region_id") is not None \
            else (None, e.get("material"), e.get("element_id"))
        grouped.setdefault(key, []).append(e)
    out = []
    for _key, items in grouped.items():
        chains = sorted({e["attribution_chain"] for e in items}, key=_chain_rank)
        has_leader = any(c.startswith("leader") for c in chains)
        own = [e for e in items if e.get("leader_own_value")]
        dims = [e for e in items if e["attribution_chain"] == "dimension"]
        geos = [e for e in items if e["attribution_chain"] == "geometry"]
        base = dict(min(items, key=lambda e: _chain_rank(e["attribution_chain"])))
        conflict = any(e.get("conflict") for e in items)
        if own:
            o = own[0]
            base.update(value=o["value"], exact_value=o.get("exact_value", o["value"]),
                        qualifier=o.get("qualifier"), part=o.get("part") or base.get("part"),
                        element_id=o.get("element_id") or base.get("element_id"),
                        attribution_chain="+".join(chains))
        elif dims and geos and abs(dims[0]["value"] - geos[0]["value"]) <= 0.15:
            dv = dims[0]
            base.update(value=dv["value"], exact_value=dv["value"],
                        element_id=dv.get("element_id") or base.get("element_id"))
            base["attribution_chain"] = ("leader+dimension+geometry" if has_leader
                                         else "dimension+geometry")
        elif has_leader and geos:
            base.update(value=geos[0]["value"], exact_value=geos[0]["value"])
            base["attribution_chain"] = "leader+geometry"
        if conflict:
            base["confidence"] = "low"
            base["conflict"] = True
        elif own or (dims and geos):
            base["confidence"] = "high"
            base["conflict"] = False
        out.append(base)
    return out


def analyze_records(records, doc, materials_cfg, geom_cfg) -> dict:
    """Full 7.2 pass over extract records (+ open doc for hatches/leaders)."""
    geom_cfg = {**DEFAULT_GEOMETRY, **(geom_cfg or {})}
    regions = find_regions(doc, records, materials_cfg, geom_cfg) if doc is not None else []
    by_handle = {}
    if doc is not None:
        try:
            for e in doc.modelspace():
                try:
                    by_handle[str(e.dxf.handle)] = e
                except Exception:
                    pass
        except Exception:
            pass
    for r in regions:
        # attach source element: hatch record with same layer+pattern wins
        for rec in records:
            if rec.get("hatch_pattern") == r["hatch_pattern"] and rec.get("layer") == r["layer"] \
                    and r["hatch_pattern"]:
                r["source_element_id"] = rec.get("element_id")
                break
        else:
            for rec in records:
                if rec.get("type") == "LWPOLYLINE" and rec.get("layer") == r["layer"]:
                    r["source_element_id"] = rec.get("element_id")
                    break
    # scale from dimensions
    dim_infos = []
    for r in records:
        if r.get("type") != "DIMENSION":
            continue
        disp = _num(r.get("dim_override")) if r.get("dim_override") else None
        if disp is None and r.get("dim_measurement") is not None:
            disp = float(r["dim_measurement"])
        pts = r.get("dim_defpoints") or []
        geo = None
        if len(pts) >= 3:
            geo = math.hypot(float(pts[1][0]) - float(pts[2][0]), float(pts[1][1]) - float(pts[2][1]))
        elif r.get("dim_measurement") is not None:
            geo = float(r["dim_measurement"])
        dim_infos.append({"displayed": disp, "measured": geo})
    scale_factor, scale_conflict = reconcile_scale(dim_infos)
    # mark leader-own-value evidence for reconcile
    leader_ev = attribute_leaders(records, doc, regions, materials_cfg, geom_cfg)
    for e in leader_ev:
        rec = next((x for x in records if x.get("element_id") == e.get("element_id")), None)
        parsed = (rec or {}).get("parsed") or {}
        e["leader_own_value"] = parsed.get("value") is not None and e.get("value") == parsed.get("value")
    dim_bound, assemblies, dim_unattr = attribute_dimensions(
        records, regions, materials_cfg, geom_cfg, scale_factor)
    geo_ev = measure_regions(regions)
    attributions = reconcile(leader_ev + dim_bound + geo_ev)
    # unattributed: dimension leftovers + loose numbers nowhere
    unattributed = list(dim_unattr)
    texts = [r.get("text_raw") for r in records if r.get("text_raw")]
    texts += [r.get("dim_override") for r in records if r.get("dim_override")]
    return {"regions": regions, "attributions": attributions, "assemblies": assemblies,
            "unattributed": unattributed, "wet_area": wet_area_flag(texts),
            "depends_on": extract_references(" ".join(t for t in texts if t)),
            "scale_factor": scale_factor, "scale_conflict": scale_conflict}


def analyze_doc(doc, materials_cfg, geometry_cfg) -> dict:
    from .extract import extract_state_from_doc
    records = extract_state_from_doc(doc, materials_cfg)
    return analyze_records(records, doc, materials_cfg, geometry_cfg)


def analyze_dxf(dxf_path: str | Path, materials_cfg: dict, geometry_cfg: dict) -> dict:
    import ezdxf
    doc = ezdxf.readfile(str(dxf_path))
    return analyze_doc(doc, materials_cfg, geometry_cfg)
