"""Part 7: 7.1 sheet segmentation + 7.5 detail records (7.9 tests 1 and 11).

Reference: tests/fixtures/architrave.dxf — one sheet, two detail regions
AR 1 (plasterboard build-up) and AR 2 (aluminium), each with its own title
marker (CIRCLE + tag + title + Scale: 1:5).
"""
import json
import subprocess
from pathlib import Path

import ezdxf
from click.testing import CliRunner

from gitail.cli import cli
from gitail.extract import extract_state
from gitail.identity import resolve
from gitail.index import build_index
from gitail.segment import build_sheet_details, segment_sheet
from gitail.semantics import load_materials

FIXTURE = Path(__file__).parent / "fixtures" / "architrave.dxf"
CFG = Path(__file__).parent.parent / "config" / "materials.yaml"


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


def _resolved(rows=None):
    cfg = load_materials(CFG)
    raw = extract_state(FIXTURE, cfg)
    resolved, _idmap, _, _ = resolve(raw, [], {"drawing": "A.204", "elements": {}})
    return resolved, cfg


def _repo_with_details(tmp_path, name="A.204.dxf"):
    """Tmp drawing repo with the architrave sheet extracted AND segmented,
    state + details committed (the drafter single-command flow)."""
    import shutil
    from gitail.segment import build_sheet_details, write_details_file
    repo = tmp_path / "repo"
    (repo / "drawings").mkdir(parents=True)
    (repo / "state").mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "t@t.t")
    git(repo, "config", "user.name", "t")
    dst = repo / "drawings" / name
    shutil.copy(FIXTURE, dst)
    cfg = load_materials(CFG)
    raw = extract_state(dst, cfg)
    drawing = Path(name).stem
    resolved, new_idmap, _, _ = resolve(raw, [], {"drawing": drawing, "elements": {}})
    storable = [{k: v for k, v in r.items()
                 if k not in ("status", "match_tier", "match_confidence") and not k.startswith("_")}
                for r in resolved]
    (repo / "state" / f"{drawing}.jsonl").write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in storable), encoding="utf-8")
    (repo / "state" / f"{drawing}.idmap.json").write_text(
        json.dumps(new_idmap, sort_keys=True, indent=2), encoding="utf-8")
    doc = ezdxf.readfile(str(dst))
    write_details_file(repo / "details", drawing,
                       build_sheet_details(drawing, resolved, cfg, {}, doc))
    git(repo, "add", ".")
    git(repo, "commit", "-m", "architrave with details")
    return repo


def test_1_sheet_splits_into_two_details():
    """7.9 #1: the reference sheet segments into AR 1 and AR 2."""
    resolved, _cfg = _resolved()
    seg = segment_sheet(resolved, "A.204")
    assert seg["segmentation"] == "certain"
    assert [r["tag"] for r in seg["regions"]] == ["AR 1", "AR 2"]
    counts = [len(r["record_indexes"]) for r in seg["regions"]]
    assert all(n > 0 for n in counts)
    assert sum(counts) == len(resolved)
    # regions sit side by side, each carrying its own scale
    assert [r["scale"] for r in seg["regions"]] == ["1:5", "1:5"]
    assert seg["regions"][0]["bbox"][2] <= seg["regions"][1]["bbox"][0]


def test_11_facets_agree_with_classifications():
    """7.9 #11: every flat facet field agrees with classifications[].

    Prefilled facets resolve to facet_nodes, and every facet node appears in
    at least one classification path; every classification carries its
    evidence (element_id + attribution_chain) back to the geometry.
    """
    from gitail.classify import FACET_NODES
    resolved, cfg = _resolved()
    doc = ezdxf.readfile(str(FIXTURE))
    payload = build_sheet_details("A.204", resolved, cfg, {}, doc)
    assert len(payload["details"]) == 2
    for d in payload["details"]:
        for c in d["classifications"]:
            assert c.get("path"), c
            assert c.get("attribution_chain"), c
            assert c.get("element_id") in d["element_ids"]
        pathed = {el for c in d["classifications"] for el in c["path"]}
        comps = [n for n in (d.get("facet_nodes") or []) if n.startswith("component.")]
        parts = [n for n in (d.get("facet_nodes") or []) if n.startswith("part.")]
        # component facets prefix paths (no foreign components ever appear);
        # part facets are subject descriptors (sibling context) and need no
        # geometric classification of their own.
        for node in comps:
            assert node in pathed, f"{node} not in any path of {d['detail_id']}"
        for c in d["classifications"]:
            for el in c["path"]:
                if el.startswith("component."):
                    assert el in comps, f"foreign {el} in {d['detail_id']}"
        assert set(parts) <= set((d.get("facet_nodes") or []))
        # slug mapping is consistent: every non-null junction/assembly slug
        # resolves to a facet node (or has no node — never a guessed id).
        for key in ("junction_type", "assembly"):
            slug = d.get(key)
            if slug is not None:
                nid = FACET_NODES.get(slug, FACET_NODES.get(f"{slug}_asm"))
                assert nid is None or nid in (d.get("facet_nodes") or [])
        assert d["detail_id"].startswith("d_")
        assert d["source_sheet"] == "A.204"
        assert d["reusable"] is True and d["fidelity"] == "full"
    by_tag = {d["detail_tag"]: d for d in payload["details"]}
    assert by_tag["AR 1"]["depends_on"] == ["A.109", "A.110"]
    assert by_tag["AR 1"]["performance"]["wet_area"] is True
    assert "PLASTERBOARD" in by_tag["AR 1"]["text_blob"]
    # prefill fires on the reference sheet (door jamb, interior):
    assert by_tag["AR 1"]["junction_type"] == "door_jamb"
    assert by_tag["AR 1"]["assembly"] == "door"
    assert by_tag["AR 1"]["context"] == "interior"
    assert by_tag["AR 1"]["classified_by"] == "auto"
    # quirk bound to jamb under the door component (brief 7.5 shape):
    assert ["component.door", "part.jamb", "attr.material", "value.quirk"] in \
        [c["path"] for c in by_tag["AR 1"]["classifications"]]


def test_bare_bubble_is_uncertain_whole_sheet():
    """A lone circle+tag without title/scale is a bubble, not a detail."""
    doc = ezdxf.new("R2018")
    msp = doc.modelspace()
    msp.add_circle((0, 0), 8, dxfattribs={"layer": "A"})
    msp.add_text("G1", dxfattribs={"layer": "A", "height": 5}).set_placement((0, 0))
    msp.add_line((-50, -50), (50, 50), dxfattribs={"layer": "A"})
    cfg = load_materials(CFG)
    from gitail.extract import extract_state_from_doc
    raw = extract_state_from_doc(doc, cfg)
    resolved, _, _, _ = resolve(raw, [], {"drawing": "X", "elements": {}})
    seg = segment_sheet(resolved, "X")
    assert seg["segmentation"] == "uncertain"
    assert len(seg["regions"]) == 1 and seg["regions"][0]["tag"] == "SHEET"


def test_segmentation_deterministic():
    resolved, cfg = _resolved()
    doc = ezdxf.readfile(str(FIXTURE))
    a = json.dumps(build_sheet_details("A.204", resolved, cfg, {}, doc), sort_keys=True)
    b = json.dumps(build_sheet_details("A.204", resolved, cfg, {}, doc), sort_keys=True)
    assert a == b


def test_index_carries_detail_rows(tmp_path):
    """Committed details/*.json lands in the detail table on index."""
    import sqlite3
    repo = _repo_with_details(tmp_path)
    db = tmp_path / "i.sqlite"
    build_index(repo, db)
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "SELECT detail_id, drawing, detail_tag, title, scale, segmentation,"
        " entity_count, depends_on, wet_area FROM detail ORDER BY detail_tag")]
    con.close()
    assert [r["detail_tag"] for r in rows] == ["AR 1", "AR 2"]
    assert all(r["segmentation"] == "certain" for r in rows)
    assert all(r["drawing"] == "A.204" for r in rows)
    ar1 = rows[0]
    assert json.loads(ar1["depends_on"]) == ["A.109", "A.110"]
    assert ar1["wet_area"] == 1


def test_cli_extract_writes_and_checks_details(tmp_path):
    import shutil
    dxf = tmp_path / "A.204.dxf"
    shutil.copy(FIXTURE, dxf)
    runner = CliRunner()
    r = runner.invoke(cli, ["extract", str(dxf),
                            "--state-dir", str(tmp_path / "state"),
                            "--details-dir", str(tmp_path / "details"),
                            "--drawings-dir", str(tmp_path),
                            "--thumbs-dir", str(tmp_path / "thumbs"),
                            "--crops-dir", str(tmp_path / "crops"),
                            "--config-dir", str(CFG.parent)])
    assert r.exit_code == 0, r.output
    det = tmp_path / "details" / "A.204.json"
    assert det.exists()
    assert [d["detail_tag"] for d in json.loads(det.read_text(encoding="utf-8"))["details"]] == ["AR 1", "AR 2"]
    r = runner.invoke(cli, ["extract", str(dxf),
                            "--state-dir", str(tmp_path / "state"),
                            "--details-dir", str(tmp_path / "details"),
                            "--drawings-dir", str(tmp_path),
                            "--thumbs-dir", str(tmp_path / "thumbs"),
                            "--crops-dir", str(tmp_path / "crops"),
                            "--config-dir", str(CFG.parent), "--check"])
    assert r.exit_code == 0, r.output
    assert "2 details" in r.output
    # CLI list surface
    db = tmp_path / "i.sqlite"
    repo = tmp_path / "repo2"
    (repo / "drawings").mkdir(parents=True)
    (repo / "state").mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "t@t.t")
    git(repo, "config", "user.name", "t")
    shutil.copy(FIXTURE, repo / "drawings" / "A.204.dxf")
    shutil.copy(tmp_path / "state" / "A.204.jsonl", repo / "state" / "A.204.jsonl")
    shutil.copy(tmp_path / "state" / "A.204.idmap.json", repo / "state" / "A.204.idmap.json")
    (repo / "details").mkdir()
    shutil.copy(det, repo / "details" / "A.204.json")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "architrave")
    build_index(repo, db)
    r = runner.invoke(cli, ["details", "--db", str(db)])
    assert r.exit_code == 0, r.output
    assert "AR 1" in r.output and "AR 2" in r.output
