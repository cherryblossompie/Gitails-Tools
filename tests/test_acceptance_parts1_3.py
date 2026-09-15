"""Acceptance tests — Parts 1-3 (brief, adapted: history/index deferred).

1. Same DXF twice -> byte-identical .jsonl
2. Re-save without edits -> zero changed elements
3. 3mm->2mm GLASS -> exactly one value_changed, same element_id
4. Erase+redraw identically -> zero changes (tier-2)
5. Move whole detail 500mm -> one translation event, zero element changes
6. Move one line 3mm -> one moved element, same ID
7. Unparseable annotation kept, parsed null, text_raw searchable
Plus semantic pattern table from the brief.
"""
import json
from pathlib import Path

import ezdxf
import pytest
from click.testing import CliRunner

from gitail.cli import cli
from gitail.extract import extract_state
from gitail.identity import resolve
from gitail.semantics import load_materials, parse_text

CFG = Path(__file__).parent.parent / "config" / "materials.yaml"


def materials():
    return load_materials(CFG)


def make_doc():
    return ezdxf.new("R2018")


def add_glass_detail(msp, glass_text="3mm GLASS", glass_pos=(1240.5, 880.0)):
    msp.add_mtext(glass_text, dxfattribs={"layer": "A-DETL-ANNO"}).dxf.insert = glass_pos
    msp.add_mtext("12 THK PLYWOOD", dxfattribs={"layer": "A-DETL-ANNO"}).dxf.insert = (1240.5, 860.0)
    msp.add_line((0, 0), (100, 0), dxfattribs={"layer": "A-DETL-STEL"})
    msp.add_lwpolyline([(0, 0), (50, 0), (50, 20), (0, 20)], close=True,
                       dxfattribs={"layer": "A-DETL-CONC"})
    h = msp.add_hatch(dxfattribs={"layer": "A-DETL-CONC"})
    h.set_pattern_fill("AR-CONC")
    h.paths.add_polyline_path([(0, 0), (50, 0), (50, 20), (0, 20)], is_closed=True)


def save(doc, path):
    doc.saveas(str(path))


def run_extract(dxf, state_dir, config_dir=None):
    runner = CliRunner()
    args = [str(dxf), "--state-dir", str(state_dir)]
    if config_dir:
        args += ["--config-dir", str(config_dir)]
    else:
        args += ["--config-dir", str(CFG.parent)]
    res = runner.invoke(cli, ["extract"] + args)
    assert res.exit_code == 0, res.output
    return res.output


def read_jsonl(path):
    return [json.loads(l) for l in Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]


# 1 — byte-identical
def test_1_extract_twice_byte_identical(tmp_path):
    dxf = tmp_path / "D-101.dxf"
    doc = make_doc()
    add_glass_detail(doc.modelspace())
    save(doc, dxf)
    st = tmp_path / "state"
    run_extract(dxf, st)
    first = (st / "D-101.jsonl").read_bytes()
    run_extract(dxf, st)
    second = (st / "D-101.jsonl").read_bytes()
    assert first == second


# 2 — re-save, zero changes
def test_2_resave_zero_changes(tmp_path):
    dxf = tmp_path / "D-101.dxf"
    doc = make_doc()
    add_glass_detail(doc.modelspace())
    save(doc, dxf)
    cfg = materials()
    v1 = extract_state(dxf, cfg)
    # simulate AutoCAD re-save: read + save copy (entity order/handles preserved)
    doc2 = ezdxf.readfile(str(dxf))
    dxf2 = tmp_path / "D-101-resave.dxf"
    doc2.saveas(str(dxf2))
    v2 = extract_state(dxf2, cfg)
    idmap = {"drawing": "D-101", "elements": {}}
    r1, idmap, ev, _ = resolve(v1, [], idmap)
    r2, _, ev2, _ = resolve(v2, r1, idmap)
    assert ev2 is None
    assert {r["status"] for r in r2} == {"unchanged"}


# 3 — value change keeps ID
def test_3_glass_3mm_to_2mm(tmp_path):
    dxf1 = tmp_path / "v1.dxf"
    doc = make_doc()
    add_glass_detail(doc.modelspace(), glass_text="3mm GLASS")
    save(doc, dxf1)
    cfg = materials()
    v1raw = extract_state(dxf1, cfg)
    idmap = {"drawing": "D-101", "elements": {}}
    v1, idmap, _, _ = resolve(v1raw, [], idmap)

    doc2 = ezdxf.readfile(str(dxf1))
    for e in doc2.modelspace():
        if e.dxftype() == "MTEXT" and "3mm" in (e.text or ""):
            e.text = "2mm GLASS"
    dxf2 = tmp_path / "v2.dxf"
    doc2.saveas(str(dxf2))
    v2raw = extract_state(dxf2, cfg)
    v2, _, _, _ = resolve(v2raw, v1, idmap)

    changed = [r for r in v2 if r["status"] == "value_changed"]
    assert len(changed) == 1
    assert changed[0]["text_raw"] == "2mm GLASS"
    assert changed[0]["parsed"]["value"] == 2
    # same element_id as the 3mm row
    old = next(r for r in v1 if r["text_raw"] == "3mm GLASS")
    assert changed[0]["element_id"] == old["element_id"]
    assert changed[0]["match_tier"] == "handle"


# 4 — erase + redraw identical -> tier-2, zero changes
def test_4_erase_redraw_identical(tmp_path):
    cfg = materials()
    doc = make_doc()
    add_glass_detail(doc.modelspace(), glass_text="3mm GLASS")
    dxf1 = tmp_path / "v1.dxf"
    save(doc, dxf1)
    v1raw = extract_state(dxf1, cfg)
    idmap = {"drawing": "D-101", "elements": {}}
    v1, idmap, _, _ = resolve(v1raw, [], idmap)

    # rebuild from scratch with identical geometry/text but fresh handles
    doc2 = make_doc()
    add_glass_detail(doc2.modelspace(), glass_text="3mm GLASS")
    dxf2 = tmp_path / "v2.dxf"
    save(doc2, dxf2)
    v2raw = extract_state(dxf2, cfg)
    # force handles to differ from v1 so tier-1 cannot fire
    for r in v2raw:
        r["dxf_handle"] = "NEW" + r["dxf_handle"]
    v2, _, _, _ = resolve(v2raw, v1, idmap)
    assert {r["status"] for r in v2} == {"unchanged"}
    assert {r["element_id"] for r in v1} == {r["element_id"] for r in v2}
    assert any(r["match_tier"] == "fingerprint" for r in v2)


# 5 — whole-drawing 500mm move -> translation event, zero changes
def test_5_global_move_500mm(tmp_path):
    cfg = materials()
    doc = make_doc()
    add_glass_detail(doc.modelspace())
    # need >=3 handle-matched entities; we have 5
    dxf1 = tmp_path / "v1.dxf"
    save(doc, dxf1)
    v1raw = extract_state(dxf1, cfg)
    idmap = {"drawing": "D-101", "elements": {}}
    v1, idmap, _, _ = resolve(v1raw, [], idmap)

    doc2 = ezdxf.readfile(str(dxf1))
    for e in doc2.modelspace():
        try:
            e.translate(500, 0, 0)
        except Exception:
            pass
    dxf2 = tmp_path / "v2.dxf"
    doc2.saveas(str(dxf2))
    v2raw = extract_state(dxf2, cfg)
    v2, _, event, _ = resolve(v2raw, v1, idmap)
    assert event is not None
    assert event["dx"] == pytest.approx(500, abs=1.0)
    assert {r["status"] for r in v2} == {"unchanged"}


# 6 — one line moved 3mm
def test_6_single_line_moved_3mm(tmp_path):
    cfg = materials()
    doc = make_doc()
    msp = doc.modelspace()
    msp.add_line((0, 0), (100, 0), dxfattribs={"layer": "A-DETL-STEL"})
    msp.add_line((0, 50), (100, 50), dxfattribs={"layer": "A-DETL-STEL"})
    dxf1 = tmp_path / "v1.dxf"
    save(doc, dxf1)
    v1raw = extract_state(dxf1, cfg)
    idmap = {"drawing": "T", "elements": {}}
    v1, idmap, _, _ = resolve(v1raw, [], idmap)

    doc2 = ezdxf.readfile(str(dxf1))
    lines = [e for e in doc2.modelspace() if e.dxftype() == "LINE"]
    # move first line +3mm in Y
    lines[0].dxf.start = (lines[0].dxf.start.x, lines[0].dxf.start.y + 3, 0)
    lines[0].dxf.end = (lines[0].dxf.end.x, lines[0].dxf.end.y + 3, 0)
    dxf2 = tmp_path / "v2.dxf"
    doc2.saveas(str(dxf2))
    v2raw = extract_state(dxf2, cfg)
    v2, _, _, _ = resolve(v2raw, v1, idmap)
    moved = [r for r in v2 if r["status"] == "moved"]
    assert len(moved) == 1
    # same ID as one of the v1 lines
    assert moved[0]["element_id"] in {r["element_id"] for r in v1}
    # both positions recorded
    assert moved[0]["geom"]["y"] == pytest.approx(3.0, abs=0.2)


# 7 — unparseable kept
def test_7_unparseable_kept_searchable(tmp_path):
    cfg = materials()
    doc = make_doc()
    doc.modelspace().add_mtext("XYZ FOOBAR ???", dxfattribs={"layer": "A-DETL-ANNO"}).dxf.insert = (0, 0)
    dxf = tmp_path / "odd.dxf"
    save(doc, dxf)
    rows = extract_state(dxf, cfg)
    assert len(rows) == 1
    assert rows[0]["parsed"] is None
    assert rows[0]["text_raw"] == "XYZ FOOBAR ???"


@pytest.mark.parametrize("raw,expected", [
    ("2mm GLASS", {"material": "glass", "value": 2, "unit": "mm"}),
    ("GLASS 2mm", {"material": "glass", "value": 2, "unit": "mm"}),
    ("12 THK PLYWOOD", {"material": "plywood", "value": 12, "unit": "mm"}),
    ("6.38 LAMINATED", {"material": "glass", "value": 6.38, "unit": "mm", "qualifier": "laminated"}),
    ("75x50 SHS", {"material": "steel", "profile": "SHS", "dims": [75, 50]}),
    ("R2.5 BATT INSUL", {"material": "insulation", "r_value": 2.5}),
    ("2mm TOUGHENED GLASS", {"material": "glass", "value": 2, "unit": "mm", "qualifier": "toughened"}),
])
def test_semantics_table(raw, expected):
    got = parse_text(raw, materials())
    assert got is not None
    for k, v in expected.items():
        assert got.get(k) == v
