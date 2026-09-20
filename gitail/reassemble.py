"""Addendum A — annotation reassembly. Runs BEFORE semantic parsing.

A single annotation is frequently drawn as several text entities. Parsed
line-by-line, `NOM. 50MM SLAB` + `SETDOWN` mis-reads a setdown depth as a slab
thickness — wrong in a way that silently corrupts thickness searches. Parse
annotations, never text lines.

Signal hierarchy (geometric first; AI only for the residue):
  1. already one unit — MTEXT (unescaped) and MULTILEADER own-text.
  2. leader association — one annotation has exactly one leader (decisive).
  3. geometric contiguity — alignment/height/gap/rotation/layer gates.
  4. parse completeness — merged-vs-split scored on clean parses + orphans.
  5. AI adjudication — partition-constrained, validated, geometric fallback.

Output: ANNOTATION records replacing member TEXT/MTEXT/MULTILEADER records.
source_lines keeps every original entity handle, so identity still tracks the
underlying text entities: editing one line of a two-line annotation registers
as value_changed on that annotation (stable handle-set id), not as a new one.
"""
from __future__ import annotations

import logging
import math
import re
from pathlib import Path

log = logging.getLogger("gitail.reassemble")

DEFAULT_TEXT_CFG = {
    "left_edge_tolerance_mm": 1.5,
    "height_ratio_tolerance": 0.15,
    "line_gap_ratio": [0.9, 1.7],
    "same_rotation_tolerance_deg": 1.0,
    "same_layer_required": True,
    "leader_text_capture_mm": 8.0,
    "continuation_words": ["FOR", "TO", "ON", "WITH", "AT", "AND", "OF",
                           "REFER", "DWGS"],
    "annotation_layer_substrings": ["ANNO", "TEXT", "NOTE", "LEAD"],
}

TEXT_TYPES = ("TEXT", "MTEXT", "MULTILEADER", "PDFTEXT", "ANNOTATION")


def load_text_config(path: str | Path | None) -> dict:
    """config/text.yaml with bundled fallback (mirrors load_geometry_config)."""
    cfg = dict(DEFAULT_TEXT_CFG)
    cfg["line_gap_ratio"] = list(DEFAULT_TEXT_CFG["line_gap_ratio"])
    cfg["continuation_words"] = list(DEFAULT_TEXT_CFG["continuation_words"])
    cfg["annotation_layer_substrings"] = list(
        DEFAULT_TEXT_CFG["annotation_layer_substrings"])
    if not path:
        return cfg
    try:
        import yaml
        with open(path, "r", encoding="utf-8-sig") as f:
            data = yaml.safe_load(f) or {}
        for k, default in cfg.items():
            if data.get(k) is not None:
                cfg[k] = data[k]
    except Exception:
        pass
    return cfg


# --------------------------------------------------------------------------
# Signal 1 — MTEXT unescape: one entity holds its whole string.

_MTEXT_CODES = re.compile(r"\\([LOKloPX pomp\d;?.]|f[^;]*;|C\d+;|H[\d.]+x?;|"
                          r"S([^;^]*)\^([^;]*);|T\d+;|Q\d+;|W\d+;|A\d+;)")


def unescape_mtext(text: str | None) -> str:
    r"""Join an MTEXT raw string into one annotation: \P paragraph breaks and
    inline formatting become spaces, braces drop, %% specials resolve."""
    if not text:
        return ""
    s = str(text)
    s = s.replace("\\P", " ").replace("\\p", " ")
    s = s.replace("%%d", "\u00b0").replace("%%p", "\u00b1").replace("%%c", "\u00d8")
    s = s.replace("%%D", "\u00b0").replace("%%P", "\u00b1").replace("%%C", "\u00d8")

    def _code(m: re.Match) -> str:
        whole = m.group(0)
        stacked = re.match(r"\\S([^;^]*)\^([^;]*);", whole)
        if stacked:
            return f"{stacked.group(1)}/{stacked.group(2)}"
        return " "

    s = _MTEXT_CODES.sub(_code, s)
    s = s.replace("{", "").replace("}", "")
    s = re.sub(r"\s+", " ", s).strip()
    return s


# --------------------------------------------------------------------------
# Line model (internal). Rotation normalised for contiguity testing; the
# record keeps the original rotation (it labels vertical dimensions).

def _rotated_frame(bbox, rotation_deg):
    """Axis-aligned bbox in a rotation-normalised frame (for gap/edge tests).
    Handles horizontal and near-vertical text; other angles pass through."""
    x0, y0, x1, y1 = bbox
    rot = (rotation_deg or 0.0) % 180.0
    if 45.0 < rot < 135.0:
        cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        hw, hh = (x1 - x0) / 2.0, (y1 - y0) / 2.0
        return [cx - hh, cy - hw, cx + hh, cy + hw]
    return [x0, y0, x1, y1]


def _line_height(line) -> float | None:
    if line.get("height"):
        try:
            return float(line["height"])
        except (TypeError, ValueError):
            pass
    bb = _rotated_frame(line["bbox"], line.get("rotation"))
    return abs(bb[3] - bb[1]) or None


def _same_height(a, b, tol_ratio) -> bool:
    ha, hb = _line_height(a), _line_height(b)
    if not ha or not hb:
        return True  # unknown height never blocks (documented wildcard)
    return abs(ha - hb) / max(ha, hb) <= tol_ratio


def _same_rotation(a, b, tol_deg) -> bool:
    ra = (a.get("rotation") or 0.0) % 180.0
    rb = (b.get("rotation") or 0.0) % 180.0
    return abs(ra - rb) <= tol_deg or abs(abs(ra - rb) - 180.0) <= tol_deg


def _detect_alignment(lines) -> str:
    """left / right / center from edge distributions (centre-aligned title
    blocks fail a left-edge test — detect, don't assume)."""
    if len(lines) < 2:
        return "left"
    frames = [_rotated_frame(ln["bbox"], ln.get("rotation")) for ln in lines]
    hs = [_line_height(ln) or 1.0 for ln in lines]
    h = sum(hs) / len(hs)

    def spread(edge):
        vals = [f[edge] / h for f in frames]
        mean = sum(vals) / len(vals)
        return sum((v - mean) ** 2 for v in vals) / len(vals)
    left, right = spread(0), spread(2)
    centers = [((f[0] + f[2]) / 2.0) / h for f in frames]
    mean = sum(centers) / len(centers)
    center = sum((v - mean) ** 2 for v in centers) / len(centers)
    best = min(("left", left), ("right", right), ("center", center),
               key=lambda t: t[1])
    return best[0]


def _edge_of(line, alignment: str) -> float:
    bb = _rotated_frame(line["bbox"], line.get("rotation"))
    if alignment == "right":
        return bb[2]
    if alignment == "center":
        return (bb[0] + bb[2]) / 2.0
    return bb[0]


def _vertical_gap(upper, lower) -> float | None:
    """Gap between two stacked lines (upper above lower) in the normalised frame."""
    a = _rotated_frame(upper["bbox"], upper.get("rotation"))
    b = _rotated_frame(lower["bbox"], lower.get("rotation"))
    if a[1] >= b[3]:
        return a[1] - b[3]
    if b[1] >= a[3]:
        return b[1] - a[3]
    return 0.0


def _lines_mergeable(group: list, candidate, text_cfg) -> bool:
    """Signal 3 gates: same layer, rotation, height, aligned edge, line gap."""
    if text_cfg.get("same_layer_required", True) and \
            candidate.get("layer") != group[0].get("layer"):
        return False
    if not all(_same_rotation(candidate, g,
                              float(text_cfg.get("same_rotation_tolerance_deg", 1.0)))
               for g in group):
        return False
    if not all(_same_height(candidate, g,
                            float(text_cfg.get("height_ratio_tolerance", 0.15)))
               for g in group):
        return False
    # Single-edge groups cannot vote on alignment: accept the candidate when
    # ANY edge (left/right/centre) aligns. Longer groups use their detected
    # alignment strictly (addendum A.2 signal 3, test 90).
    tol = float(text_cfg.get("left_edge_tolerance_mm", 1.5))
    if len(group) == 1:
        diffs = [abs(_edge_of(candidate, e) - _edge_of(group[-1], e))
                 for e in ("left", "right", "center")]
        if min(diffs) > tol:
            return False
    else:
        alignment = _detect_alignment(group)
        if abs(_edge_of(candidate, alignment) - _edge_of(group[-1], alignment)) > tol:
            return False
    lo, hi = text_cfg.get("line_gap_ratio", [0.9, 1.7])
    h = _line_height(group[-1]) or _line_height(candidate) or 1.0
    gap = _vertical_gap(group[-1], candidate)
    if gap is None:
        return False
    return (lo * h) <= gap <= (hi * h)


# --------------------------------------------------------------------------
# Signal 2 — leader association: one annotation, exactly one leader.

def _is_annotation_layer(layer: str | None, text_cfg) -> bool:
    upper = str(layer or "").upper()
    return any(s in upper for s in
               text_cfg.get("annotation_layer_substrings", ["ANNO"]))


def collect_leaders(records, doc, text_cfg) -> list[dict]:
    """Leader geometry: MULTILEADER entities (doc needed for arrow vertices)
    plus LINE/LWPOLYLINE polylines on annotation layers with one endpoint
    adjacent to text and the other terminating away from text."""
    leaders: list[dict] = []
    texts = [r for r in records if r.get("type") in ("TEXT", "MTEXT", "MULTILEADER")
             and (r.get("text_raw") or "").strip()]
    blocks = []
    for t in texts:
        g = t.get("geom") or {}
        bb = list(g.get("bbox") or [0, 0, 0, 0])
        if bb != [0, 0, 0, 0]:
            blocks.append([float(bb[0]), float(bb[1]), float(bb[2]), float(bb[3])])
    capture = float(text_cfg.get("leader_text_capture_mm", 8.0))
    lid = 0

    def _near_text(pt) -> bool:
        return any(_pt_bbox_dist(pt, bb) <= capture for bb in blocks)

    if doc is not None:
        try:
            for e in doc.modelspace():
                try:
                    if e.dxftype() != "MULTILEADER":
                        continue
                    ctx = e.context
                    arrow = None
                    text_at = None
                    for attr in ("anchor_point", "text_location"):
                        try:
                            p = getattr(ctx, attr, None)
                            if p is not None:
                                text_at = (round(float(p.x), 1), round(float(p.y), 1))
                                break
                        except Exception:
                            continue
                    leaders_attr = getattr(ctx, "leaders", None) or []
                    if leaders_attr:
                        verts = getattr(leaders_attr[0], "vertices", None) or []
                        if verts:
                            arrow = (round(float(verts[-1].x), 1),
                                     round(float(verts[-1].y), 1))
                    if arrow is not None:
                        leaders.append({"id": f"l_{lid:02d}", "kind": "MULTILEADER",
                                        "text_point": text_at or arrow,
                                        "far_point": arrow,
                                        "handle": str(e.dxf.handle)})
                        lid += 1
                except Exception:
                    continue
        except Exception:
            pass
    for r in records:
        if r.get("type") not in ("LINE", "LWPOLYLINE"):
            continue
        if not _is_annotation_layer(r.get("layer"), text_cfg):
            continue
        verts = [(float(p[0]), float(p[1])) for p in (r.get("vertices") or [])]
        if len(verts) < 2:
            continue
        near0, near1 = _near_text(verts[0]), _near_text(verts[-1])
        if near0 == near1:
            continue  # both ends at text (underline?) or neither — not a leader
        text_pt, far_pt = (verts[0], verts[-1]) if near0 else (verts[-1], verts[0])
        leaders.append({"id": f"l_{lid:02d}", "kind": "LINE",
                        "text_point": (round(text_pt[0], 1), round(text_pt[1], 1)),
                        "far_point": (round(far_pt[0], 1), round(far_pt[1], 1)),
                        "handle": r.get("dxf_handle")})
        lid += 1
    leaders.sort(key=lambda l: (l["far_point"][0], l["far_point"][1], l["id"]))
    for i, l in enumerate(leaders):
        l["id"] = f"l_{i:02d}"
    return leaders


def _dist(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _pt_bbox_dist(pt, bbox) -> float:
    """Point to text-bbox distance (0 inside). Leader adjacency is about the
    drawn text block, not the arbitrary insertion corner — a leader starting
    mid-line is 0mm from the text even when its insertion sits far away."""
    x0, y0, x1, y1 = bbox
    dx = max(x0 - pt[0], pt[0] - x1, 0.0)
    dy = max(y0 - pt[1], pt[1] - y1, 0.0)
    return math.hypot(dx, dy)


def group_by_leader(lines: list, leaders: list, text_cfg) -> tuple[dict, set]:
    """Assign each line to its nearest leader endpoint (within capture), then
    grow groups along aligned, contiguous lines. Returns (leader_id ->
    [line idx], contested idx set: lines in range of 2+ leaders)."""
    capture = float(text_cfg.get("leader_text_capture_mm", 8.0))
    nearest: dict[int, list] = {}
    for ln in lines:
        hits = []
        for l in leaders:
            d = _pt_bbox_dist(tuple(l["text_point"]), ln["bbox"])
            if d <= capture:
                hits.append((d, l["id"]))
        hits.sort()
        nearest[ln["idx"]] = hits
    contested = {idx for idx, hits in nearest.items() if len(hits) > 1}
    groups: dict[str, list] = {l["id"]: [] for l in leaders}
    for ln in lines:
        if nearest[ln["idx"]]:
            groups[nearest[ln["idx"]][0][1]].append(ln["idx"])
    # grow along alignment + contiguity (test 85: five lines, two leaders).
    # Spatial order matters: compare against the group's current edge line.
    by_idx = {ln["idx"]: ln for ln in lines}

    def _edge_order(idxs: list[int]) -> list:
        def key(i):
            bb = _rotated_frame(by_idx[i]["bbox"], by_idx[i].get("rotation"))
            return (-bb[3], bb[0])
        return [by_idx[i] for i in sorted(idxs, key=key)]

    assigned = {idx for members in groups.values() for idx in members}
    changed = True
    while changed:
        changed = False
        for lid, members in groups.items():
            if not members:
                continue
            group_lines = _edge_order(members)
            for ln in lines:
                if ln["idx"] in assigned:
                    continue
                if _lines_mergeable(group_lines, ln, text_cfg):
                    # never steal a line nearer another leader's endpoint
                    bb = ln["bbox"]
                    own = _pt_bbox_dist(
                        tuple(next(l["text_point"] for l in leaders if l["id"] == lid)), bb)
                    rival = min((_pt_bbox_dist(tuple(l["text_point"]), bb)
                                 for l in leaders if l["id"] != lid),
                                default=float("inf"))
                    if rival < own:
                        continue
                    members.append(ln["idx"])
                    assigned.add(ln["idx"])
                    changed = True
    for lid in groups:
        groups[lid] = sorted(groups[lid])
    return groups, contested


# --------------------------------------------------------------------------
# Signal 4 — parse completeness: merged vs split, scored on clean parses.

def _ends_continuation_hint(text: str, continuation_words) -> bool:
    s = str(text or "").rstrip()
    if not s:
        return False
    if s.endswith(("-", ",")):
        return True
    last = re.split(r"\s+", s)[-1].rstrip(".,;:").upper()
    if last in {w.upper() for w in _cont_words(continuation_words)}:
        return True
    return bool(re.match(r"^[A-Z]{1,4}\.$", last))  # abbrev stump: "ENG."


def _cont_words(continuation_words) -> list[str]:
    """Continuation words as strings. YAML 1.1 reads bare ON/OFF as
    booleans, so coerce defensively: True -> "ON", False -> "OFF"."""
    out = []
    for w in (continuation_words or []):
        if isinstance(w, bool):
            out.append("ON" if w else "OFF")
        else:
            out.append(str(w))
    return out


def _starts_continuation(text: str, continuation_words) -> bool:
    s = str(text or "").lstrip().upper()
    return any(s == w.upper() or s.startswith(w.upper() + " ")
               or s.startswith(w.upper() + ".") for w in _cont_words(continuation_words))


def score_reading(text: str, materials_cfg, continuation_words) -> tuple[float, bool]:
    """(score, parses): +2 material-or-measure, +1 value, +0.5 part/delegated.
    A setdown/fall with value but no material still parses (addendum A.5); a
    bare measure word alone does not. Fragments handled by caller."""
    from .semantics import parse_text
    try:
        parsed = parse_text(text, materials_cfg)
    except Exception:
        parsed = None
    if not parsed:
        return 0.0, False
    score = 0.0
    if parsed.get("material"):
        score += 2.0
    elif parsed.get("measure") not in (None, "thickness") and parsed.get("value") is not None:
        score += 2.0
    if parsed.get("value") is not None:
        score += 1.0
    if parsed.get("spec_delegated") or parsed.get("part"):
        score += 0.5
    return score, score >= 2.0


def choose_merge(line_texts: list[str], materials_cfg, text_cfg) -> tuple[bool, str]:
    """Test both hypotheses (addendum A.2 signal 4). Merge wins when it parses
    and the split reading does not — i.e. split leaves an orphan fragment or
    parses worse. Returns (merge, reason)."""
    from .semantics import parse_text
    cont = text_cfg.get("continuation_words", [])
    merged = " ".join(t.strip() for t in line_texts if t.strip())
    merged_score, merged_parses = score_reading(merged, materials_cfg, cont)
    frag_scores = []
    orphans = 0
    for i, t in enumerate(line_texts):
        s, parses = score_reading(t.strip(), materials_cfg, cont)
        prev = line_texts[i - 1] if i else ""
        if not parses and (_starts_continuation(t, cont)
                           or (i and _ends_continuation_hint(prev, cont))):
            orphans += 1
            s -= 3.0
        frag_scores.append(s)
    split_score = sum(frag_scores)
    if merged_parses and (orphans or merged_score > split_score):
        return True, "parse-complete"
    return False, "split-parses-better"


# --------------------------------------------------------------------------
# Grouping driver + annotation records.

def _order_top_down(idxs: list, by_idx) -> list:
    def key(i):
        bb = _rotated_frame(by_idx[i]["bbox"], by_idx[i].get("rotation"))
        return (-bb[3], bb[0])
    return sorted(idxs, key=key)


def _union_bbox(lines: list) -> list:
    xs = [c for ln in lines for c in (ln["bbox"][0], ln["bbox"][2])]
    ys = [c for ln in lines for c in (ln["bbox"][1], ln["bbox"][3])]
    return [round(min(xs), 1), round(min(ys), 1), round(max(xs), 1), round(max(ys), 1)]


def build_annotation(group_lines: list, reassembly: dict, layer: str | None,
                     leader_endpoint=None) -> dict:
    """One ANNOTATION record from grouped lines (top-down join)."""
    from .extract import fingerprint_for, r1
    from .semantics import parse_annotation as _parse  # resolved by caller instead
    ordered = sorted(group_lines, key=lambda ln: (
        -_rotated_frame(ln["bbox"], ln.get("rotation"))[3], ln["bbox"][0]))
    texts = [ln["text"] for ln in ordered]
    joined = re.sub(r"\s+", " ", " ".join(texts)).strip()
    handles = sorted({ln["handle"] for ln in ordered if ln.get("handle")})
    bbox = _union_bbox(ordered)
    geom = {"x": r1((bbox[0] + bbox[2]) / 2.0), "y": r1((bbox[1] + bbox[3]) / 2.0),
            "bbox": bbox}
    first = ordered[0]
    return {
        "type": "ANNOTATION",
        "dxf_handle": "|".join(handles),
        "handles": handles,
        "layer": layer or first.get("layer") or "0",
        "geom": geom,
        "text_raw": joined,
        "parsed": None,  # filled by caller (needs materials cfg)
        "hatch_pattern": None,
        "dim_measurement": None,
        "dim_override": None,
        "fingerprint": fingerprint_for("ANNOTATION", layer or first.get("layer") or "0",
                                       geom, joined, None, None, None),
        "height": first.get("height"),
        "rotation": first.get("rotation"),
        "vertices": None, "length": None, "linetype": None,
        "hatch_scale": None, "hatch_area": None,
        "dim_defpoints": None, "dim_style": None,
        "insert_x": (first.get("insert") or [None, None])[0],
        "insert_y": (first.get("insert") or [None, None])[1],
        "source_lines": [{"text": ln["text"], "bbox": list(ln["bbox"]),
                          "entity_handle": ln.get("handle"),
                          "insert": list(ln["insert"]) if ln.get("insert") else None}
                         for ln in ordered],
        "reassembly": reassembly,
        "leader_endpoint": list(leader_endpoint) if leader_endpoint else None,
        "leader_id": reassembly.get("leader_id"),
        "_join_text": joined,  # caller parses + drops the private key
    }


def reassemble_records(records: list[dict], doc, materials_cfg: dict,
                       text_cfg: dict | None = None, adapter=None,
                       adapter_enabled: bool = False,
                       budget_left=None, crop_fn=None) -> list[dict]:
    """Replace member TEXT/MTEXT/MULTILEADER records with ANNOTATION records.

    Signals run in hierarchy order; parse() applies to joined annotations
    only (never to raw lines). Deterministic output order.
    """
    from .semantics import parse_annotation
    text_cfg = text_cfg or load_text_config(None)
    leaders = collect_leaders(records, doc, text_cfg)
    # line model per text entity
    lines: list[dict] = []
    consumed: set[str] = set()  # record indexes consumed, by id()
    for n, r in enumerate(records):
        if r.get("type") not in ("TEXT", "MTEXT", "MULTILEADER", "PDFTEXT"):
            continue
        text = (r.get("text_raw") or "")
        if r.get("type") == "MTEXT":
            text = unescape_mtext(text)
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue
        g = r.get("geom") or {}
        bb = list(g.get("bbox") or [0, 0, 0, 0])
        ix, iy = r.get("insert_x"), r.get("insert_y")
        lines.append({
            "idx": len(lines), "rec": n,
            "text": text,
            "bbox": [float(bb[0]), float(bb[1]), float(bb[2]), float(bb[3])],
            "insert": [float(ix), float(iy)] if ix is not None and iy is not None else None,
            "height": r.get("height"), "rotation": r.get("rotation"),
            "layer": r.get("layer") or "0",
            "handle": r.get("dxf_handle") or "",
            "kind": r.get("type"),
            "pregrouped": r.get("type") in ("MTEXT", "MULTILEADER"),
        })
    by_idx = {ln["idx"]: ln for ln in lines}
    # Signal 1: MTEXT / MULTILEADER own-text units never merge further.
    # They still take a leader when exactly one serves them: Chain B needs the
    # endpoint, and one annotation has exactly one leader.
    groups: list[tuple[list[int], dict]] = []
    grouped_idx: set[int] = set()
    mtext_leader = {l["handle"]: l for l in leaders if l.get("handle")}
    capture = float(text_cfg.get("leader_text_capture_mm", 8.0))
    for ln in lines:
        if not ln["pregrouped"]:
            continue
        lead = None
        if ln["kind"] == "MULTILEADER" and ln["handle"] in mtext_leader:
            lead = mtext_leader[ln["handle"]]
        else:
            bb = ln["bbox"]
            near = sorted(
                ((_pt_bbox_dist(tuple(l["text_point"]), bb), l) for l in leaders),
                key=lambda t: t[0])
            near = [(d, l) for d, l in near if d <= capture]
            if near:
                lead = near[0][1]
        meta = {"signal": "mtext" if ln["kind"] == "MTEXT" else "multileader",
                "confidence": "high",
                "leader_id": lead["id"] if lead else None,
                "alternatives_considered": max(0, len(near) - 1) if not (
                    ln["kind"] == "MULTILEADER" and ln["handle"] in mtext_leader)
                else 0,
                "ai_used": False}
        if lead is not None:
            meta["leader_far"] = list(lead["far_point"])
        groups.append(([ln["idx"]], meta))
        grouped_idx.add(ln["idx"])
    free = [ln for ln in lines if ln["idx"] not in grouped_idx]
    # Signal 2: leader association over remaining TEXT lines.
    if free and leaders:
        lg, contested = group_by_leader(free, leaders, text_cfg)
        for lid, members in lg.items():
            if not members:
                continue
            lead = next(l for l in leaders if l["id"] == lid)
            far = tuple(lead["far_point"])
            members = [i for i in members if i not in grouped_idx]
            if not members:
                continue
            # Signal 2 is decisive (addendum A.2): one leader = one annotation.
            # Parse-completeness (signal 4) validates contiguity groups only;
            # a leader-grown group is never split on parse score. Test 85.
            ordered = _order_top_down(members, by_idx)
            groups.append((ordered, {"signal": "leader", "confidence": "high",
                                     "leader_id": lid,
                                     "alternatives_considered": len(contested),
                                     "ai_used": False,
                                     "leader_far": list(far)}))
            grouped_idx.update(ordered)
    # Signal 3: contiguity for the rest, validated by signal 4.
    rest = [ln for ln in free if ln["idx"] not in grouped_idx]
    rest.sort(key=lambda ln: (-_rotated_frame(ln["bbox"], ln.get("rotation"))[3],
                              _rotated_frame(ln["bbox"], ln.get("rotation"))[0]))
    pending: list[list[int]] = []
    for ln in rest:
        placed = False
        for g in pending:
            if _lines_mergeable([by_idx[i] for i in g], ln, text_cfg):
                g.append(ln["idx"])
                placed = True
                break
        if not placed:
            pending.append([ln["idx"]])
    ambiguous: list[list[int]] = []
    for members in pending:
        if len(members) == 1:
            groups.append((members, {"signal": "singleton", "confidence": "high",
                                     "leader_id": None, "alternatives_considered": 0,
                                     "ai_used": False}))
            continue
        ordered = _order_top_down(members, by_idx)
        texts = [by_idx[i]["text"] for i in ordered]
        merge, why = choose_merge(texts, materials_cfg, text_cfg)
        if not merge and why == "split-parses-better" and len(members) == 2:
            # Geometry-only fallback: a clean contiguous pair where the split
            # reading orphans a line (titles like "DETAILS AR 1", whose
            # fragment parses to nothing) has no split reading to prefer.
            # Merging is the only non-orphaning choice (test 90).
            from .semantics import parse_text as _pt
            try:
                frag_parses = [bool(_pt(t.strip(), materials_cfg)) for t in texts]
            except Exception:
                frag_parses = [False]
            if not all(frag_parses):
                merge, why = True, "contiguous-fragment-orphaned"
        if merge:
            groups.append((ordered, {"signal": "contiguity+parse",
                                     "confidence": "high",
                                     "leader_id": None,
                                     "alternatives_considered": 1,
                                     "ai_used": False}))
        else:
            # geometric grouping disagrees with parse scoring — AI residue.
            ambiguous.append(ordered)
            for i in ordered:
                groups.append(([i], {"signal": "split-parses-better",
                                     "confidence": "medium",
                                     "leader_id": None,
                                     "alternatives_considered": 1,
                                     "ai_used": False}))
    # Signal 5: AI adjudication over ambiguous blocks (validated partition).
    if ambiguous and adapter is not None and adapter_enabled \
            and (budget_left is None or budget_left()):
        from .ai import TextLine, validate_partition
        flat = [i for block in ambiguous for i in block]
        ai_lines = [TextLine(index=k, text=by_idx[i]["text"],
                             bbox=list(by_idx[i]["bbox"]),
                             height=by_idx[i].get("height"),
                             rotation=by_idx[i].get("rotation"))
                    for k, i in enumerate(flat)]
        crop = None
        if crop_fn is not None:
            try:
                crop = crop_fn(_union_bbox([by_idx[i] for i in flat]))
            except Exception:
                crop = None
        try:
            raw_groups = adapter.group_text_lines(
                crop, ai_lines, leader_count=len(leaders))
            part = validate_partition(raw_groups, len(flat))
            # accept: rebuild those groups as AI annotations
            ai_idx = {i for block in ambiguous for i in block}
            groups = [g for g in groups
                      if not (len(g[0]) == 1 and g[0][0] in ai_idx)]
            for g in part:
                members = [flat[k] for k in g]
                ordered = _order_top_down(members, by_idx)
                groups.append((ordered, {"signal": "ai", "confidence": "medium",
                                         "leader_id": None,
                                         "alternatives_considered": len(ambiguous),
                                         "ai_used": True}))
        except Exception as ex:
            log.warning("group_text_lines discarded (%s); geometric grouping kept", ex)
    # emit annotation records; parse joined text only
    consumed_recs = {by_idx[i]["rec"] for members, _meta in groups for i in members}
    out = [r for n, r in enumerate(records) if n not in consumed_recs]
    for members, meta in groups:
        member_lines = [by_idx[i] for i in members]
        far = meta.pop("leader_far", None)
        endpoint = None
        if far is not None:
            endpoint = far
        elif meta.get("leader_id"):
            lead = next((l for l in leaders if l["id"] == meta["leader_id"]), None)
            endpoint = list(lead["far_point"]) if lead else None
        layer = member_lines[0].get("layer") or "0"
        ann = build_annotation(member_lines, meta, layer, endpoint)
        joined = ann.pop("_join_text")
        ann["parsed"] = parse_annotation(joined, None, materials_cfg)
        out.append(ann)
    out.sort(key=lambda r: (r["layer"], r["type"], r["geom"]["x"], r["geom"]["y"],
                            r.get("text_raw") or ""))
    return out
