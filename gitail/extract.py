"""Part 1 — Extractor: DXF -> canonical state records.

Deterministic, rounded (0.1mm), sorted. Floating-point re-save noise
must never register as a change.
"""
from __future__ import annotations

import hashlib
import math
import re
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


def extract_state(dxf_path: str | Path, materials_cfg: dict, text_cfg: dict | None = None) -> list[dict]:
    """Read DXF modelspace and return canonical records (no element_id yet)."""
    import ezdxf
    doc = ezdxf.readfile(str(dxf_path))
    return extract_state_from_doc(doc, materials_cfg, text_cfg)


def extract_state_from_doc(doc, materials_cfg: dict, text_cfg: dict | None = None) -> list[dict]:
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
    # Addendum A: reassemble text entities into annotations BEFORE parsing.
    # Parse annotations, never text lines.
    try:
        from .reassemble import reassemble_records
        out = reassemble_records(out, doc, materials_cfg, text_cfg)
    except Exception:
        pass
    return out


PT_TO_MM = 25.4 / 72.0  # PDF points -> mm: keeps tolerances/rounding identical to DXF


def pdfminer_spans(pdf_path: str | Path) -> list[dict]:
    """Raw text spans from a vector PDF (addendum A.3): one dict per
    pdfminer LTChar run — text, bbox (points), size, rotation, font, page.

    Exporters emit text in draw order, not reading order, so NO block/line
    grouping is trusted here; clustering happens downstream from spans.
    Returns [] when the PDF has no text layer (scanned raster).
    """
    import math
    from pdfminer.high_level import extract_pages
    from pdfminer.layout import LTAnno, LTChar, LTContainer, LTTextLine
    try:
        pages = list(extract_pages(str(pdf_path)))
    except Exception:
        return []
    spans = []
    for pno, page in enumerate(pages, 1):
        def walk(el):
            if isinstance(el, LTTextLine):
                buf: list[dict] = []

                def flush():
                    if not buf:
                        return
                    text = "".join(b["ch"] for b in buf)
                    if text.strip():
                        x0 = min(b["x0"] for b in buf)
                        y0 = min(b["y0"] for b in buf)
                        x1 = max(b["x1"] for b in buf)
                        y1 = max(b["y1"] for b in buf)
                        sizes = sorted(b["size"] for b in buf)
                        rots = sorted(b["rot"] for b in buf)
                        spans.append({"text": text, "bbox": (x0, y0, x1, y1),
                                      "size": sizes[len(sizes) // 2],
                                      "rotation": rots[len(rots) // 2],
                                      "font": buf[0]["font"], "page": pno})
                    buf.clear()

                pending_space = False
                for item in el:
                    if isinstance(item, LTAnno):
                        if item.get_text() and item.get_text().strip() == "":
                            pending_space = True
                        continue
                    if not isinstance(item, LTChar):
                        continue
                    ch = item.get_text()
                    try:
                        m = item.matrix
                        rot = round(math.degrees(math.atan2(m[1], m[0]))) % 360
                    except Exception:
                        rot = 0 if getattr(item, "upright", True) else 90
                    if pending_space and buf:
                        # pdfminer represents word gaps as LTAnno between chars
                        buf.append({"ch": " ", "x0": item.x0, "y0": item.y0,
                                    "x1": item.x0, "y1": item.y0,
                                    "size": item.size, "rot": rot,
                                    "font": item.fontname})
                    pending_space = False
                    buf.append({"ch": ch, "x0": item.x0, "y0": item.y0,
                                "x1": item.x1, "y1": item.y1,
                                "size": item.size, "rot": rot,
                                "font": item.fontname})
                flush()
            elif isinstance(el, LTContainer):
                for child in el:
                    walk(child)
        try:
            walk(page)
        except Exception:
            continue
    return spans


def pdfminer_paths(pdf_path: str | Path) -> list[dict]:
    """Stroked path segments (LTLine/LTRect edges, LTFigure recursion) in mm.
    Chain A2's primary geometry source for PDFs, where no DIMENSION entity
    can exist. Curves reduce to endpoints (documented approximation)."""
    from pdfminer.high_level import extract_pages
    from pdfminer.layout import LTContainer, LTCurve, LTFigure, LTLine, LTRect
    try:
        pages = list(extract_pages(str(pdf_path)))
    except Exception:
        return []
    segs = []
    for pno, page in enumerate(pages, 1):
        def emit(x0, y0, x1, y1):
            segs.append({"page": pno,
                         "a": (r1(x0 * PT_TO_MM), r1(y0 * PT_TO_MM)),
                         "b": (r1(x1 * PT_TO_MM), r1(y1 * PT_TO_MM))})

        def walk(el):
            if isinstance(el, LTLine):
                emit(el.x0, el.y0, el.x1, el.y1)
            elif isinstance(el, LTRect):
                emit(el.x0, el.y0, el.x1, el.y0)
                emit(el.x1, el.y0, el.x1, el.y1)
                emit(el.x1, el.y1, el.x0, el.y1)
                emit(el.x0, el.y1, el.x0, el.y0)
            elif isinstance(el, LTCurve):
                pts = getattr(el, "pts", None) or []
                if len(pts) >= 2:
                    emit(pts[0][0], pts[0][1], pts[-1][0], pts[-1][1])
            elif isinstance(el, (LTFigure, LTContainer)):
                for child in el:
                    walk(child)
        try:
            walk(page)
        except Exception:
            continue
    return segs


def cluster_spans_to_lines(spans: list[dict], text_cfg: dict | None = None) -> list[dict]:
    """Spans -> visual text lines (addendum A.3): same baseline within
    tolerance, horizontally contiguous, same size. A single visual line is
    often several spans (kerning, font switches); per-character exporters
    merge here too, by baseline and horizontal adjacency."""
    tol_h = 0.15
    lines: list[list[dict]] = []
    for sp in spans:
        # Whitespace-only spans still mark word gaps (test 93): keep them
        # for gap measurement, then drop them at text assembly.
        if not (sp.get("text") or ""):
            continue
        x0, y0, x1, y1 = sp["bbox"]
        size = sp.get("size") or 1.0
        rot = sp.get("rotation") or 0
        placed = False
        for ln in lines:
            first = ln[0]
            if (first.get("rotation") or 0) != rot:
                continue
            fsize = first.get("size") or 1.0
            if abs(size - fsize) / max(size, fsize) > tol_h:
                continue
            fx0, fy0, fx1, fy1 = first["line_bbox"]
            if rot % 180 == 0:
                if abs(y0 - fy0) > 0.3 * max(size, fsize):
                    continue
                gap_ok = (x0 <= fx1 + 0.6 * size) and (fx0 <= x1 + 0.6 * size)
            else:
                if abs(x0 - fx0) > 0.3 * max(size, fsize):
                    continue
                gap_ok = (y0 <= fy1 + 0.6 * size) and (fy0 <= y1 + 0.6 * size)
            if not gap_ok:
                continue
            ln.append(sp)
            fx = [first["line_bbox"][0], first["line_bbox"][1],
                  first["line_bbox"][2], first["line_bbox"][3]]
            first["line_bbox"] = [min(fx[0], x0), min(fx[1], y0),
                                  max(fx[2], x1), max(fx[3], y1)]
            placed = True
            break
        if not placed:
            first = dict(sp)
            first["line_bbox"] = [x0, y0, x1, y1]
            lines.append([first])
    out = []
    for ln in lines:
        rot = ln[0].get("rotation") or 0
        ordered = sorted(ln, key=lambda s: s["bbox"][0] if rot % 180 == 0 else s["bbox"][1])
        # Per-character exporters emit no space chars: a horizontal gap wider
        # than half the char size marks a word break (test 93).
        parts: list[str] = []
        prev_end = None
        for s in ordered:
            t = s.get("text") or ""
            if not t.strip():
                parts.append(" ")
                prev_end = None
                continue
            if prev_end is not None and parts and not parts[-1].endswith(" "):
                size = s.get("size") or ln[0].get("size") or 1.0
                start = s["bbox"][0] if rot % 180 == 0 else s["bbox"][1]
                if start - prev_end > 0.5 * size:
                    parts.append(" ")
            parts.append(t)
            prev_end = s["bbox"][2] if rot % 180 == 0 else s["bbox"][3]
        text = re.sub(r" {2,}", " ", "".join(parts)).strip()
        if not text:
            continue
        xs = [s["bbox"][0] for s in ln] + [s["bbox"][2] for s in ln]
        ys = [s["bbox"][1] for s in ln] + [s["bbox"][3] for s in ln]
        sizes = sorted(s.get("size") or 0 for s in ln)
        out.append({"text": text, "bbox": (min(xs), min(ys), max(xs), max(ys)),
                    "size": sizes[len(sizes) // 2] if sizes else 0,
                    "rotation": rot, "page": ln[0].get("page", 1)})
    # draw order is untrustworthy — deterministic top-down, left-to-right
    out.sort(key=lambda l: (-l["bbox"][3], l["bbox"][0]))
    return out


def extract_pdf_state(pdf_path: str | Path, materials_cfg: dict, text_cfg: dict | None = None) -> list[dict]:
    """PDF text layer -> canonical records (no element_id yet).

    Spans re-cluster into lines (never trusted exporter blocks), lines
    reassemble into ANNOTATION records like DXF, plus PDFPATH records for
    stroked segments (Chain A2 geometry). Positions in sheet mm. Empty list
    <=> no text layer (e.g. scanned raster) — caller keeps the PDF as
    view-only. Deterministic order.
    """
    from .reassemble import load_text_config, reassemble_records
    text_cfg = text_cfg or load_text_config(None)
    spans = pdfminer_spans(pdf_path)
    if not spans:
        return []
    lines = cluster_spans_to_lines(spans, text_cfg)
    paths = pdfminer_paths(pdf_path)
    pseudo: list[dict] = []
    per_page_line: dict[int, int] = {}
    for ln in lines:
        pno = ln.get("page", 1)
        per_page_line[pno] = per_page_line.get(pno, 0) + 1
        lineno = per_page_line[pno]
        x0, y0, x1, y1 = (r1(v * PT_TO_MM) for v in ln["bbox"])
        geom = {"x": r1((x0 + x1) / 2.0), "y": r1((y0 + y1) / 2.0),
                "bbox": [x0, y0, x1, y1]}
        size_mm = r1((ln.get("size") or 0) * PT_TO_MM)
        pseudo.append({
            "dxf_handle": f"pdf:{pno:02d}-{lineno:04d}",
            "type": "PDFTEXT",
            "layer": f"PDF-P{pno}",
            "geom": geom,
            "text_raw": ln["text"],
            "parsed": None,  # parsed post-reassembly, on joined annotations
            "hatch_pattern": None,
            "dim_measurement": None,
            "dim_override": None,
            "fingerprint": "",
            "height": size_mm or None,
            "rotation": ln.get("rotation") or 0,
            "vertices": None, "length": None,
            "linetype": None, "hatch_scale": None, "hatch_area": None,
            "dim_defpoints": None, "dim_style": None,
            "insert_x": x0, "insert_y": y1,
            "source": "pdf",
        })
    try:
        out = reassemble_records(pseudo, None, materials_cfg, text_cfg)
    except Exception:
        out = pseudo
    for rec in out:
        rec["source"] = "pdf"
    # stroked path geometry for Chain A2 (viewing never; measuring only)
    for i, sg in enumerate(paths):
        pno = sg["page"]
        (ax, ay), (bx, by) = sg["a"], sg["b"]
        out.append({
            "dxf_handle": f"pdfpath:{pno:02d}-{i:04d}",
            "type": "PDFPATH",
            "layer": f"PDF-P{pno}",
            "geom": {"x": r1((ax + bx) / 2.0), "y": r1((ay + by) / 2.0),
                     "bbox": [min(ax, bx), min(ay, by), max(ax, bx), max(ay, by)]},
            "text_raw": None, "parsed": None, "hatch_pattern": None,
            "dim_measurement": None, "dim_override": None,
            "fingerprint": fingerprint_for("PDFPATH", f"PDF-P{pno}",
                                           {"x": r1((ax + bx) / 2.0), "y": r1((ay + by) / 2.0),
                                            "bbox": [min(ax, bx), min(ay, by), max(ax, bx), max(ay, by)]},
                                           None, None, None, None),
            "height": None, "rotation": None,
            "vertices": [[ax, ay], [bx, by]],
            "length": r1(math.hypot(bx - ax, by - ay)),
            "linetype": None, "hatch_scale": None, "hatch_area": None,
            "dim_defpoints": None, "dim_style": None,
            "insert_x": None, "insert_y": None,
            "source": "pdf",
        })
    out.sort(key=lambda r: (r["layer"], r["type"], r["geom"]["x"], r["geom"]["y"],
                            r.get("text_raw") or ""))
    return out
