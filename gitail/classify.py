"""Taxonomy classification bridge (feeds Part 8).

Every 7.2 bound value is tested against the taxonomy graph. A value whose
part, material or enum member matches no node goes to QUARANTINE (8.7) —
never into the tree, never blocking the upload. Resolvable values become
classifications with root-to-leaf paths, each carrying the element_id back
to geometry and revision history.

Path shape (brief 7.5): component and part levels are NODE ids, attribute
levels are attr ids, numeric values bucket for display (value.10mm) while the
exact float stays on the record for tolerance queries::

    {"path": ["component.door", "part.jamb", "attr.material", "value.aluminium"],
     "confidence": "high", "attribution_chain": "leader+geometry",
     "element_id": "e_7f3a91"}

Component prefixes come from confirmed facet nodes (7.7): a component
prefixes a classification only when it is an ancestor of (or equal to) the
path's part — never by proximity. Material-only paths attach under every
facet component. With no facets, paths start at part or attribute level and
the flat facet fields stay null (7.9 #11).

Part/material token folding: a parsed part token that names no part node but
names a material (e.g. "plasterboard" parsed as part) folds to material-only
evidence instead of quarantining — the token is vocabulary, just not a part.
"""
from __future__ import annotations

import re


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().casefold())


def matches_ignore(tax, text: str | None) -> bool:
    """8.7.4 action D: boilerplate that is never a taxonomy node. Ignored
    strings stay full-text searchable — this only gates quarantine candidacy
    and tree classification, never the element index."""
    norm = _norm(text)
    if not norm:
        return True
    for entry in (tax.ignore if tax else []) or []:
        e = _norm(entry)
        if not e:
            continue
        if norm == e or norm.startswith(e) or \
                re.search(r"\b" + re.escape(e) + r"\b", norm):
            return True
    for pat in (getattr(tax, "ignore_patterns", None) or []):
        try:
            if re.search(pat, str(text), re.IGNORECASE):
                return True
        except re.error:
            continue
    return False


def _value_id(material: str) -> str:
    return "value." + re.sub(r"[^a-z0-9]+", "_", material.casefold()).strip("_")


def _part_id(part: str) -> str:
    return "part." + re.sub(r"[^a-z0-9]+", "_", part.casefold()).strip("_")


def _bucket(value: float) -> str:
    v = float(value)
    return f"value.{int(v)}mm" if v.is_integer() else f"value.{v}mm"


# Addendum A.5 — measure word -> attribute leaf. Unknown measures fall back
# to thickness (never a guessed attribute id).
MEASURE_ATTRS = {
    "thickness": "attr.thickness",
    "setdown": "attr.setdown",
    "fall": "attr.fall",
    "setback": "attr.setback",
    "dimension": "attr.dimension",
}


def resolve_material(tax, material: str) -> str | None:
    """value.<material> node id, alias-aware, or None.

    The search fallback only fires on EXACT normalised matches (a label or
    synonym equal to the token). Substring hits ("wall" in "wall finish")
    must never resolve — a wrong branch corrupts history worse than quarantine.
    """
    if not material:
        return None
    vid = _value_id(material)
    if vid in tax.nodes:
        return tax.resolve(vid)
    norm = _norm(material)
    for node in tax.nodes.values():
        if node["level"] != "value" or node.get("deprecated_by"):
            continue
        names = [node.get("label") or ""] + list(node.get("synonyms") or [])
        if any(_norm(n) == norm for n in names):
            return tax.resolve(node["id"])
    return None


def resolve_part(tax, part: str) -> str | None:
    """part.<part> node id, alias-aware, or None (exact matches only — see
    resolve_material for why substrings must not resolve)."""
    if not part:
        return None
    pid = _part_id(part)
    if pid in tax.nodes:
        return tax.resolve(pid)
    norm = _norm(part)
    for node in tax.nodes.values():
        if node["level"] != "part" or node.get("deprecated_by"):
            continue
        names = [node.get("label") or ""] + list(node.get("synonyms") or [])
        if any(_norm(n) == norm for n in names):
            return tax.resolve(node["id"])
    return None


def is_material_word(tax, token: str) -> bool:
    return resolve_material(tax, token) is not None


def match_known_string(tax, text: str) -> str | None:
    """A previously resolved annotation string: exact normalised match
    against node labels/synonyms (8.7.4A — assigning adds the raw string as a
    synonym so the same annotation resolves automatically forever after, with
    no new notification). Returns the resolved node id or None."""
    norm = _norm(text)
    if not norm:
        return None
    for nid in sorted(tax.nodes):
        node = tax.nodes[nid]
        if node.get("deprecated_by"):
            continue
        names = [node.get("label") or ""] + list(node.get("synonyms") or [])
        if any(_norm(n) == norm for n in names):
            return tax.resolve(nid)
    return None


# Uppercase nouns that are sheet furniture, not vocabulary — the unknown-part
# guess below must not surface them (they go to quarantine/ignore instead).
_GUESS_STOPWORDS = frozenset("""
    DETAIL DETAILS DRAWING SHEET PLAN SECTION ELEVATION NOTES NOTE GENERAL
    TYPICAL NOMINAL VERIFY VERIFIED SITE DIMENSIONS DIMENSION UNLESS NOTED
    OTHERWISE SUPPLY INSTALL EXISTING PROPOSED REFER SCALE NORTH FINISH
    FINISHES PAINT
""".split())


def guess_unknown_part(text: str, materials_cfg: dict | None) -> str | None:
    """Unknown-noun fallback run at CLASSIFICATION time (never in parsed, so
    Part-3 semantics stay byte-stable): the longest ALLCAPS word (>=4 chars)
    that is not a material keyword, qualifier, unit or stopword.

    Without this, part tokens outside the config list (FLANGE, EZY CAV ...)
    never become part claims and can never reach quarantine (8.7) — the
    vocabulary would only ever learn words the config already knows.
    Deliberately unspecified dimensions (VARIES/TBC) are information, never
    vocabulary: callers skip the guess when they carry no material.
    """
    cfg = materials_cfg or {}
    materials = cfg.get("materials", {}) or {}
    skip: set[str] = set(_GUESS_STOPWORDS)
    for spec in materials.values():
        for kw in (spec or {}).get("keywords", []) or []:
            skip.add(str(kw).upper())
        for q in (spec or {}).get("qualifiers", []) or []:
            skip.add(str(q).upper())
    for q in (cfg.get("qualifiers", []) or []):
        skip.add(str(q).upper())
    skip.update({"MM", "THK", "NOM", "TYP", "MIN", "MAX"})
    best = None
    for word in re.findall(r"\b[A-Z]{4,}\b", str(text or "")):
        if word in skip:
            continue
        if best is None or len(word) > len(best):
            best = word
    return best.lower() if best else None


def is_component_word(tax, token: str) -> bool:
    """Generic nouns ("wall", "door") name components, not parts. Component
    linkage comes from facets — the part claim folds away instead of
    quarantining every generic mention. Matches full names and head nouns
    ("wall" in "External Wall")."""
    norm = _norm(token)
    if not norm or " " in norm:
        return False
    for node in tax.nodes.values():
        if node["level"] != "component" or node.get("deprecated_by"):
            continue
        names = [node.get("label") or ""] + list(node.get("synonyms") or [])
        for name in names:
            words = _norm(name).split()
            if norm in words and (len(words) == 1 or words[-1] == norm):
                return True
    return False


def compose_path(tax, facet_components: list[str], part_id: str | None,
                 material: str | None, value_num: float | None,
                 measure: str = "thickness") -> list[list[str]]:
    """Candidate tree paths for one bound fact. One path per applicable facet
    component (deduped); empty list when nothing resolves (caller quarantines).

    The measure selects the attribute leaf (addendum A.5): a setdown records
    under attr.setdown, never attr.thickness — a thickness search must not
    return a setdown.
    """
    part_node = tax.resolve(part_id) if part_id else None
    if part_node is not None and part_node not in tax.nodes:
        part_node = None
    value_node = resolve_material(tax, material) if material else None
    if part_node is None and value_node is None and value_num is None:
        return []
    attr_leaf = MEASURE_ATTRS.get(measure or "thickness", "attr.thickness")
    # applicable components: facet components ancestral to the part; for
    # part-less facts every facet component applies (material-only evidence).
    if part_node is not None:
        ancestors = tax.ancestors(part_node) | {part_node}
        comps = [c for c in (facet_components or [])
                 if tax.resolve(c) in ancestors and tax.resolve(c) in tax.nodes]
    else:
        comps = [c for c in (facet_components or []) if tax.resolve(c) in tax.nodes]
    comps = sorted(set(tax.resolve(c) for c in comps))
    tails: list[list[str]] = []
    if part_node is not None and value_node is not None:
        tails = [[part_node, "attr.material", value_node]]
        if value_num is not None:
            tails.append([part_node, attr_leaf, _bucket(value_num)])
    elif part_node is not None:
        tails = [[part_node]]
    elif value_node is not None:
        tails = [["attr.material", value_node]]
        if value_num is not None:
            tails.append([attr_leaf, _bucket(value_num)])
    elif value_num is not None:
        tails = [[attr_leaf, _bucket(value_num)]]
    if not comps:
        return tails
    out = []
    for tail in tails:
        for c in comps:
            out.append([c] + tail)
    # dedupe, keep order
    seen, paths = set(), []
    for p in out:
        key = tuple(p)
        if key not in seen:
            seen.add(key)
            paths.append(p)
    return paths


def classify_attribution(tax, attribution: dict,
                         facet_components: list[str]) -> tuple[list[dict], dict | None]:
    """One 7.2 attribution -> (classifications, quarantine_candidate|None).

    The candidate carries everything needed to re-derive the classification
    later (after a synonym lands or a node is approved) without re-reading
    the drawing.
    """
    material = attribution.get("material")
    part = attribution.get("part")
    value_num = attribution.get("value")
    part_id = resolve_part(tax, part) if part else None
    if part and part_id is None and not is_material_word(tax, part) \
            and not is_component_word(tax, part):
        return [], {"level_guess": "part", "attribute": None,
                    "reason": f"unknown-part:{part}"}
    value_node = resolve_material(tax, material) if material else None
    if material and value_node is None:
        return [], {"level_guess": "value", "attribute": "attr.material",
                    "reason": f"unknown-material:{material}"}
    paths = compose_path(tax, facet_components, part_id, material,
                         float(value_num) if value_num is not None else None,
                         attribution.get("measure") or "thickness")
    if not paths:
        return [], {"level_guess": "part", "attribute": None,
                    "reason": "unresolvable"}
    out = []
    for path in paths:
        entry: dict = {"path": path, "confidence": attribution.get("confidence"),
                       "attribution_chain": attribution.get("attribution_chain"),
                       "element_id": attribution.get("element_id")}
        if value_num is not None:
            entry["exact_value"] = float(value_num)
        if attribution.get("qualifier") is not None:
            entry["qualifier"] = attribution.get("qualifier")
        out.append(entry)
    return out, None


# --------------------------------------------------------------------------
# 7.7 facet pre-fill (heuristic now, LLM later) + confirm surgery

# First match wins; evaluated against title + annotation blob, case-insensitive.
JUNCTION_RULES: list[tuple[str, list[str]]] = [
    ("door_jamb", ["door", "jamb"]),
    ("window_head", ["window", "head"]),
    ("window_sill", ["window", "sill"]),
    ("architrave", ["architrave"]),
    ("threshold", ["threshold"]),
    ("movement", ["movement", "expansion", "control joint"]),
    ("sill", ["sill", "cill"]),
    ("head", ["head"]),
    ("jamb", ["jamb"]),
    ("capping", ["capping", "coping", "parapet cap"]),
    ("parapet", ["parapet"]),
    ("skirting", ["skirting"]),
    ("eave", ["eave"]),
    ("corner", ["corner"]),
]
ASSEMBLY_RULES: list[tuple[str, list[str]]] = [
    ("internal_partition", ["partition", "stud wall"]),
    ("external_wall", ["external wall", "cavity wall", "brick veneer", "facade"]),
    ("curtain_wall", ["curtain wall"]),
    ("window", ["window"]),
    ("door", ["door"]),
    ("roof", ["roof"]),
    ("floor", ["floor"]),
    ("ceiling", ["ceiling"]),
    ("stair", ["stair"]),
    ("balustrade", ["balustrade"]),
    ("threshold", ["threshold"]),
    ("parapet", ["parapet"]),
]
CONTEXT_WORDS = {
    "exterior": ["external", "exterior", "facade", "outside", "outdoor"],
    "interior": ["internal", "interior", "inside", "wet area", "bathroom",
                 "kitchen", "ensuite", "laundry"],
}
PROJECTION_WORDS = {"plan": ["plan"], "section": ["section", "detail"],
                    "elevation": ["elevation"], "axonometric": ["axonometric", "iso"]}
CONTEXT_ENUM = ("interior", "exterior", "both")
PROJECTION_ENUM = ("plan", "section", "elevation", "axonometric")

# Facet slug -> taxonomy node. Junctions resolve to parts, assemblies to
# components; unknown slugs contribute no node (never a guessed id).
FACET_NODES = {
    "door_jamb": "part.jamb", "window_head": "part.head",
    "window_sill": "part.sill", "architrave": "part.architrave",
    "threshold": "part.threshold", "movement": "part.movement_joint",
    "sill": "part.sill", "head": "part.head", "jamb": "part.jamb",
    "capping": "part.capping", "parapet": "part.capping",
    "skirting": "part.skirting", "eave": "part.eave", "corner": None,
    "internal_partition": "component.internal_wall",
    "external_wall": "component.external_wall",
    "curtain_wall": "component.curtain_wall", "window": "component.window",
    "door": "component.door", "roof": "component.roof",
    "floor": "component.floor", "ceiling": "component.ceiling",
    "stair": "component.stair", "balustrade": "component.balustrade",
    "threshold_asm": "component.threshold", "parapet_asm": "component.parapet",
}


def _rule_hit(blob: str, words: list[str]) -> bool:
    if len(words) > 1:
        return all(re.search(r"\b" + re.escape(w) + r"\b", blob) for w in words)
    return bool(re.search(r"\b" + re.escape(words[0]) + r"\b", blob))


def prefill_facets(title: str | None, texts: list[str]) -> dict:
    """Heuristic 7.7 pre-fill. Only fills what keyword evidence supports;
    everything else stays null for the uploader to confirm. The LLM upgrade
    slots in here (same return shape, plus thumbnail input)."""
    blob = _norm(f"{title or ''} {' '.join(texts or [])}")
    facets: dict = {"junction_type": None, "assembly": None, "context": None,
                    "projection": None}
    for slug, words in JUNCTION_RULES:
        if _rule_hit(blob, words):
            facets["junction_type"] = slug
            break
    for slug, words in ASSEMBLY_RULES:
        if _rule_hit(blob, words):
            facets["assembly"] = slug
            break
    for ctx, words in CONTEXT_WORDS.items():
        if any(re.search(r"\b" + re.escape(w) + r"\b", blob) for w in words):
            facets["context"] = ctx
            break
    for proj, words in PROJECTION_WORDS.items():
        if any(re.search(r"\b" + re.escape(w) + r"\b", blob) for w in words):
            facets["projection"] = proj
            break
    filled = {k: v for k, v in facets.items() if v is not None}
    if filled:
        facets["classified_by"] = "auto"
    else:
        facets["classified_by"] = "none"
    return facets


def facet_nodes(tax, facets: dict) -> list[str]:
    """Taxonomy node ids behind confirmed facets (dedupe, alias-resolved,
    unknowns dropped — never a guessed id)."""
    out = []
    for key in ("junction_type", "assembly"):
        slug = (facets or {}).get(key)
        nid = FACET_NODES.get(slug) if slug else None
        if nid is None and slug == "threshold" and key == "assembly":
            nid = FACET_NODES.get("threshold_asm")
        if nid is None and slug == "parapet" and key == "assembly":
            nid = FACET_NODES.get("parapet_asm")
        if nid and nid in tax.nodes:
            out.append(tax.resolve(nid))
    return sorted(set(out))


def facet_components(tax, facets: dict) -> list[str]:
    """Component-level facet nodes: the only legal classification prefixes."""
    return sorted(n for n in facet_nodes(tax, facets)
                  if tax.nodes.get(n, {}).get("level") == "component")


def refacet(tax, record: dict, facets: dict) -> dict:
    """Re-derive a detail record's facet fields + classification component
    prefixes after human confirm (pure path surgery — no drawing re-read).

    Only the leading component.* elements are touched; part/attribute/value
    levels and evidence fields are preserved byte-for-byte.
    """
    record = dict(record)
    for key in ("junction_type", "assembly", "context", "projection"):
        if facets.get(key) is not None:
            record[key] = facets[key]
    if facets.get("context") is not None and facets["context"] not in CONTEXT_ENUM:
        raise ValueError(f"context must be one of {CONTEXT_ENUM}")
    if facets.get("projection") is not None and facets["projection"] not in PROJECTION_ENUM:
        raise ValueError(f"projection must be one of {PROJECTION_ENUM}")
    record["classified_by"] = facets.get("classified_by", "human")
    nodes = facet_nodes(tax, record)
    record["facet_nodes"] = nodes
    comps = [n for n in nodes if tax.nodes.get(n, {}).get("level") == "component"]
    new_classifications = []
    for c in record.get("classifications", []) or []:
        path = [e for e in (c.get("path") or [])
                if not str(e).startswith("component.")]
        part_el = next((e for e in path if str(e).startswith("part.")), None)
        if part_el is not None:
            ancestors = tax.ancestors(part_el) | {part_el}
            use = sorted({tax.resolve(x) for x in comps if tax.resolve(x) in ancestors})
        else:
            use = sorted(set(comps))
        if not use:
            new_classifications.append({**c, "path": path})
        else:
            for comp in use:
                new_classifications.append({**c, "path": [comp] + path})
    # dedupe identical paths (same element, same chain)
    seen, deduped = set(), []
    for c in new_classifications:
        key = (tuple(c["path"]), c.get("element_id"), c.get("attribution_chain"))
        if key not in seen:
            seen.add(key)
            deduped.append(c)
    record["classifications"] = sorted(
        deduped, key=lambda c: (c["path"], c.get("element_id") or ""))
    return record
