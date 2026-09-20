"""8.8 tree-view search (acceptance 8.9 #2, #5, #6, #7, #9).

Synthetic library: a curtain-wall spandrel detail (capping + mullion), a
window detail (value-level glazing only) and a roof detail (capping) —
committed details/*.json + minimal state, then indexed like any repo.
"""
import json
import subprocess
from pathlib import Path

from click.testing import CliRunner

from gitail.cli import cli
from gitail.index import build_index
from gitail.taxonomy import load_taxonomy

CFG = Path(__file__).parent.parent / "config" / "materials.yaml"


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


def _state_row(eid, text="X"):
    return {"element_id": eid, "dxf_handle": "1", "type": "MTEXT",
            "layer": "A", "geom": {"x": 0.0, "y": 0.0, "bbox": [0, 0, 1, 1]},
            "text_raw": text, "parsed": None, "hatch_pattern": None,
            "dim_measurement": None, "dim_override": None,
            "fingerprint": "sha1:x"}


def _detail(did, title, classifications, conf_default="high"):
    return {"detail_id": did, "title": title, "detail_tag": did,
            "source_sheet": "S", "scale": "1:5", "projection": "section",
            "junction_type": None, "assembly": None, "context": None,
            "facet_nodes": [], "classifications": classifications,
            "classified_by": "none", "build_up": [], "total_thickness": None,
            "components": [], "performance": {}, "standards": [],
            "depends_on": [], "provenance": {"status": "unknown"},
            "fidelity": "full", "reusable": True, "thumbnail": None,
            "phash": None, "near_duplicates": [], "text_blob": title,
            "sheet_region_bbox": [0, 0, 10, 10], "segmentation": "certain",
            "entity_count": 1, "element_ids": ["e_1"]}


def _cl(path, conf="high", eid="e_1", **kw):
    entry = {"path": path, "confidence": conf,
             "attribution_chain": "leader", "element_id": eid}
    entry.update(kw)
    return entry


def make_library(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "state").mkdir(parents=True)
    (repo / "details").mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "t@t.t")
    git(repo, "config", "user.name", "t")
    specs = {
        "A.204": [_detail("d_c91b", "Typical Spandrel Detail", [
            _cl(["component.curtain_wall", "part.vision_panel", "attr.material",
                 "value.glass"]),
            _cl(["component.curtain_wall", "part.glazing_unit", "attr.material",
                 "value.glass"]),
            _cl(["component.curtain_wall", "part.capping", "attr.material",
                 "value.aluminium"]),
            _cl(["component.curtain_wall", "part.capping", "attr.thickness",
                 "value.3mm"], exact_value=3.0),
            _cl(["component.curtain_wall", "part.spandrel_panel",
                 "attr.material", "value.aluminium"]),
            _cl(["component.curtain_wall", "part.pressure_plate",
                 "attr.material", "value.steel"]),
        ])],
        "A.205": [_detail("d_w11d", "Typical Window Head", [
            # value-level only: no thickness measured on this sheet
            _cl(["component.window", "part.glazing_unit", "attr.material",
                 "value.glass"]),
        ])],
        "A.206": [_detail("d_r22f", "Parapet Cap Flashing", [
            _cl(["component.roof", "part.capping", "attr.material",
                 "value.steel"], conf="medium"),
        ])],
    }
    for drawing, details in specs.items():
        (repo / "state" / f"{drawing}.jsonl").write_text(
            json.dumps(_state_row("e_1", drawing)) + "\n", encoding="utf-8")
        (repo / "state" / f"{drawing}.idmap.json").write_text(
            json.dumps({"drawing": drawing, "elements": {}}), encoding="utf-8")
        (repo / "details" / f"{drawing}.json").write_text(
            json.dumps({"drawing": drawing, "segmentation": "certain",
                        "details": details}, sort_keys=True, indent=2),
            encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "library")
    db = tmp_path / "i.sqlite"
    build_index(repo, db)
    return repo, db


def tax():
    return load_taxonomy(None)


def test_2_glazing_returns_curtain_wall_once():
    """8.9 #2: the multi-parent glazing_unit lives under four components but
    each drawing appears exactly once (dedup by node, results by drawing)."""
    from gitail.treeview import search_tree
    repo, db = make_library(Path(__import__("tempfile").mkdtemp()))
    res = search_tree(db, tax(), "glazing")
    assert res["query"]["node"] == "part.glazing_unit"
    assert [r["detail_id"] for r in res["results"]] == ["d_c91b", "d_w11d"]


def test_9_window_component_finds_value_level_only():
    """8.9 #9: component search reaches value-level-only drawings (matched
    via window descendants — the curtain-wall sheet joins through its own
    glazing unit, which is correct multi-parent behaviour)."""
    from gitail.treeview import search_tree
    repo, db = make_library(Path(__import__("tempfile").mkdtemp()))
    res = search_tree(db, tax(), node_id="component.window")
    assert {r["detail_id"] for r in res["results"]} == {"d_c91b", "d_w11d"}
    window_only = next(r for r in res["results"] if r["detail_id"] == "d_w11d")
    assert window_only["matched_nodes"] == [
        "attr.material", "component.window", "part.glazing_unit", "value.glass"]


def test_capping_two_branches_ranked():
    from gitail.treeview import search_tree
    repo, db = make_library(Path(__import__("tempfile").mkdtemp()))
    res = search_tree(db, tax(), "capping")
    assert [r["detail_id"] for r in res["results"]] == ["d_c91b", "d_r22f"]
    # high-confidence curtain wall outranks medium roof (7.8)
    assert res["results"][0]["confidence"] == "high"
    # faceted counts follow subtree membership across the whole result set:
    # capping lives under four components; the spandrel sheet also carries
    # a glazing unit (window/door ancestors)
    assert res["facet_counts"] == {"component.balustrade": 2,
                                   "component.curtain_wall": 2,
                                   "component.door": 1,
                                   "component.parapet": 2,
                                   "component.roof": 2,
                                   "component.window": 1}


def test_5_pruned_tree_root_to_match_only():
    """8.9 #5: pruned trees carry root-to-match chains; siblings as counts."""
    from gitail.treeview import search_tree
    repo, db = make_library(Path(__import__("tempfile").mkdtemp()))
    res = search_tree(db, tax(), "capping")
    spandrel = next(r for r in res["results"] if r["detail_id"] == "d_c91b")

    def walk(entries, trail=()):
        for e in entries:
            if e.get("collapsed"):
                yield (trail + ("…collapsed…",), e)
                continue
            yield (trail + (e["id"],), e)
            yield from walk(e.get("children", []), trail + (e["id"],))

    nodes = dict(walk(spandrel["pruned_tree"]))
    # only the curtain-wall root survives; the other four parts collapse
    assert [e["id"] for e in spandrel["pruned_tree"]] == ["component.curtain_wall"]
    assert ("component.curtain_wall", "part.capping") in nodes
    # the matched part displays its attribute leaves (brief 8.8 shape)
    capped = nodes[("component.curtain_wall", "part.capping")]
    assert {c["id"] for c in capped["children"]} == {"attr.material",
                                                     "attr.thickness"}
    collapsed = [e for k, e in nodes.items() if k[-1] == "…collapsed…"]
    assert collapsed and collapsed[0]["parts"] == 4
    assert spandrel["collapsed_counts"]["sibling_parts"] == 4
    assert spandrel["collapsed_counts"]["total_classifications"] == 6
    # matched nodes highlighted across branches
    assert "part.capping" in spandrel["matched_nodes"]


def test_6_expand_returns_everything():
    """8.9 #6: expansion returns the full tree on demand."""
    from gitail.treeview import expand_detail
    repo, db = make_library(Path(__import__("tempfile").mkdtemp()))
    out = expand_detail(db, tax(), "d_c91b")
    assert len(out["classifications"]) == 6

    def leaves(entries):
        for e in entries:
            yield from leaves(e.get("children", []))
            yield e

    assert sum(len(e.get("classifications", [])) for e in leaves(out["full_tree"])) == 6


def test_7_unmatched_lands_outside_tree_but_searchable():
    """8.9 #7: no taxonomy node -> absent from tree, present below as
    unclassified (and still full-text findable via the element index)."""
    from gitail.resolve import insert_occurrences
    from gitail.treeview import search_tree
    import sqlite3
    repo, db = make_library(Path(__import__("tempfile").mkdtemp()))
    con = sqlite3.connect(str(db))
    n = insert_occurrences(con, {"quarantine_candidates": [{
        "raw_string": "EZY CAV FLANGE", "normalised": "ezy cav flange",
        "level_guess": "part", "attribute": None, "material": None,
        "part": "flange", "value": None, "qualifier": None,
        "element_id": "e_9", "detail_id": "d_w11d",
        "evidence": {"sibling_context": []}, "reason": "unknown-part:flange",
        "crop": None}]}, "A.205", "", "c" * 40, "2026-09-18T00:00:00")
    con.commit()
    con.close()
    assert n == 1
    res = search_tree(db, tax(), "ezy")
    assert res["query"]["node"] is None
    assert res["results"] == []
    assert len(res["unclassified"]) == 1
    assert res["unclassified"][0]["detail_id"] == "d_w11d"
    assert res["unclassified"][0]["matches"] == ["EZY CAV FLANGE"]


def test_cli_search_and_path_and_expand(tmp_path):
    repo, db = make_library(tmp_path)
    runner = CliRunner()
    r = runner.invoke(cli, ["search", "capping", "--db", str(db), "--json"])
    assert r.exit_code == 0, r.output
    import json as _json
    res = _json.loads(r.output)
    assert [x["detail_id"] for x in res["results"]] == ["d_c91b", "d_r22f"]
    r = runner.invoke(cli, ["search", "--path",
                            "component.curtain_wall/part.capping/attr.material/value.aluminium",
                            "--db", str(db), "--json"])
    assert r.exit_code == 0, r.output
    assert _json.loads(r.output)["query"]["node"] == "value.aluminium"
    r = runner.invoke(cli, ["search", "--expand", "d_c91b",
                            "--db", str(db), "--json"])
    assert r.exit_code == 0 and "full_tree" in r.output
