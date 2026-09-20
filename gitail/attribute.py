"""7.2 attribution engine (+ 7.4 scale reconciliation, 7.3 references,
addendum B: Chain A2 extension-line tracing + Chain D vision-assisted).

Binds numbers to materials through geometry — never by text proximity.
A dimension measures geometry, not the annotation sitting next to it.

Chains (addendum B.4 precedence):
  B   leader + region                high     names the material outright
  A   dimension defpoints            high     where defpoints are sound
  A2  extension-line tracing         high     corroborated by arithmetic
  C   direct geometry measurement    medium   no dimension needed
  D   vision-assisted                low      flagged, review required
      unattributed                   —        stored, excluded from search

Run all applicable chains and reconcile per 7.2. Where two chains disagree
beyond tolerance, record conflict: true and take the higher-precedence
chain's answer at reduced confidence.
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
    # Chain A2 extension-line tracing (addendum B.2/B.5).
    "extension_probe_mm": 3.0,
    "extension_parallel_tolerance_deg": 2.0,
    "dimension_group_cluster_mm": 15.0,
    "chain_d_candidate_radius_mm": 60.0,
    "chain_d_enabled": True,
    # Arithmetic corroboration: struck-edge separation must equal the stated
    # measurement within this (mm), or the trace is rejected, not guessed at.
    "value_conflict_tolerance_mm": 1.0,
}


def load_geometry_config(path: str | Path | None) -> dict:
    cfg = dict(DEFAULT_GEOMETRY)
    if not path:
        return cfg
    try:
        import yaml
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        for k, default in cfg.items():
            if data.get(k) is not None:
                cfg[k] = data[k] if isinstance(default, bool) else float(data[k])
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
                              "qualifier": None, "measure": "thickness",
                              "confidence": "medium", "attribution_chain": "dimension",
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
    # Reassembled annotations carry their resolved leader endpoint (signal 2):
    # use it directly instead of re-deriving proximity pairs.
    try:
        lep = rec.get("leader_endpoint")
        if lep is not None:
            return (round(float(lep[0]), 1), round(float(lep[1]), 1))
    except (TypeError, ValueError, IndexError):
        pass
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
        if r.get("type") not in ("TEXT", "MTEXT", "MULTILEADER", "PDFTEXT", "ANNOTATION"):
            continue
        parsed = r.get("parsed") or {}
        # Addendum A.5: measure-only annotations (setdown/fall with a value
        # but no material word, e.g. "NOM. 50MM SLAB SETDOWN") still bind
        # Chain B so the fact indexes under its measure, never thickness.
        if not parsed.get("material") and not (
                parsed.get("measure") not in (None, "thickness")
                and parsed.get("value") is not None):
            continue
        end = _leader_terminal(r, doc, records, regions, tol, geom_cfg)
        reg = resolve_region(end[0], end[1], regions, tol) if end else None
        base = {"element_id": r.get("element_id"), "region_id": reg["id"] if reg else None,
                "material": parsed.get("material"), "part": parsed.get("part"),
                "unit": parsed.get("unit", "mm"), "qualifier": parsed.get("qualifier"),
                "measure": parsed.get("measure") or "thickness"}
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
                        "measure": "thickness",
                        "confidence": "medium", "attribution_chain": "geometry",
                        "conflict": False, "exact_value": reg["thickness"]})
    return out


# --------------------------------------------------------------------------
# Chain A2 — extension-line tracing (addendum B.2, deterministic).
#
# A dimension is drawn as a dimension line, terminators, two extension lines
# running from the dimension line back to the measured points, and a label.
# The extension lines physically point at what is measured: take each
# extension line's FAR endpoint (away from the dimension line), extend the ray
# beyond it, and strike the first geometry. The struck edges are the measured
# boundaries — corroborated by arithmetic (struck separation must equal the
# stated value) or the trace is rejected, never guessed at.


def _seg_angle_deg(ax, ay, bx, by) -> float:
    return math.degrees(math.atan2(by - ay, bx - ax)) % 180.0


def _angle_diff_deg(a: float, b: float) -> float:
    d = abs((a - b) % 180.0)
    return min(d, 180.0 - d)


def _ray_seg_t(ox, oy, dx, dy, ax, ay, bx, by) -> float | None:
    """Forward distance t along unit (dx,dy) to segment ab; None on parallel,
    miss, or behind origin. Includes grazing/collinear overlap as t=origin."""
    ex, ey = bx - ax, by - ay
    denom = dx * ey - dy * ex
    if abs(denom) < 1e-9:
        # parallel: strike only on collinear overlap (extension lines drawn
        # exactly along a region edge, e.g. generated dimensions)
        if abs((ax - ox) * dy - (ay - oy) * dx) > 1e-6:
            return None
        t0 = (ax - ox) * dx + (ay - oy) * dy
        t1 = (bx - ox) * dx + (by - oy) * dy
        if max(t0, t1) < 0:
            return None
        return max(0.0, min(t0, t1))
    t = ((ax - ox) * ey - (ay - oy) * ex) / denom
    u = ((ax - ox) * dy - (ay - oy) * dx) / denom
    if t < 0 or not (0.0 <= u <= 1.0):
        return None
    return t


def _trace_segments(records) -> list[tuple[tuple, tuple]]:
    """All drawable segments incl. PDF paths (Chain A2 runs on both)."""
    segs = list(_record_segments(records))
    for r in records:
        if r.get("type") != "PDFPATH":
            continue
        try:
            pts = [(float(p[0]), float(p[1])) for p in (r.get("vertices") or [])]
        except (TypeError, ValueError):
            continue
        for i in range(len(pts) - 1):
            segs.append((pts[i], pts[i + 1]))
    return segs


def _region_edges(regions) -> list[tuple[tuple, tuple]]:
    edges = []
    for reg in regions:
        poly = reg.get("polygon") or []
        for i in range(len(poly)):
            try:
                edges.append(((float(poly[i][0]), float(poly[i][1])),
                              (float(poly[(i + 1) % len(poly)][0]),
                               float(poly[(i + 1) % len(poly)][1]))))
            except (TypeError, ValueError, IndexError):
                continue
    return edges


def _strike_first(ox, oy, dx, dy, records, regions, max_dist,
                  min_dist: float = 0.15) -> tuple[float, float] | None:
    """Nearest geometry struck by the ray within (min_dist, max_dist]."""
    best, best_t = None, None
    edges = _trace_segments(records) + _region_edges(regions)
    ln = math.hypot(dx, dy) or 1.0
    ux, uy = dx / ln, dy / ln
    for (ax, ay), (bx, by) in edges:
        t = _ray_seg_t(ox, oy, ux, uy, ax, ay, bx, by)
        if t is None:
            continue
        if t <= min_dist:
            # At-origin strike: the ray starts ON drawn geometry (the measured
            # edge itself, or the extension line overlapping it). That is the
            # measured boundary, not a miss (test 97).
            if t < 0.05:
                return (round(ox, 1), round(oy, 1))
            continue
        if t > max_dist:
            continue
        if best_t is None or t < best_t:
            best_t = t
            best = (round(ox + ux * t, 1), round(oy + uy * t, 1))
    return best


def _extension_lines_near(pt, axis_deg, records, radius, par_tol_deg):
    """Segments passing near pt and near-perpendicular to the measurement
    axis. Returns [(seg, far_end)]: far end = the endpoint nearer pt (the
    extension line starts with a drafting gap off the measured point, so the
    near end is the far-from-dimension-line end)."""
    found = []
    for (ax, ay), (bx, by) in _trace_segments(records):
        if math.hypot((ax + bx) / 2 - pt[0], (ay + by) / 2 - pt[1]) > radius + \
                math.hypot(bx - ax, by - ay) / 2:
            # cheap reject: midpoint farther than radius + half length
            d0 = math.hypot(ax - pt[0], ay - pt[1])
            d1 = math.hypot(bx - pt[0], by - pt[1])
            if min(d0, d1) > radius:
                continue
        ang = _seg_angle_deg(ax, ay, bx, by)
        if abs(_angle_diff_deg(ang, axis_deg) - 90.0) > par_tol_deg:
            continue
        d0 = math.hypot(ax - pt[0], ay - pt[1])
        d1 = math.hypot(bx - pt[0], by - pt[1])
        if min(d0, d1) > radius:
            continue
        far = (ax, ay) if d0 <= d1 else (bx, by)
        near_end = (bx, by) if d0 <= d1 else (ax, ay)
        found.append((((ax, ay), (bx, by)), far, near_end))
    found.sort(key=lambda item: math.hypot(item[1][0] - pt[0], item[1][1] - pt[1]))
    return found


def _dim_stated_value(rec, scale_factor: float = 1.0) -> float | None:
    displayed = _num(rec.get("dim_override")) if rec.get("dim_override") else None
    if displayed is None and rec.get("dim_measurement") is not None:
        try:
            displayed = float(rec["dim_measurement"])
        except (TypeError, ValueError):
            return None
    if displayed is None:
        return None
    return round(displayed * scale_factor, 2)


def _dim_anchor(rec) -> tuple[float, float] | None:
    try:
        g = rec.get("geom") or {}
        return (float(g["x"]), float(g["y"]))
    except (TypeError, ValueError, KeyError):
        return None


def trace_dimension(value: float, p2, p3, records, regions, geom_cfg,
                    element_id=None, label: str | None = None) -> tuple[dict | None, dict]:
    """One A2 trace. Returns (bound|None, dim_candidate).

    bound on corroborated strike (high, extension_trace); otherwise a Chain D
    candidate carrying arithmetically-consistent regions (contradicted regions
    are excluded before any model sees them — addendum E).
    """
    probe = float(geom_cfg.get("extension_probe_mm", 3.0))
    par_tol = float(geom_cfg.get("extension_parallel_tolerance_deg", 2.0))
    vtol = float(geom_cfg.get("value_conflict_tolerance_mm", 1.0))
    radius = float(geom_cfg.get("chain_d_candidate_radius_mm", 60.0))
    p2 = (float(p2[0]), float(p2[1]))
    p3 = (float(p3[0]), float(p3[1]))
    axis = _seg_angle_deg(p2[0], p2[1], p3[0], p3[1])
    axux, axuy = math.cos(math.radians(axis)), math.sin(math.radians(axis))
    anchor = ((p2[0] + p3[0]) / 2.0, (p2[1] + p3[1]) / 2.0)
    assoc = max(probe, 1.0)
    ext2 = _extension_lines_near(p2, axis, records, assoc, par_tol)
    ext3 = _extension_lines_near(p3, axis, records, assoc, par_tol)
    if (not ext2 or not ext3) and len(records) < 10000:
        # Stale defpoints (B.1 case 3): the points sit in space while the
        # drawn extension lines remain. Search a wider net around the
        # defpoint midpoint for a parallel pair of extension lines.
        wide = max(assoc * 8.0, 12.0)
        cands = []
        for (ax, ay), (bx, by) in _trace_segments(records):
            ang = _seg_angle_deg(ax, ay, bx, by)
            if abs(_angle_diff_deg(ang, axis) - 90.0) > par_tol:
                continue
            mx, my = (ax + bx) / 2.0, (ay + by) / 2.0
            d = math.hypot(mx - anchor[0], my - anchor[1])
            if d <= wide:
                cands.append((((ax, ay), (bx, by)), d))
        cands.sort(key=lambda t: t[1])
        if len(cands) >= 2:
            (s1, _), (s2, _) = cands[0], cands[1]
            proj = lambda s: ((s[0][0] + s[1][0]) / 2.0 * axux
                              + (s[0][1] + s[1][1]) / 2.0 * axuy)
            if abs(proj(s1) - proj(s2)) > 0.5:
                def _ends(seg):
                    (ax, ay), (bx, by) = seg
                    d0 = math.hypot(ax - anchor[0], ay - anchor[1])
                    d1 = math.hypot(bx - anchor[0], by - anchor[1])
                    return (seg, (ax, ay), (bx, by)) if d0 <= d1 else (seg, (bx, by), (ax, ay))
                ext2, ext3 = [_ends(s1)], [_ends(s2)]
    candidate = {"element_id": element_id, "value": value, "unit": "mm",
                 "label": label, "anchor": [round(anchor[0], 1), round(anchor[1], 1)],
                 "regions": []}
    bound = None
    strikes = []
    if ext2 and ext3:
        for (seg, far, near_end) in (ext2[0], ext3[0]):
            dx, dy = far[0] - near_end[0], far[1] - near_end[1]
            if math.hypot(dx, dy) < 1e-9:
                break
            hit = _strike_first(far[0], far[1], dx, dy, records, regions, probe)
            if hit is None:
                break
            strikes.append(hit)
        else:
            sep = abs((strikes[0][0] - strikes[1][0]) * axux
                      + (strikes[0][1] - strikes[1][1]) * axuy)
            if abs(sep - value) <= vtol:
                mid = ((strikes[0][0] + strikes[1][0]) / 2.0,
                       (strikes[0][1] + strikes[1][1]) / 2.0)
                tol = float(geom_cfg.get("region_snap_tolerance", 0.5))
                reg = resolve_region(mid[0], mid[1], regions, tol)
                if reg is not None and reg.get("material"):
                    bound = {
                        "element_id": element_id, "region_id": reg["id"],
                        "material": reg["material"], "part": None,
                        "value": value, "unit": "mm", "qualifier": None,
                        "measure": "thickness", "confidence": "high"
                        if reg.get("hatch_pattern") else "medium",
                        "attribution_chain": "extension_trace", "conflict": False,
                        "exact_value": value}
    # Chain D candidate regions: within radius AND arithmetically consistent.
    # A region whose measured width contradicts the dimension never reaches
    # the model (addendum E) — whatever Chain D is, it chooses among these.
    for reg in regions:
        try:
            c = reg.get("centroid") or [0, 0]
            d = math.hypot(float(c[0]) - anchor[0], float(c[1]) - anchor[1])
        except (TypeError, ValueError):
            continue
        if d > radius:
            continue
        try:
            w = float(reg.get("thickness") or 0)
        except (TypeError, ValueError):
            continue
        if abs(w - value) > vtol:
            continue
        candidate["regions"].append({
            "region_id": reg["id"], "measured_width": reg.get("thickness"),
            "material": reg.get("material"), "layer": reg.get("layer"),
            "polygon": [list(p) for p in (reg.get("polygon") or [])]})
    candidate["regions"].sort(key=lambda r: r["region_id"])
    return bound, candidate


def find_exploded_dimensions(records, geom_cfg) -> list[dict]:
    """Cluster exploded dimensions (test 99): no DIMENSION entity — a bare
    numeric label, a short dimension line with terminators, and two roughly
    parallel extension lines meeting it near-perpendicular at its ends."""
    cluster_mm = float(geom_cfg.get("dimension_group_cluster_mm", 15.0))
    par_tol = float(geom_cfg.get("extension_parallel_tolerance_deg", 2.0))
    labels = []
    for r in records:
        if r.get("type") not in ("TEXT", "MTEXT", "MULTILEADER", "PDFTEXT", "ANNOTATION"):
            continue
        text = (r.get("text_raw") or "").strip()
        m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*", text)
        if not m:
            continue
        anchor = _dim_anchor(r)
        if anchor is None:
            continue
        labels.append({"rec": r, "value": float(m.group(1)), "anchor": anchor})
    dim_segs = []
    for (a, b) in _trace_segments(records):
        ln = math.hypot(b[0] - a[0], b[1] - a[1])
        if 2.0 <= ln <= 500.0:
            dim_segs.append((a, b))
    used, out = set(), []
    for lab in labels:
        best, best_d = None, cluster_mm
        for i, (a, b) in enumerate(dim_segs):
            if i in used:
                continue
            mx, my = (a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0
            d = math.hypot(mx - lab["anchor"][0], my - lab["anchor"][1])
            if d < best_d:
                best, best_d = (a, b, i), d
        if best is None:
            continue
        (ax, ay), (bx, by), si = best
        axis = _seg_angle_deg(ax, ay, bx, by)
        nx, ny = -math.sin(math.radians(axis)), math.cos(math.radians(axis))
        ends = []
        for (cx, cy), (dx_, dy_) in _trace_segments(records):
            ang = _seg_angle_deg(cx, cy, dx_, dy_)
            if abs(_angle_diff_deg(ang, axis) - 90.0) > par_tol:
                continue
            # meets the dim line near one of its ends
            d_end = min(_seg_dist(ax, ay, cx, cy, dx_, dy_),
                        _seg_dist(bx, by, cx, cy, dx_, dy_))
            if d_end > 2.0:
                continue
            mx, my = (cx + dx_) / 2.0, (cy + dy_) / 2.0
            side = (mx - ax) * nx + (my - ay) * ny
            far = (cx, cy) if abs((cx - ax) * nx + (cy - ay) * ny) > \
                abs((dx_ - ax) * nx + (dy_ - ay) * ny) else (dx_, dy_)
            ends.append({"seg": ((cx, cy), (dx_, dy_)), "far": far, "side": side})
        if len(ends) < 2:
            continue
        # one extension line per dim-line end, both running to the same side
        by_end = {}
        for e in ends:
            d0 = math.hypot(e["seg"][0][0] - ax, e["seg"][0][1] - ay) + \
                math.hypot(e["seg"][1][0] - ax, e["seg"][1][1] - ay)
            d1 = math.hypot(e["seg"][0][0] - bx, e["seg"][0][1] - by) + \
                math.hypot(e["seg"][1][0] - bx, e["seg"][1][1] - by)
            key = "a" if d0 <= d1 else "b"
            if key not in by_end:
                by_end[key] = e
        if set(by_end) != {"a", "b"}:
            continue
        if by_end["a"]["side"] * by_end["b"]["side"] < 0:
            continue  # extension lines must run to the same side
        used.add(si)
        out.append({"label_rec": lab["rec"], "value": lab["value"],
                    "dim_seg": ((ax, ay), (bx, by)),
                    "ext": [by_end["a"], by_end["b"]],
                    "anchor": lab["anchor"]})
    return out


def attribute_a2(records, regions, geom_cfg, scale_factor: float = 1.0,
                 skip_ids: set | None = None) -> tuple[list, list, list]:
    """Chain A2 over Chain-A-unattributed entity dims + exploded clusters.

    Returns (bound, dim_candidates, rejected_notes). dim_candidates feed
    Chain D (or unattributed-with-candidates when Chain D cannot run).
    """
    skip_ids = skip_ids or set()
    bound, candidates, rejected = [], [], []
    seen_labels: set[str] = set()
    for r in records:
        if r.get("type") != "DIMENSION":
            continue
        if r.get("element_id") in skip_ids:
            continue  # Chain A already bound this dimension
        value = _dim_stated_value(r, scale_factor)
        if value is None:
            continue
        pts = [p for p in (r.get("dim_defpoints") or []) if p]
        if len(pts) < 2:
            rejected.append({"element_id": r.get("element_id"),
                             "reason": "dimension has no usable defpoints"})
            continue
        ordered = ([pts[1], pts[2]] if len(pts) >= 3 else []) + pts
        p2 = (float(ordered[0][0]), float(ordered[0][1]))
        p3 = (float(ordered[1][0]), float(ordered[1][1]))
        label = r.get("dim_override") or None
        b, cand = trace_dimension(value, p2, p3, records, regions, geom_cfg,
                                  element_id=r.get("element_id"), label=label)
        if b is not None:
            bound.append(b)
        else:
            candidates.append(cand)
    for g in find_exploded_dimensions(records, geom_cfg):
        lab_rec = g["label_rec"]
        if lab_rec.get("element_id") in seen_labels:
            continue
        seen_labels.add(lab_rec.get("element_id"))
        fars = [g["ext"][0]["far"], g["ext"][1]["far"]]
        value = round(g["value"] * scale_factor, 2)
        b, cand = trace_dimension(value, fars[0], fars[1], records, regions,
                                  geom_cfg, element_id=lab_rec.get("element_id"),
                                  label=(lab_rec.get("text_raw") or "").strip())
        # exploded corroboration compares against the SCALED stated value
        if b is not None:
            bound.append(b)
        else:
            candidates.append(cand)
    return bound, candidates, rejected


# --------------------------------------------------------------------------
# Chain D — vision-assisted attribution (addendum B.3, last resort).


def dim_review_uid(drawing: str, element_id: str | None, value: float) -> str:
    """Stable id per (drawing, element, value): a revision changing the value
    re-opens review; reindexing the same state collides onto one row."""
    import hashlib
    base = "|".join([drawing or "", element_id or "", str(value)])
    return "vd_" + hashlib.sha1(base.encode("utf-8")).hexdigest()[:6]


def run_chain_d(dim_candidates, regions, geom_cfg, adapter=None,
                adapter_enabled: bool = False, budget_left=None,
                crop_fn=None, drawing: str = "") -> tuple[list, list]:
    """Chain D over unresolved dimension candidates. Returns (attributions,
    dim_review_rows). Confidence is capped at LOW whatever the model reports
    (test 102); with the adapter disabled nothing is called and everything
    stays unattributed (test 107)."""
    attributions, reviews = [], []
    if not dim_candidates:
        return attributions, reviews
    for cand in dim_candidates:
        uid = dim_review_uid(drawing, cand.get("element_id"), cand.get("value"))
        overlays = [{"polygon": r.get("polygon"), "label": str(i + 1)}
                    for i, r in enumerate(cand.get("regions", []))
                    if r.get("polygon")]
        review = {"uid": uid, "drawing": drawing,
                  "element_id": cand.get("element_id"),
                  "detail_id": None, "value": cand.get("value"),
                  "unit": cand.get("unit", "mm"),
                  "label": cand.get("label"),
                  "anchor": cand.get("anchor"),
                  "regions": [{k: r.get(k) for k in
                               ("region_id", "measured_width", "material", "layer")}
                              for r in cand.get("regions", [])],
                  "overlays": overlays,
                  "crop": None, "status": "pending",
                  "material": None, "region_id": None,
                  "chain": "vision", "confidence": "low"}
        if not cand.get("regions"):
            reviews.append(review)  # nothing arithmetic allows: stays pending
            continue
        if adapter is None or not adapter_enabled or (
                budget_left is not None and not budget_left()):
            reviews.append(review)
            continue
        from .ai import RegionSuggestion, RegionSummary, validate_region_ranking
        summaries = [RegionSummary(region_id=r["region_id"],
                                   measured_width=float(r["measured_width"] or 0),
                                   material=r.get("material"),
                                   layer=r.get("layer"))
                     for r in cand["regions"]]
        crop = None
        if crop_fn is not None:
            try:
                crop = crop_fn(cand["anchor"], cand["regions"])
            except Exception:
                crop = None
        try:
            raw = adapter.attribute_dimension(
                crop, float(cand["value"]), cand.get("label") or "",
                summaries)
            ranked = validate_region_ranking(
                raw, [r.region_id for r in summaries])
        except Exception as ex:
            import logging
            logging.getLogger("gitail.attribution").warning(
                "attribute_dimension discarded (%s); dimension stays unattributed", ex)
            reviews.append(review)
            continue
        top = ranked[0]
        chosen = next(r for r in cand["regions"] if r["region_id"] == top.region_id)
        if not chosen.get("material"):
            reviews.append(review)
            continue
        review.update(material=chosen["material"], region_id=chosen["region_id"],
                      crop=None)  # crop bytes stay out of the index; file path set at extract
        reviews.append(review)
        attributions.append({
            "element_id": cand.get("element_id"), "region_id": chosen["region_id"],
            "material": chosen["material"], "part": None,
            "value": cand.get("value"), "unit": cand.get("unit", "mm"),
            "qualifier": None, "measure": "thickness",
            "confidence": "low", "attribution_chain": "vision",
            "conflict": False, "exact_value": cand.get("value"),
            "review_needed": True, "dim_uid": uid})
    return attributions, reviews


# --------------------------------------------------------------------------
# reconcile + entry points


def _chain_rank(chain: str) -> int:
    order = {"leader+dimension+geometry": 0, "leader+geometry": 1, "leader": 2,
             "dimension": 3, "extension_trace": 4, "geometry": 5, "vision": 6}
    return order.get(chain, 9)


def reconcile(evidence: list, value_tol: float = 1.0) -> list:
    """Merge per (region, material, measure). Own-text values win; conflicts
    downgrade. Where two high-precedence chains (leader-own value, Chain A,
    Chain A2) disagree beyond value_tol, record conflict: true and keep the
    higher-precedence chain's answer at reduced confidence (addendum B.4)."""
    grouped: dict[tuple, list] = {}
    for e in evidence:
        measure = e.get("measure") or "thickness"
        # Region-less leader evidence is per-element: two callouts naming the
        # same material must not collapse into one row.
        key = (e.get("region_id"), e.get("material"), measure) if e.get("region_id") is not None \
            else (None, e.get("material"), e.get("element_id"), measure)
        grouped.setdefault(key, []).append(e)
    out = []
    for _key, items in grouped.items():
        chains = sorted({e["attribution_chain"] for e in items}, key=_chain_rank)
        has_leader = any(c.startswith("leader") for c in chains)
        own = [e for e in items if e.get("leader_own_value")]
        dims = [e for e in items if e["attribution_chain"] in ("dimension", "extension_trace")]
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
            names = sorted({e["attribution_chain"] for e in (dims + geos)},
                           key=_chain_rank)
            base["attribution_chain"] = ("leader+" if has_leader else "") + "+".join(names)
        elif has_leader and geos:
            base.update(value=geos[0]["value"], exact_value=geos[0]["value"])
            base["attribution_chain"] = "leader+geometry"
        # cross-chain disagreement beyond tolerance: higher precedence wins,
        # reduced to low confidence with conflict flagged.
        if not conflict:
            strong = [( _chain_rank(e["attribution_chain"]), float(e["value"]))
                      for e in items
                      if e.get("value") is not None and (
                          e.get("leader_own_value")
                          or e["attribution_chain"] in ("dimension", "extension_trace"))]
            if len(strong) > 1:
                vals = [v for _, v in strong]
                if max(vals) - min(vals) > value_tol:
                    conflict = True
        if conflict:
            base["confidence"] = "low"
            base["conflict"] = True
        elif own or (dims and geos):
            base["confidence"] = "high"
            base["conflict"] = False
        out.append(base)
    return out


def analyze_records(records, doc, materials_cfg, geom_cfg, ai_ctx: dict | None = None,
                    crop_fn=None, drawing: str = "") -> dict:
    """Full 7.2 pass over extract records (+ open doc for hatches/leaders).

    ai_ctx = {"adapter", "enabled", "budget_left"} gates Chain D; with none
    (or disabled) unresolvable dimensions stay unattributed and no AI call is
    ever made. crop_fn(anchor, regions) -> PNG bytes for Chain D crops.
    """
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
    # Chain A2 over Chain-A failures + exploded clusters (addendum B.2).
    bound_ids = {b.get("element_id") for b in dim_bound if b.get("element_id")}
    a2_bound, dim_candidates, _a2_rejected = attribute_a2(
        records, regions, geom_cfg, scale_factor, skip_ids=bound_ids)
    dim_unattr = [u for u in dim_unattr
                  if u.get("element_id") not in
                  {b.get("element_id") for b in a2_bound if b.get("element_id")}]
    # Chain D over whatever no chain resolved (addendum B.3, last resort).
    ai_ctx = ai_ctx or {}
    d_attributions, dim_review = run_chain_d(
        dim_candidates, regions, geom_cfg,
        adapter=ai_ctx.get("adapter"), adapter_enabled=bool(ai_ctx.get("enabled")),
        budget_left=ai_ctx.get("budget_left"), crop_fn=crop_fn, drawing=drawing)
    attributions = reconcile(
        leader_ev + dim_bound + geo_ev + a2_bound + d_attributions,
        value_tol=float(geom_cfg.get("value_conflict_tolerance_mm", 1.0)))
    # unattributed: dimension leftovers + loose numbers nowhere
    unattributed = list(dim_unattr)
    texts = [r.get("text_raw") for r in records if r.get("text_raw")]
    texts += [r.get("dim_override") for r in records if r.get("dim_override")]
    return {"regions": regions, "attributions": attributions, "assemblies": assemblies,
            "unattributed": unattributed, "wet_area": wet_area_flag(texts),
            "depends_on": extract_references(" ".join(t for t in texts if t)),
            "scale_factor": scale_factor, "scale_conflict": scale_conflict,
            "dim_candidates": dim_candidates, "dim_review": dim_review}


def analyze_doc(doc, materials_cfg, geometry_cfg) -> dict:
    from .extract import extract_state_from_doc
    records = extract_state_from_doc(doc, materials_cfg)
    return analyze_records(records, doc, materials_cfg, geometry_cfg)


def analyze_dxf(dxf_path: str | Path, materials_cfg: dict, geometry_cfg: dict) -> dict:
    import ezdxf
    doc = ezdxf.readfile(str(dxf_path))
    return analyze_doc(doc, materials_cfg, geometry_cfg)
