"""Part 7 acceptance (7.2 attribution + 7.8 bound search, first slice).

Reference: tests/fixtures/architrave.dxf (committed).
Covers 7.9 tests 2,3,4,6,7,8,9 (+5 depends_on as parsed refs).
Segmentation (1), phash (10), facet agreement (11) arrive with 7.1/7.6/7.7.
"""
from pathlib import Path

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "architrave.dxf"


def _repo_with_fixture(tmp_path, name="A.204.dxf"):
    import shutil
    import subprocess
    from gitail.cli import _load_idmap, _load_jsonl
    from gitail.extract import extract_state
    from gitail.identity import resolve
    from gitail.semantics import load_materials
    import json
    repo = tmp_path / "repo"
    (repo / "drawings").mkdir(parents=True)
    (repo / "state").mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-b", "main"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    dst = repo / "drawings" / name
    shutil.copy(FIXTURE, dst)
    cfg = load_materials(Path(__file__).parent.parent / "config" / "materials.yaml")
    raw = extract_state(dst, cfg)
    prev, idmap = [], {"drawing": Path(name).stem, "elements": {}}
    resolved, new_idmap, _, _ = resolve(raw, prev, idmap)
    storable = [{k: v for k, v in r.items()
                 if k not in ("status", "match_tier", "match_confidence") and not k.startswith("_")}
                for r in resolved]
    (repo / "state" / f"{Path(name).stem}.jsonl").write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in storable), encoding="utf-8")
    (repo / "state" / f"{Path(name).stem}.idmap.json").write_text(
        json.dumps(new_idmap, sort_keys=True, indent=2), encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "architrave"], check=True, capture_output=True)
    return repo


def test_2_tile_10_returns_nothing(tmp_path):
    """The 10s in this drawing belong to quirk and plasterboard — never tile."""
    from gitail.index import build_index
    from gitail.query import find
    repo = _repo_with_fixture(tmp_path)
    db = tmp_path / "i.sqlite"
    build_index(repo, db)
    assert find(db, material="tile", value=10, ever=True) == []


def test_3_plasterboard_10_nominal(tmp_path):
    from gitail.index import build_index
    from gitail.query import find
    repo = _repo_with_fixture(tmp_path)
    db = tmp_path / "i.sqlite"
    build_index(repo, db)
    hits = find(db, material="plasterboard", value=10, ever=True)
    assert len(hits) == 1
    assert hits[0]["qualifier"] == "nominal"


def test_4_quirk_10(tmp_path):
    from gitail.index import build_index
    from gitail.query import find
    repo = _repo_with_fixture(tmp_path)
    db = tmp_path / "i.sqlite"
    build_index(repo, db)
    hits = find(db, material="quirk", value=10, ever=True)
    assert len(hits) == 1
    assert hits[0]["confidence"] == "high"


def test_5_depends_on(tmp_path):
    from gitail.attribute import extract_references
    assert extract_references("REFER. A.109 + A.110 FOR BUILD UP DETAILS") == ["A.109", "A.110"]


def test_6_wet_area(tmp_path):
    from gitail.attribute import analyze_dxf
    from gitail.semantics import load_materials
    cfg = load_materials(Path(__file__).parent.parent / "config" / "materials.yaml")
    summary = analyze_dxf(FIXTURE, cfg, {})
    assert summary["wet_area"] is True


def test_7_every_numeric_value_has_chain(tmp_path):
    from gitail.attribute import analyze_dxf
    from gitail.semantics import load_materials
    cfg = load_materials(Path(__file__).parent.parent / "config" / "materials.yaml")
    summary = analyze_dxf(FIXTURE, cfg, {})
    assert summary["attributions"], "no attributions produced"
    for a in summary["attributions"]:
        assert a.get("attribution_chain"), a
    for u in summary["unattributed"]:
        assert u.get("raw_value") is not None


def test_8_build_up_without_dimensions():
    import ezdxf
    from gitail.attribute import analyze_doc
    from gitail.semantics import load_materials
    cfg = load_materials(Path(__file__).parent.parent / "config" / "materials.yaml")
    doc = ezdxf.readfile(str(FIXTURE))
    for e in list(doc.modelspace()):
        if e.dxftype() == "DIMENSION":
            doc.modelspace().delete_entity(e)
    summary = analyze_doc(doc, cfg, {})
    stack = [a for a in summary["attributions"] if a.get("confidence") == "medium"]
    assert stack, "Chain C produced no medium-confidence build-up without dimensions"


def test_9_floating_dimension_unattributed_and_unsearchable(tmp_path):
    from gitail.attribute import analyze_dxf
    from gitail.index import build_index
    from gitail.query import find
    from gitail.semantics import load_materials
    cfg = load_materials(Path(__file__).parent.parent / "config" / "materials.yaml")
    summary = analyze_dxf(FIXTURE, cfg, {})
    floating = [u for u in summary["unattributed"] if u.get("raw_value") == 20]
    assert floating, "the floating 20 dimension was not recorded unattributed"
    repo = _repo_with_fixture(tmp_path)
    db = tmp_path / "i.sqlite"
    build_index(repo, db)
    assert find(db, value=20, ever=True) == []
