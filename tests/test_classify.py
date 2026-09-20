"""classify.py units: paths, folding, quarantine triggers, prefill, refacet."""
from gitail.classify import (compose_path, facet_components, facet_nodes,
                             is_component_word, is_material_word,
                             match_known_string, matches_ignore,
                             classify_attribution, prefill_facets, refacet,
                             resolve_material, resolve_part)
from gitail.taxonomy import load_taxonomy


def tax():
    return load_taxonomy(None)


def attr(material=None, part=None, value=None, qualifier=None, eid="e_1",
         conf="high", chain="leader"):
    return {"material": material, "part": part, "value": value,
            "qualifier": qualifier, "confidence": conf,
            "attribution_chain": chain, "element_id": eid}


def test_material_value_paths_with_component_prefix():
    t = tax()
    entries, cand = classify_attribution(
        t, attr("quirk", "jamb", 10.0), ["component.door"])
    assert cand is None
    paths = [e["path"] for e in entries]
    assert ["component.door", "part.jamb", "attr.material", "value.quirk"] in paths
    assert ["component.door", "part.jamb", "attr.thickness", "value.10mm"] in paths
    assert entries[0]["exact_value"] == 10.0
    assert entries[0]["element_id"] == "e_1"


def test_component_prefix_requires_ancestry():
    """Part-ful paths prefix only ancestral components (a lining is not a
    door part — never by proximity); material-only evidence attaches under
    every facet component (the detail genuinely involves it)."""
    t = tax()
    entries, cand = classify_attribution(
        t, attr("plasterboard", "lining", 10.0, "nominal"), ["component.door"])
    assert cand is None
    paths = [e["path"] for e in entries]
    assert ["part.lining", "attr.material", "value.plasterboard"] in paths
    assert all(not p or p[0] != "component.door" for p in paths)
    entries, cand = classify_attribution(
        t, attr("plasterboard", None, 10.0, "nominal"), ["component.door"])
    paths = [e["path"] for e in entries]
    assert ["component.door", "attr.material", "value.plasterboard"] in paths


def test_part_token_folding():
    """'plasterboard' parsed as a part token folds to material evidence;
    generic 'wall' folds via the component vocabulary. Neither quarantines."""
    t = tax()
    assert is_material_word(t, "plasterboard")
    assert is_component_word(t, "wall")
    entries, cand = classify_attribution(t, attr("tile", "wall"), [])
    assert cand is None
    assert ["attr.material", "value.tile"] in [e["path"] for e in entries]


def test_unknown_part_and_material_quarantine():
    t = tax()
    paths, cand = classify_attribution(t, attr("aluminium", "flange"), [])
    assert paths == []
    assert cand["level_guess"] == "part" and "flange" in cand["reason"]
    paths, cand = classify_attribution(t, attr("unobtanium", None, 5.0), [])
    assert paths == []
    assert cand["level_guess"] == "value"
    assert cand["attribute"] == "attr.material"


def test_exact_only_resolution():
    """'wall' must not resolve to part.finish via substring ('wall finish')."""
    t = tax()
    assert resolve_part(t, "wall") is None
    assert resolve_part(t, "cill") == "part.sill"
    assert resolve_material(t, "alu") == "value.aluminium"


def test_match_known_string():
    t = tax()
    assert match_known_string(t, "EZY CAV JAMB LINER") is None
    t.nodes["part.jamb_liner"]["synonyms"] = \
        sorted(set(t.nodes["part.jamb_liner"]["synonyms"]) | {"ezy cav jamb liner"})
    assert match_known_string(t, "EZY CAV JAMB LINER") == "part.jamb_liner"


def test_matches_ignore():
    t = tax()
    assert matches_ignore(t, "Scale: 1:5")
    assert matches_ignore(t, "A.204")
    assert matches_ignore(t, "REFER. A.109 + A.110 FOR BUILD UP DETAILS")
    assert matches_ignore(t, "DO NOT SCALE THIS DRAWING")
    assert not matches_ignore(t, "NOM. 10MM FLUSH JOINTED PLASTERBOARD")
    assert not matches_ignore(t, "EZY CAV FLANGE")


def test_prefill_facets():
    f = prefill_facets("Door jamb detail", ["10MM QUIRK AT DOOR JAMB",
                                            "TYPICAL WET AREA"])
    assert f["junction_type"] == "door_jamb"
    assert f["assembly"] == "door"
    assert f["context"] == "interior"
    assert f["classified_by"] == "auto"
    f = prefill_facets("AR 2", ["Scale: 1:5"])
    assert f["junction_type"] is None and f["classified_by"] == "none"


def test_facet_nodes_and_components():
    t = tax()
    facets = {"junction_type": "door_jamb", "assembly": "door",
              "context": "interior", "projection": None}
    assert facet_nodes(t, facets) == ["component.door", "part.jamb"]
    assert facet_components(t, facets) == ["component.door"]


def test_refacet_prefix_surgery():
    t = tax()
    record = {"detail_id": "d_x", "junction_type": None, "assembly": None,
              "context": None, "projection": None, "classified_by": "none",
              "classifications": [
                  {"path": ["part.jamb", "attr.material", "value.quirk"],
                   "confidence": "high", "attribution_chain": "leader",
                   "element_id": "e_1"}]}
    out = refacet(t, record, {"junction_type": "door_jamb",
                              "assembly": "door", "classified_by": "human"})
    assert out["classified_by"] == "human"
    assert out["facet_nodes"] == ["component.door", "part.jamb"]
    assert out["classifications"][0]["path"] == \
        ["component.door", "part.jamb", "attr.material", "value.quirk"]
    # evidence preserved byte-for-byte below the component prefix
    assert out["classifications"][0]["element_id"] == "e_1"
    import pytest
    with pytest.raises(ValueError):
        refacet(t, record, {"context": "underwater"})
