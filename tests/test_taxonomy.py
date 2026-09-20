"""Part 8 (first slice): 8.1 graph + 8.6 registration gate.

Covers 8.9 tests 1 (multi-parent paths), 3 (duplicate blocked), 4 (synonym
search) and 8 (cycle rejected), plus the propose/approve/merge lifecycle.
Tree-search behaviour (8.8: tests 2, 5, 6, 9) and reference rewriting (test
10) arrive with their slices — merge_node file behaviour is covered here.
"""
import pytest
from click.testing import CliRunner

from gitail.cli import cli
from gitail.register import approve, list_proposals, merge_proposal, propose
from gitail.taxonomy import (TaxonomyError, init_taxonomy, load_taxonomy,
                             merge_node)

CFG = "config"


def _owned(tmp_path):
    """Repo-owned taxonomy copy (what taxonomy-init produces)."""
    target = tmp_path / "taxonomy"
    init_taxonomy(target)
    return target


def test_1_glazing_unit_four_parents_four_paths():
    """8.9 #1: one node, four parents, four rendered paths — never duplicated."""
    tax = load_taxonomy(None)
    node = tax.nodes["part.glazing_unit"]
    assert sorted(node["parents"]) == ["component.balustrade", "component.curtain_wall",
                                       "component.door", "component.window"]
    assert sum(1 for n in tax.nodes.values() if n.get("slug") == "glazing_unit") == 1
    paths = tax.paths("part.glazing_unit")
    assert len(paths) == 4
    assert {p[0] for p in paths} == {"component.window", "component.curtain_wall",
                                     "component.door", "component.balustrade"}
    forest = tax.render()
    hits = []

    def walk(entries, trail):
        for e in entries:
            here = trail + [e["id"]]
            if e["id"] == "part.glazing_unit":
                hits.append(here)
            walk(e["children"], here)

    walk(forest, [])
    assert len(hits) == 4  # visible in all four paths


def test_3_coping_blocked_with_capping_offered(tmp_path):
    """8.9 #3: registering `Coping` is blocked, part.capping is offered."""
    owned = _owned(tmp_path)
    with pytest.raises(TaxonomyError, match=r"part\.capping"):
        propose(owned, "part", "Coping", ["component.roof"],
                "Profile closing the top.", "d_0000", threshold=0.75)


def test_4_cill_finds_sill_via_synonym():
    """8.9 #4: synonym matching — searching `cill` returns part.sill."""
    tax = load_taxonomy(None)
    hits = tax.search("cill")
    assert hits and hits[0][0]["id"] == "part.sill"


def test_8_cycle_rejected_with_path(tmp_path):
    """8.9 #8: a node made its own ancestor fails load, naming the path."""
    import yaml
    owned = _owned(tmp_path)
    parts = owned / "nodes" / "parts.yaml"
    data = yaml.safe_load(parts.read_text(encoding="utf-8"))
    data["nodes"].append({"id": "part.loop_a", "level": "part", "label": "Loop A",
                          "slug": "loop_a", "parents": ["component.roof"],
                          "synonyms": [], "definition": "Cycle helper A.",
                          "status": "approved", "created_by": "seed",
                          "created_at": "2026-03-04", "drawing_count": 0,
                          "deprecated_by": None})
    data["nodes"].append({"id": "part.loop_b", "level": "part", "label": "Loop B",
                          "slug": "loop_b", "parents": ["part.loop_a"],
                          "synonyms": [], "definition": "Cycle helper B.",
                          "status": "approved", "created_by": "seed",
                          "created_at": "2026-03-04", "drawing_count": 0,
                          "deprecated_by": None})
    for n in data["nodes"]:
        if n["id"] == "part.loop_a":
            n["parents"] = ["part.loop_b"]
    parts.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
                     encoding="utf-8")
    with pytest.raises(TaxonomyError, match=r"cycle rejected.*loop"):
        load_taxonomy(owned)


def test_seed_loads_clean():
    tax = load_taxonomy(None)
    comps = [n for n in tax.nodes.values() if n["level"] == "component"]
    assert len(comps) == 21  # brief 8.2 seed list, exactly
    assert {a["id"] for a in tax.nodes.values() if a["level"] == "attribute"} >= {
        "attr.material", "attr.thickness", "attr.finish", "attr.qualifier"}
    assert tax.resolve("part.capping") == "part.capping"


def test_propose_approve_lifecycle(tmp_path):
    owned = _owned(tmp_path)
    res = propose(owned, "part", "Drip Groove", ["component.window"],
                  "Slot shedding water under a sill.", "d_a41f",
                  synonyms=["drip, drip channel"], submitted_by="t")
    assert res["id"] == "part.drip_groove" and res["status"] == "pending"
    assert len(list_proposals(owned)) == 1
    out = approve(owned, "drip_groove")
    assert out["id"] == "part.drip_groove"
    assert list_proposals(owned) == []
    tax = load_taxonomy(owned)
    assert tax.nodes["part.drip_groove"]["parents"] == ["component.window"]
    assert tax.search("drip channel")[0][0]["id"] == "part.drip_groove"


def test_merge_proposal_adds_synonym_and_resolves(tmp_path):
    owned = _owned(tmp_path)
    propose(owned, "part", "Mesh Guard", ["component.window"],
            "Framed insect mesh.", "d_a41f")
    out = merge_proposal(owned, "mesh_guard", "part.insect_screen")
    assert out["into"] == "part.insect_screen"
    assert list_proposals(owned) == []
    tax = load_taxonomy(owned)
    # the same string now classifies automatically — no new notification.
    assert tax.search("mesh guard")[0][0]["id"] == "part.insect_screen"


def test_merge_node_deprecates_rewrites_and_aliases(tmp_path):
    """8.9 #10 (file level): deprecating rewrites references, nothing lost."""
    import yaml
    owned = _owned(tmp_path)
    propose(owned, "part", "Cover Strip", ["component.roof"],
            "Strip covering a joint.", "d_0001")
    approve(owned, "cover_strip")
    out = merge_node(owned, "part.cover_strip", "part.capping", actor="t")
    assert out["deprecated"] == "part.cover_strip"
    tax = load_taxonomy(owned)
    assert tax.nodes["part.cover_strip"]["deprecated_by"] == "part.capping"
    assert tax.resolve("part.cover_strip") == "part.capping"
    assert tax.nodes["part.cover_strip"] not in [
        n for n in tax.nodes.values() if not n.get("deprecated_by")]
    assert (tax.nodes["part.cover_strip"]["label"]) == "Cover Strip"  # never deleted


def test_cli_tree_register_proposals(tmp_path):
    owned = _owned(tmp_path)
    runner = CliRunner()
    r = runner.invoke(cli, ["tree", "--taxonomy-dir", str(owned), "--search", "cill"])
    assert r.exit_code == 0, r.output
    assert "part.sill" in r.output
    r = runner.invoke(cli, ["register", "--level", "part", "--label", "Coping",
                            "--parents", "component.roof",
                            "--definition", "Top profile.",
                            "--example-detail", "d_1",
                            "--taxonomy-dir", str(owned),
                            "--config-dir", CFG])
    assert r.exit_code == 1 and "part.capping" in r.output
    r = runner.invoke(cli, ["register", "--level", "part", "--label", "Drip Groove",
                            "--parents", "component.window",
                            "--definition", "Slot shedding water.",
                            "--example-detail", "d_1",
                            "--taxonomy-dir", str(owned),
                            "--config-dir", CFG])
    assert r.exit_code == 0, r.output
    r = runner.invoke(cli, ["proposals", "--taxonomy-dir", str(owned)])
    assert r.exit_code == 0 and "drip_groove" in r.output
    r = runner.invoke(cli, ["proposals", "approve", "drip_groove",
                            "--taxonomy-dir", str(owned),
                            "--config-dir", CFG])
    assert r.exit_code == 0, r.output
    r = runner.invoke(cli, ["tree", "--taxonomy-dir", str(owned),
                            "--node", "component.window", "--json"])
    assert r.exit_code == 0 and "part.drip_groove" in r.output
