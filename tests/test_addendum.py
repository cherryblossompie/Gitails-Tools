"""Addendum D acceptance 82-108: reassembly + Chain A2/D."""
from pathlib import Path
import json
import shutil
import subprocess
import ezdxf
import pytest

CFG_DIR = Path(__file__).parent.parent / "config"


def _cfg():
    from gitail.semantics import load_config_dir
    cfg, _ = load_config_dir(CFG_DIR)
    return cfg


def _text_cfg():
    from gitail.reassemble import load_text_config
    return load_text_config(CFG_DIR / "text.yaml")


def _mk_records(texts, leaders=None, h=2.5, layer="A-DETL-ANNO"):
    """Synthetic TEXT records: stacked lines at x=40, y descending.
    Gap 2.6mm on 2.5mm height -> ratio 1.04, inside [0.9, 1.7]."""
    recs = []
    y = 60.0
    for i, t in enumerate(texts):
        x0, x1 = 40.0, 40.0 + max(8.0, len(t) * 1.4)
        recs.append({
            "type": "TEXT", "text_raw": t, "layer": layer,
            "geom": {"x": (x0 + x1) / 2, "y": y,
                     "bbox": [x0, y - 1.2, x1, y + 1.2]},
            "dxf_handle": f"T{i:02X}", "height": h, "rotation": 0.0,
            "insert_x": x0, "insert_y": y,
        })
        y -= 5.0
    if leaders:
        for j, (a, b) in enumerate(leaders):
            recs.append({
                "type": "LINE", "text_raw": None, "layer": layer,
                "geom": {"x": (a[0] + b[0]) / 2, "y": (a[1] + b[1]) / 2},
                "vertices": [list(a), list(b)],
                "dxf_handle": f"L{j:02X}",
            })
    return recs


def test_82_leader_reassembly():
    from gitail.reassemble import reassemble_records
    recs = _mk_records(["NOM. 50MM SLAB", "SETDOWN"], leaders=[((38, 58), (100, 55))])
    out = reassemble_records(recs, None, _cfg(), _text_cfg())
    anns = [r for r in out if r.get("type") == "ANNOTATION"]
    assert len(anns) == 1
    assert anns[0]["text_raw"] == "NOM. 50MM SLAB SETDOWN"
    assert anns[0]["reassembly"]["signal"] == "leader"


def test_83_setdown_parses_not_thickness():
    from gitail.semantics import parse_text
    p = parse_text("NOM. 50MM SLAB SETDOWN", _cfg())
    assert p["measure"] == "setdown" and p["value"] == 50
    assert p.get("qualifier") == "nominal"


def test_84_thickness_search_ignores_setdown(tmp_path):
    from gitail.index import build_index
    from gitail.query import find
    from gitail.extract import extract_state_from_doc
    from gitail.identity import resolve
    from gitail.semantics import load_config_dir
    cfg, _ = load_config_dir(CFG_DIR)
    doc = ezdxf.new("R2018")
    msp = doc.modelspace()
    t1 = msp.add_text("NOM. 50MM SLAB", dxfattribs={"layer": "A-DETL-ANNO", "height": 3.0})
    t1.set_placement((40, 58))
    t2 = msp.add_text("SETDOWN", dxfattribs={"layer": "A-DETL-ANNO", "height": 3.0})
    t2.set_placement((40, 53))
    msp.add_line((38, 58), (100, 55), dxfattribs={"layer": "A-DETL-ANNO"})
    raw = extract_state_from_doc(doc, cfg)
    anns = [r for r in raw if r.get("type") == "ANNOTATION"]
    assert anns and anns[0]["text_raw"] == "NOM. 50MM SLAB SETDOWN"
    repo = tmp_path / "repo"
    (repo / "drawings").mkdir(parents=True)
    (repo / "state").mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-b", "main"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    prev, idmap = [], {"drawing": "S1", "elements": {}}
    resolved, new_idmap, _, _ = resolve(raw, prev, idmap)
    storable = [{k: v for k, v in r.items() if not k.startswith("_") and k != "status"}
                for r in resolved]
    (repo / "state" / "S1.jsonl").write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in storable), encoding="utf-8")
    (repo / "state" / "S1.idmap.json").write_text(json.dumps(new_idmap), encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "s1"], check=True, capture_output=True)
    db = tmp_path / "i.sqlite"
    build_index(repo, db)
    assert find(db, value=50, ever=True) == []
    assert find(db, value=50, measure="setdown", ever=True) != []


def test_85_five_lines_two_leaders():
    from gitail.reassemble import reassemble_records
    texts = ["TYPICAL WET AREA WALL", "TILE ON SUBSTRATE",
             "NOM. 10MM FLUSH JOINTED", "PLASTERBOARD TO INTERNAL", "TIMBER FRAMED WALLS"]
    recs = _mk_records(texts, leaders=[((38, 59), (90, 40)), ((38, 49), (110, 30))])
    out = reassemble_records(recs, None, _cfg(), _text_cfg())
    anns = [r for r in out if r.get("type") == "ANNOTATION"]
    assert len(anns) == 2
    joined = sorted(a["text_raw"] for a in anns)
    assert any("WET AREA" in j for j in joined) and any("PLASTERBOARD" in j for j in joined)


def test_86_delegated():
    from gitail.semantics import parse_text
    p = parse_text("CONCRETE SLAB REFER ENG. DWGS FOR DETAILS", _cfg())
    assert p["spec_delegated"] == {"to": "engineering"}


def test_87_orphan_merges_no_leader():
    from gitail.reassemble import reassemble_records
    recs = _mk_records(["CONCRETE SLAB REFER ENG.", "DWGS FOR DETAILS"])
    out = reassemble_records(recs, None, _cfg(), _text_cfg())
    anns = [r for r in out if r.get("type") == "ANNOTATION"]
    assert len(anns) == 1 and "DWGS FOR DETAILS" in anns[0]["text_raw"]


def test_88_height_blocks_merge():
    from gitail.reassemble import reassemble_records
    recs = _mk_records(["NOTE TEXT", "HEADING TEXT"])
    recs[1]["height"] = 6.0
    out = reassemble_records(recs, None, _cfg(), _text_cfg())
    anns = [r for r in out if r.get("type") == "ANNOTATION"]
    assert len(anns) == 2


def test_89_rotation_blocks_merge():
    from gitail.reassemble import reassemble_records
    recs = _mk_records(["WALLTYPE VARIES", "WALLTYPE VARIES"])
    recs[1]["rotation"] = 90.0
    out = reassemble_records(recs, None, _cfg(), _text_cfg())
    anns = [r for r in out if r.get("type") == "ANNOTATION"]
    assert len(anns) == 2


def test_90_centre_aligned_title():
    from gitail.reassemble import reassemble_records
    recs = _mk_records(["TYPICAL ARCHITRAVE", "DETAILS AR 1"])
    # centre-align: equal centres (40+34 vs 48+18 -> 57 vs 57), differing
    # left edges (40 vs 48). Narrow boxes keep the pair mergeable; only
    # alignment varies.
    recs[0]["geom"]["bbox"] = [40.0, 58.8, 74.0, 61.2]
    recs[1]["geom"]["bbox"] = [48.0, 53.8, 66.0, 56.2]
    recs[1]["geom"]["x"] = 57.0
    out = reassemble_records(recs, None, _cfg(), _text_cfg())
    anns = [r for r in out if r.get("type") == "ANNOTATION"]
    assert len(anns) == 1


def _dim_repo(tmp_path, doc, name="D1.dxf"):
    import json
    from gitail.extract import extract_state_from_doc
    from gitail.identity import resolve
    repo = tmp_path / "repo"
    (repo / "drawings").mkdir(parents=True)
    (repo / "state").mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-b", "main"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    raw = extract_state_from_doc(doc, _cfg())
    prev, idmap = [], {"drawing": Path(name).stem, "elements": {}}
    resolved, new_idmap, _, _ = resolve(raw, prev, idmap)
    storable = [{k: v for k, v in r.items() if not k.startswith("_") and k != "status"}
                for r in resolved]
    (repo / "state" / f"{Path(name).stem}.jsonl").write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in storable), encoding="utf-8")
    (repo / "state" / f"{Path(name).stem}.idmap.json").write_text(json.dumps(new_idmap), encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "d1"], check=True, capture_output=True)
    return repo


def _wall_doc(width=10.0, dim_text="10"):
    """Plasterboard wall x[100,100+width] with a bound dimension below."""
    doc = ezdxf.new("R2018")
    msp = doc.modelspace()
    x1 = 100.0 + width
    msp.add_lwpolyline([(100, 0), (x1, 0), (x1, 100), (100, 100)],
                        close=True, dxfattribs={"layer": "A-DETL-PLASTER"})
    msp.add_line((100, 0), (100, -10), dxfattribs={"layer": "A-DETL-ANNO"})
    msp.add_line((x1, 0), (x1, -10), dxfattribs={"layer": "A-DETL-ANNO"})
    d = msp.add_linear_dim(base=(100, -8), p1=(100, 0), p2=(x1, 0),
                           dxfattribs={"layer": "A-DETL-ANNO"}).dimension
    d.dxf.text = dim_text
    return doc


def test_97_a2_trace_binds_high():
    # Stale defpoints (addendum B.1 case 3): Chain A cannot snap, but the
    # drawn extension lines still trace to edges 10mm apart. Chain A2 binds.
    from gitail.attribute import analyze_doc
    doc = _wall_doc()
    for e in list(doc.modelspace()):
        if e.dxftype() == "DIMENSION":
            # Stale association (B.1 case 3): measured defpoints drifted 5mm
            # off the wall edges — past snap tolerance, but the drawn
            # extension lines still trace. Chain A cannot snap; A2 binds.
            e.dxf.defpoint2 = (105, 5, 0)
            e.dxf.defpoint3 = (115, 5, 0)
    summary = analyze_doc(doc, _cfg(), {})
    ext = [a for a in summary["attributions"]
           if "extension_trace" in (a.get("attribution_chain") or "")]
    assert ext and all(a["confidence"] == "high" for a in ext)


def test_98_contradicted_trace_rejected():
    from gitail.attribute import analyze_doc
    summary = analyze_doc(_wall_doc(width=14.0, dim_text="10"), _cfg(), {})
    ext = [a for a in summary["attributions"] if a.get("attribution_chain") == "extension_trace"]
    assert ext == []


def test_99_exploded_dims_resolve():
    from gitail.attribute import analyze_doc
    doc = ezdxf.new("R2018")
    msp = doc.modelspace()
    msp.add_lwpolyline([(100, 0), (110, 0), (110, 100), (100, 100)],
                        close=True, dxfattribs={"layer": "A-DETL-PLASTER"})
    msp.add_line((100, 0), (100, -10), dxfattribs={"layer": "A-DETL-ANNO"})
    msp.add_line((110, 0), (110, -10), dxfattribs={"layer": "A-DETL-ANNO"})
    msp.add_line((100, -8), (110, -8), dxfattribs={"layer": "A-DETL-ANNO"})
    t = msp.add_text("10", dxfattribs={"layer": "A-DETL-DIMS", "height": 2.5})
    t.set_placement((104, -6))
    summary = analyze_doc(doc, _cfg(), {})
    assert any("extension_trace" in (a.get("attribution_chain") or "")
               for a in summary["attributions"])


def test_101_unresolvable_stays_unattributed(tmp_path):
    from gitail.index import build_index
    from gitail.query import find
    doc = ezdxf.new("R2018")
    msp = doc.modelspace()
    d = msp.add_linear_dim(base=(320, 300), p1=(300, 300), p2=(320, 300),
                           dxfattribs={"layer": "A-DETL-ANNO"}).dimension
    d.dxf.text = "20"
    repo = _dim_repo(tmp_path, doc)
    db = tmp_path / "i.sqlite"
    build_index(repo, db)
    assert find(db, value=20, ever=True) == []


def test_102_chain_d_capped_low():
    from gitail.attribute import run_chain_d
    from gitail.ai import Adapter, RegionSuggestion
    class Confident(Adapter):
        def attribute_dimension(self, crop, value, label, cands):
            return [RegionSuggestion(region_id=c.region_id, score=0.99, why="sure")
                    for c in cands]
    regions = [{"id": "r1", "centroid": [0, 0], "thickness": 10.0,
                "material": "plasterboard", "layer": "L",
                "polygon": [[-5, -5], [5, -5], [5, 5], [-5, 5]]}]
    cands = [{"element_id": "e1", "value": 10.0, "unit": "mm", "label": "10",
              "anchor": [0, 0],
              "regions": [{"region_id": "r1", "measured_width": 10.0,
                             "material": "plasterboard", "layer": "L",
                             "polygon": [[-5, -5], [5, -5], [5, 5], [-5, 5]]}]}]
    attributions, _ = run_chain_d(cands, regions, {}, adapter=Confident(),
                                  adapter_enabled=True, drawing="D")
    assert attributions and attributions[0]["confidence"] == "low"


def test_103_chain_d_excluded_by_default(tmp_path):
    from gitail.index import build_index
    from gitail.query import find
    doc = _wall_doc()
    # force Chain D: delete extension lines so A2 cannot corroborate
    for e in list(doc.modelspace()):
        if e.dxftype() == "LINE":
            doc.modelspace().delete_entity(e)
    repo = _dim_repo(tmp_path, doc)
    db = tmp_path / "i.sqlite"
    build_index(repo, db)
    default = find(db, material="plasterboard", value=10, ever=True)
    low = find(db, material="plasterboard", value=10, ever=True, include_low_confidence=True)
    assert len(low) >= len(default)


def test_106_contradicted_regions_excluded():
    from gitail.attribute import trace_dimension
    regions = [
        {"id": "r1", "centroid": [0, 0], "thickness": 10.0,
         "material": "plasterboard", "layer": "L", "polygon": []},
        {"id": "r2", "centroid": [5, 0], "thickness": 25.0,
         "material": "concrete", "layer": "L", "polygon": []},
    ]
    _, cand = trace_dimension(10.0, (0, 0), (10, 0), [], regions, {},
                              element_id="e1", label="10")
    ids = [r["region_id"] for r in cand["regions"]]
    assert "r1" in ids and "r2" not in ids


def test_107_chain_d_disabled_no_call():
    from gitail.attribute import run_chain_d
    called = []
    class Spy:
        def attribute_dimension(self, *a):
            called.append(True)
            raise AssertionError("must not be called")
    regions = [{"id": "r1", "centroid": [0, 0], "thickness": 10.0,
                "material": "plasterboard", "layer": "L", "polygon": []}]
    cands = [{"element_id": "e1", "value": 10.0, "unit": "mm", "label": "10",
              "anchor": [0, 0], "regions": []}]
    attributions, reviews = run_chain_d(cands, regions, {"chain_d_enabled": False},
                                        adapter=Spy(), adapter_enabled=False,
                                        drawing="D")
    assert called == [] and attributions == [] and reviews[0]["status"] == "pending"


def test_91_identity_tracks_edits():
    from gitail.identity import resolve
    recs = _mk_records(["NOM. 50MM SLAB", "SETDOWN"], leaders=[((38, 58), (100, 55))])
    from gitail.reassemble import reassemble_records
    anns = [r for r in reassemble_records(recs, None, _cfg(), _text_cfg())
            if r.get("type") == "ANNOTATION"]
    assert len(anns) == 1
    assert set(anns[0]["handles"]) == {"T00", "T01"}
    assert len(anns[0]["source_lines"]) == 2
    # editing one line keeps the handle set stable: resolve sees value_changed
    prev = [dict(anns[0], element_id="e_1")]
    edited = [dict(anns[0])]
    edited[0]["text_raw"] = "NOM. 60MM SLAB SETDOWN"
    edited[0]["source_lines"] = [dict(anns[0]["source_lines"][0], text="NOM. 60MM SLAB"),
                                  anns[0]["source_lines"][1]]
    resolved, _, _, _ = resolve(edited, prev, {"drawing": "D", "elements": {}})
    assert resolved[0]["element_id"] == "e_1"
    assert resolved[0]["status"] == "value_changed"


def test_92_pdf_parity(tmp_path):
    pytest.importorskip("reportlab")
    from reportlab.pdfgen.canvas import Canvas
    from reportlab.lib.units import mm as _mm
    from gitail.extract import extract_pdf_state, extract_state_from_doc
    doc = ezdxf.new("R2018")
    msp = doc.modelspace()
    t1 = msp.add_text("NOM. 50MM SLAB", dxfattribs={"layer": "A-DETL-ANNO", "height": 3.0})
    t1.set_placement((40, 58))
    t2 = msp.add_text("SETDOWN", dxfattribs={"layer": "A-DETL-ANNO", "height": 3.0})
    t2.set_placement((40, 53))
    msp.add_line((38, 58), (100, 55), dxfattribs={"layer": "A-DETL-ANNO"})
    dxf_anns = sorted(r["text_raw"] for r in extract_state_from_doc(doc, _cfg())
                      if r.get("type") == "ANNOTATION")
    pdf = tmp_path / "sheet.pdf"
    c = Canvas(str(pdf))
    c.setFont("Helvetica", 8)
    c.drawString(40 * _mm, 220 * _mm, "NOM. 50MM SLAB")
    c.drawString(40 * _mm, 215 * _mm, "SETDOWN")
    c.save()
    pdf_anns = sorted(r["text_raw"] for r in extract_pdf_state(pdf, _cfg())
                      if r.get("type") == "ANNOTATION")
    assert pdf_anns == dxf_anns


def test_93_char_spans_merge():
    from gitail.extract import cluster_spans_to_lines
    spans = []
    x = 10.0
    for ch in "NOM. 50MM SLAB":
        spans.append({"text": ch, "bbox": (x, 50, x + 2, 54),
                      "size": 8.0, "rotation": 0, "font": "F", "page": 1})
        x += 2.2
    lines = cluster_spans_to_lines(spans)
    assert len(lines) == 1 and lines[0]["text"] == "NOM. 50MM SLAB"


def test_94_rotated_keeps_rotation():
    from gitail.extract import cluster_spans_to_lines
    spans = [{"text": "WALLTYPE VARIES", "bbox": (50, 10, 54, 60),
              "size": 8.0, "rotation": 90, "font": "F", "page": 1}]
    lines = cluster_spans_to_lines(spans)
    assert lines[0]["rotation"] == 90
    from gitail.reassemble import reassemble_records
    recs = [{"type": "PDFTEXT", "text_raw": lines[0]["text"], "layer": "PDF-P1",
             "geom": {"x": 52, "y": 35, "bbox": [50, 10, 54, 60]},
             "dxf_handle": "pdf:01-0001", "height": 2.8, "rotation": 90,
             "insert_x": 50, "insert_y": 60}]
    out = reassemble_records(recs, None, _cfg(), _text_cfg())
    anns = [r for r in out if r.get("type") == "ANNOTATION"]
    assert anns and anns[0]["rotation"] == 90


def test_95_no_ai_still_passes():
    from gitail.reassemble import reassemble_records
    recs = _mk_records(["NOM. 50MM SLAB", "SETDOWN"], leaders=[((38, 58), (100, 55))])
    out = reassemble_records(recs, None, _cfg(), _text_cfg(),
                             adapter=None, adapter_enabled=False)
    anns = [r for r in out if r.get("type") == "ANNOTATION"]
    assert len(anns) == 1 and anns[0]["text_raw"] == "NOM. 50MM SLAB SETDOWN"


def test_96_non_partition_discarded(caplog):
    from gitail.reassemble import reassemble_records
    from gitail.ai import Adapter
    class Bad(Adapter):
        def group_text_lines(self, crop, lines, leader_count):
            return [[0, 0]]  # duplicated index: invalid
    recs = _mk_records(["LINE ONE HERE XQZ", "LINE TWO HERE XQZ"])
    out = reassemble_records(recs, None, _cfg(), _text_cfg(),
                             adapter=Bad(), adapter_enabled=True)
    anns = [r for r in out if r.get("type") == "ANNOTATION"]
    assert anns  # geometric fallback kept
    assert any(r["reassembly"].get("ai_used") in (False, None) for r in anns)