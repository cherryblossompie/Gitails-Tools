"""Part 1 — Extractor: DXF -> canonical state records.

Deterministic, rounded (0.1mm), sorted. Floating-point re-save noise
must never register as a change.
"""
from __future__ import annotations

import hashlib
import math
from pathlib import Path

from .semantics import parse_annotation

ROUND_NDIGITS = 1  # 0.1 mm
SUPPORTED = {"LINE", "LWPOLYLINE", "POLYLINE", "ARC", "CIRCLE", "HATCH", "TEXT", "MTEXT", "DIMENSION", "MULTILEADER"}
# DXF versions, oldest first. Brief requires R2018 (AC1032)+ as committed source.
DXF_VERSION_ORDER = ["AC1009", "AC1015", "AC1018", "AC1021", "AC1024", "AC1027", "AC1032"]
MIN_DXF_VERSION = "AC1032"


def dxf_version(dxf_path: str | Path) -> str:
    """$ACADVER of a DXF file (e.g. 'AC1032'), '' when unreadable. Never raises."""
    try:
        import ezdxf
        return ezdxf.readfile(str(dxf_path)).dxfversion
    except Exception:
        return ""


def version_supported(ver: str) -> bool:
    try:
        return DXF_VERSION_ORDER.index(ver) >= DXF_VERSION_ORDER.index(MIN_DXF_VERSION)
    except ValueError:
        return False


def canonical_type(e) -> str:
    """DXF type mapped to canonical state type.

    Old-style 2D POLYLINE (pre-R2018 exports, e.g. Rhino) is geometrically a
    lightweight polyline — normalize to LWPOLYLINE so a re-export does not
    read as delete+add and identity survives across CAD round-trips.
    """
    t = e.dxftype()
    if t == "POLYLINE":
        return "LWPOLYLINE"
    return t


def _poly_points(e) -> list[tuple[float, float]]:
    """2D vertices for LWPOLYLINE and legacy POLYLINE, rounded. Never raises."""
    try:
        if e.dxftype() == "POLYLINE":
            return [(r1(p.x), r1(p.y)) for p in e.points()]
        return [(r1(p[0]), r1(p[1])) for p in e.get_points()]
    except Exception:
        return []


def _poly_closed(e) -> bool:
    try:
        if e.dxftype() == "POLYLINE":
            closed = getattr(e, "is_closed", None)
            if closed is None:
                closed = bool(int(e.dxf.get("flags", 0)) & 1)
            return bool(closed)
        return bool(getattr(e, "closed", False))
    except Exception:
        return False


def r1(v) -> float:
    try:
        return round(float(v), ROUND_NDIGITS)
    except (TypeError, ValueError):
        return 0.0


def _bbox_of_entity(e) -> tuple[float, float, float, float] | None:
    """Return (minx, miny, maxx, maxy) or None. Never raises."""
    try:
        from ezdxf import bbox as _bbox
        ext = _bbox.extents([e])
        if ext.has_data:
            return (float(ext.extmin.x), float(ext.extmin.y), float(ext.extmax.x), float(ext.extmax.y))
    except Exception:
        pass
    # Fallbacks per type
    try:
        t = e.dxftype()
        if t == "TEXT":
            ins = e.dxf.insert
            return (float(ins.x), float(ins.y), float(ins.x), float(ins.y))
        if t == "MTEXT":
            ins = e.dxf.insert
            return (float(ins.x), float(ins.y), float(ins.x), float(ins.y))
        if t == "LINE":
            s, en = e.dxf.start, e.dxf.end
            return (min(float(s.x), float(en.x)), min(float(s.y), float(en.y)),
                    max(float(s.x), float(en.x)), max(float(s.y), float(en.y)))
        if t in ("ARC", "CIRCLE"):
            c = e.dxf.center
            rad = float(e.dxf.radius)
            return (float(c.x) - rad, float(c.y) - rad, float(c.x) + rad, float(c.y) + rad)
        if t in ("LWPOLYLINE", "POLYLINE"):
            pts = _poly_points(e)
            xs = [float(p[0]) for p in pts]
            ys = [float(p[1]) for p in pts]
            if xs and ys:
                return (min(xs), min(ys), max(xs), max(ys))
    except Exception:
        pass
    return None


def _geom_from_bbox(bb) -> dict:
    if not bb:
        return {"x": 0.0, "y": 0.0, "bbox": [0.0, 0.0, 0.0, 0.0]}
    minx, miny, maxx, maxy = (r1(v) for v in bb)
    return {"x": r1((minx + maxx) / 2.0), "y": r1((miny + maxy) / 2.0),
            "bbox": [minx, miny, maxx, maxy]}


def _text_of(e) -> str | None:
    try:
        t = e.dxftype()
        if t == "TEXT":
            return str(e.dxf.text) if e.dxf.text is not None else None
        if t == "MTEXT":
            return str(e.text) if e.text else None
        if t == "MULTILEADER":
            # ezdxf MultiLeader: try context text, else raw
            try:
                ctx = e.context  # type: ignore[attr-defined]
                if ctx is not None and getattr(ctx, "text", None):
                    return str(ctx.text)
            except Exception:
                pass
            for attr in ("text", "plain_text"):
                try:
                    v = getattr(e, attr, None)
                    if callable(v):
                        v = v()
                    if v:
                        return str(v)[:500]
                except Exception:
                    continue
            return None
    except Exception:
        return None
    return None


def _insertion(e):
    try:
        t = e.dxftype()
        if t in ("TEXT", "MTEXT"):
            ins = e.dxf.insert
            return (r1(ins.x), r1(ins.y))
        if t == "MULTILEADER":
            try:
                ctx = e.context  # type: ignore[attr-defined]
                if ctx is not None:
                    # leader endpoint: what it points at
                    leaders = getattr(ctx, "leaders", None) or []
                    if leaders:
                        pts = getattr(leaders[0], "vertices", None) or []
                        if pts:
                            p = pts[-1]
                            return (r1(p.x), r1(p.y))
                    anchor = getattr(ctx, "anchor_point", None) or getattr(ctx, "text_location", None)
                    if anchor is not None:
                        return (r1(anchor.x), r1(anchor.y))
            except Exception:
                pass
    except Exception:
        pass
    return (None, None)


def _dim_info(e):
    measurement = None
    override = None
    defpoints = None
    style = None
    try:
        if e.dxftype() == "DIMENSION":
            try:
                override = e.dxf.text if e.dxf.text not in (None, "", "<>") else None
            except Exception:
                override = None
            try:
                measurement = float(e.get_measurement())  # type: ignore[attr-defined]
                measurement = r1(measurement)
            except Exception:
                measurement = None
            try:
                style = str(e.dxf.dimstyle) if e.dxf.dimstyle else None
            except Exception:
                style = None
            try:
                p1 = e.dxf.defpoint
                p2 = e.dxf.defpoint2
                defpoints = [[r1(p1.x), r1(p1.y)], [r1(p2.x), r1(p2.y)]]
                try:
                    p3 = e.dxf.defpoint3
                    defpoints.append([r1(p3.x), r1(p3.y)])
                except Exception:
                    pass
            except Exception:
                defpoints = None
    except Exception:
        pass
    return measurement, override, defpoints, style


def _hatch_info(e):
    pattern = None
    scale = None
    area = None
    try:
        if e.dxftype() == "HATCH":
            try:
                pattern = str(e.dxf.pattern_name) if e.dxf.pattern_name else None
            except Exception:
                pattern = None
            for attr in ("pattern_scale", "pattern_scale_factor", "scale"):
                try:
                    v = getattr(e.dxf, attr, None)
                    if v is not None:
                        scale = float(v)
                        break
                except Exception:
                    continue
            # area: sum of polyline path areas is complex; use bbox area as proxy
            # plus try ezdxf's estimator if present
            try:
                if hasattr(e, "get_area"):
                    area = r1(float(e.get_area()))  # type: ignore[attr-defined]
            except Exception:
                area = None
    except Exception:
        pass
    return pattern, scale, area


def _linear_info(e):
    vertices = None
    length = None
    linetype = None
    try:
        linetype = str(e.dxf.linetype) if getattr(e.dxf, "linetype", None) else None
    except Exception:
        linetype = None
    try:
        t = e.dxftype()
        if t == "LINE":
            s, en = e.dxf.start, e.dxf.end
            vertices = [[r1(s.x), r1(s.y)], [r1(en.x), r1(en.y)]]
            length = r1(math.hypot(en.x - s.x, en.y - s.y))
        elif t in ("LWPOLYLINE", "POLYLINE"):
            pts = _poly_points(e)
            vertices = [list(p) for p in pts]
            total = 0.0
            for i in range(1, len(pts)):
                total += math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1])
            if _poly_closed(e) and len(pts) > 2:
                total += math.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1])
            length = r1(total)
        elif t == "CIRCLE":
            rad = float(e.dxf.radius)
            length = r1(2 * math.pi * rad)
            c = e.dxf.center
            vertices = [[r1(c.x), r1(c.y)]]
        elif t == "ARC":
            rad = float(e.dxf.radius)
            a0 = math.radians(float(e.dxf.start_angle))
            a1 = math.radians(float(e.dxf.end_angle))
            sweep = (a1 - a0) % (2 * math.pi)
            length = r1(rad * sweep)
            c = e.dxf.center
            vertices = [[r1(c.x), r1(c.y)]]
    except Exception:
        pass
    return vertices, length, linetype


def fingerprint_for(type_: str, layer: str, geom: dict, text_raw, hatch_pattern,
                    dim_measurement, dim_override) -> str:
    bb = geom.get("bbox", [0, 0, 0, 0])
    canon = "|".join([
        str(type_), str(layer),
        f"{geom.get('x', 0):.1f},{geom.get('y', 0):.1f},{bb[0]:.1f},{bb[1]:.1f},{bb[2]:.1f},{bb[3]:.1f}",
        str(text_raw or ""),
        str(hatch_pattern or ""),
        str(dim_measurement if dim_measurement is not None else ""),
        str(dim_override or ""),
    ])
    return "sha1:" + hashlib.sha1(canon.encode("utf-8")).hexdigest()


def extract_state(dxf_path: str | Path, materials_cfg: dict) -> list[dict]:
    """Read DXF modelspace and return canonical records (no element_id yet)."""
    import ezdxf
    doc = ezdxf.readfile(str(dxf_path))
    return extract_state_from_doc(doc, materials_cfg)


def extract_state_from_doc(doc, materials_cfg: dict) -> list[dict]:
    """Same as extract_state but from an open document (tests, in-memory edits)."""
    msp = doc.modelspace()
    out: list[dict] = []
    for e in msp:
        try:
            raw_type = e.dxftype()
        except Exception:
            continue
        if raw_type not in SUPPORTED:
            continue
        type_ = canonical_type(e)  # POLYLINE -> LWPOLYLINE (same geometry, stable identity)
        try:
            handle = str(e.dxf.handle)
        except Exception:
            handle = ""
        try:
            layer = str(e.dxf.layer)
        except Exception:
            layer = "0"

        bb = _bbox_of_entity(e)
        geom = _geom_from_bbox(bb)

        text_raw = _text_of(e) if type_ in ("TEXT", "MTEXT", "MULTILEADER") else None
        # DIMENSION override is text-like but stored separately; also keep searchable
        dim_measurement, dim_override, dim_defpoints, dim_style = _dim_info(e)
        hatch_pattern, hatch_scale, hatch_area = _hatch_info(e)
        vertices, length, linetype = _linear_info(e)

        height = rotation = None
        if type_ in ("TEXT", "MTEXT"):
            try:
                height = r1(float(e.dxf.height)) if e.dxf.height is not None else None
            except Exception:
                height = None
            try:
                rotation = r1(float(e.dxf.rotation)) if getattr(e.dxf, "rotation", None) is not None else None
            except Exception:
                rotation = None

        parsed = parse_annotation(text_raw if text_raw is not None else dim_override,
                                  hatch_pattern, materials_cfg)

        fp = fingerprint_for(type_, layer, geom, text_raw, hatch_pattern,
                             dim_measurement, dim_override)

        rec: dict = {
            "dxf_handle": handle,
            "type": type_,
            "layer": layer,
            "geom": geom,
            "text_raw": text_raw,
            "parsed": parsed,
            "hatch_pattern": hatch_pattern,
            "dim_measurement": dim_measurement,
            "dim_override": dim_override,
            "fingerprint": fp,
        }
        # Optional detail fields (null when N/A) — kept for completeness.
        rec["height"] = height
        rec["rotation"] = rotation
        rec["vertices"] = vertices
        rec["length"] = length
        rec["linetype"] = linetype
        rec["hatch_scale"] = hatch_scale
        rec["hatch_area"] = hatch_area
        rec["dim_defpoints"] = dim_defpoints
        rec["dim_style"] = dim_style
        if type_ == "LWPOLYLINE":
            try:
                rec["closed"] = bool(_poly_closed(e))
            except Exception:
                rec["closed"] = False
        if raw_type != type_:
            rec["dxf_type"] = raw_type  # e.g. POLYLINE normalized to LWPOLYLINE
        ix, iy = _insertion(e)
        rec["insert_x"] = ix
        rec["insert_y"] = iy
        out.append(rec)

    out.sort(key=lambda r: (r["layer"], r["type"], r["geom"]["x"], r["geom"]["y"], r.get("text_raw") or ""))
    return out


PT_TO_MM = 25.4 / 72.0  # PDF points -> mm: keeps tolerances/rounding identical to DXF


def extract_pdf_state(pdf_path: str | Path, materials_cfg: dict) -> list[dict]:
    """PDF text layer -> canonical records (no element_id yet).

    One record per non-empty text line: type PDFTEXT, layer PDF-P{page},
    geometry in mm converted from PDF points. Parsed with the same
    materials config, so `find --material` works across DXF and PDF sources.
    Empty list <=> no text layer (e.g. scanned raster) — caller keeps the
    PDF as view-only. Deterministic sort: page, then bottom-up y, x, text.
    """
    from pdfminer.high_level import extract_pages
    from pdfminer.layout import LTContainer, LTTextLine

    out: list[dict] = []
    try:
        pages = list(extract_pages(str(pdf_path)))
    except Exception:
        return []
    for pno, page in enumerate(pages, 1):
        lineno = 0

        def walk(el):
            nonlocal lineno
            if isinstance(el, LTTextLine):
                text = (el.get_text() or "").strip()
                if not text:
                    return
                lineno += 1
                x0, y0, x1, y1 = (r1(v * PT_TO_MM) for v in (el.x0, el.y0, el.x1, el.y1))
                geom = {"x": r1((x0 + x1) / 2.0), "y": r1((y0 + y1) / 2.0),
                        "bbox": [x0, y0, x1, y1]}
                parsed = parse_annotation(text, None, materials_cfg)
                fp = fingerprint_for("PDFTEXT", f"PDF-P{pno}", geom, text, None, None, None)
                out.append({
                    "dxf_handle": f"pdf:{pno:02d}-{lineno:04d}",
                    "type": "PDFTEXT",
                    "layer": f"PDF-P{pno}",
                    "geom": geom,
                    "text_raw": text,
                    "parsed": parsed,
                    "hatch_pattern": None,
                    "dim_measurement": None,
                    "dim_override": None,
                    "fingerprint": fp,
                    "height": None, "rotation": None, "vertices": None, "length": None,
                    "linetype": None, "hatch_scale": None, "hatch_area": None,
                    "dim_defpoints": None, "dim_style": None,
                    "insert_x": None, "insert_y": None,
                    "source": "pdf",
                })
            elif isinstance(el, LTContainer):
                for child in el:
                    walk(child)

        try:
            walk(page)
        except Exception:
            continue
    out.sort(key=lambda r: (r["layer"], r["geom"]["y"], r["geom"]["x"], r.get("text_raw") or ""))
    return out
