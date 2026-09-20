"""8.6 registration — proposing new taxonomy nodes (CLI + future web form).

A proposal carries evidence (example_detail_id) and a one-sentence definition;
undefined nodes are rejected outright. The duplicate check (similarity above
threshold against every node's label+synonyms) BLOCKS submission and offers
the existing match — this single check prevents most duplication.

Approved nodes promote into taxonomy/nodes/ (Git-backed: every vocabulary
change is a reviewable, revertible pull request). Approving a new node
reindexes quarantine (8.7) — hooked as a no-op until 8.7 lands.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from .taxonomy import (LEVELS, VALUE_TYPES, TaxonomyError, _read_yaml,
                       append_node, load_taxonomy, resolve_dir, slugify)


def _proposal_path(directory: Path, slug: str) -> Path:
    return directory / "proposals" / f"{slug}.yaml"


def _unique_slug(directory: Path, base: str, label: str) -> str:
    slug, n = base, 2
    while True:
        path = _proposal_path(directory, slug)
        if not path.exists():
            return slug
        try:
            if (_read_yaml(path).get("label") or "") == label:
                raise TaxonomyError(f"proposal {slug} is already pending")
        except TaxonomyError:
            raise
        except OSError:
            pass
        slug, n = f"{base}-{n}", n + 1


def propose(taxonomy_dir: str | Path | None, level: str, label: str,
            parents: list[str], definition: str, example_detail_id: str,
            synonyms: list[str] | None = None, value_type: str | None = None,
            unit: str | None = None, enum_members: list[str] | None = None,
            submitted_by: str = "cli",
            threshold: float = 0.75,
            allow_blank_definition: bool = False) -> dict:
    """Validate + duplicate-check + write proposals/<slug>.yaml.

    Raises TaxonomyError naming the blocking match on near-duplicates:
    'Did you mean `part.capping` (synonyms: cap, coping)?'

    allow_blank_definition is the 8.7.4 resolution-dialog path: "create a new
    branch" opens the form PRE-FILLED with the definition blank and required —
    approve() still refuses a blank definition until a human writes it.
    """
    from .taxonomy import _validate
    directory = resolve_dir(taxonomy_dir)
    if directory is None:
        raise TaxonomyError("no owned taxonomy/ — run `gitail taxonomy-init` first "
                            "so proposals land in version control")
    if level not in LEVELS:
        raise TaxonomyError(f"level must be one of {LEVELS}, got {level!r}")
    if not (label or "").strip():
        raise TaxonomyError("label is required")
    if not parents:
        raise TaxonomyError("at least one existing parent node id is required "
                            "(multi-parent is normal, not an edge case)")
    if not (definition or "").strip() and not allow_blank_definition:
        raise TaxonomyError("definition is required — one sentence; undefined "
                            "nodes are the main source of taxonomy rot")
    if not (example_detail_id or "").strip():
        raise TaxonomyError("example_detail_id is required — no node without evidence")
    if level == "attribute":
        if value_type not in VALUE_TYPES:
            raise TaxonomyError(f"attributes need value_type in {VALUE_TYPES}")
        if value_type == "numeric":
            unit = unit or "mm"
        if value_type == "enum" and not enum_members:
            raise TaxonomyError("enum attributes need initial enum_members")
    tax = load_taxonomy(directory)
    for p in parents:
        if p not in tax.nodes:
            raise TaxonomyError(f"unknown parent: {p}")
    if level == "value":
        for p in parents:
            if tax.nodes[p].get("value_type") == "numeric":
                raise TaxonomyError(
                    f"{p} is numeric — values canonicalise to millimetres and "
                    f"bucket for display, they never become nodes (8.5)")
    synonyms = sorted({s.strip() for s in (synonyms or []) if s.strip()})
    dupes = tax.check_duplicate(label.strip(), synonyms, threshold)
    if dupes:
        node, score, _name = dupes[0]
        syn = ", ".join((node.get("synonyms") or [])[:6])
        raise TaxonomyError(
            f"blocked: {label.strip()!r} matches existing `{node['id']}` "
            f"(similarity {score}) — did you mean `{node['id']}`"
            f"{f' (synonyms: {syn})' if syn else ''}?")
    slug = _unique_slug(directory, slugify(label), label.strip())
    proposal = {"level": level, "label": label.strip(), "slug": slug,
                "parents": list(parents), "synonyms": synonyms,
                "definition": definition.strip(),
                "example_detail_id": example_detail_id.strip(),
                "submitted_by": submitted_by, "status": "pending",
                "created_at": date.today().isoformat()}
    if level == "attribute":
        proposal["value_type"] = value_type
        if unit:
            proposal["unit"] = unit
        if enum_members:
            proposal["enum_members"] = list(enum_members)
    # candidate must satisfy every loader rule (parents, cycles, enum bounds).
    # A blank resolution-prefill definition validates against a placeholder —
    # the PROPOSAL keeps "" and approve() still refuses it (8.7.4).
    candidate = {"id": f"{level}.{slug}", **proposal}
    if allow_blank_definition and not (candidate.get("definition") or "").strip():
        candidate["definition"] = "(to be defined at review)"
    candidate.pop("status", None)
    candidate["created_by"] = "registration"
    candidate["drawing_count"] = 0
    candidate["deprecated_by"] = None
    trial = dict(tax.nodes)
    trial[candidate["id"]] = candidate
    _validate(trial)
    _proposal_path(directory, slug).parent.mkdir(parents=True, exist_ok=True)
    import yaml
    with open(_proposal_path(directory, slug), "w", encoding="utf-8", newline="\n") as f:
        yaml.safe_dump(proposal, f, sort_keys=False, allow_unicode=True)
    return {"slug": slug, "path": str(_proposal_path(directory, slug)),
            "id": candidate["id"], "status": "pending"}


def list_proposals(taxonomy_dir: str | Path | None) -> list[dict]:
    directory = resolve_dir(taxonomy_dir)
    if directory is None:
        raise TaxonomyError("no owned taxonomy/ — run `gitail taxonomy-init` first")
    out = []
    for path in sorted((directory / "proposals").glob("*.yaml")):
        try:
            data = _read_yaml(path)
        except OSError:
            continue
        if data.get("status", "pending") == "pending":
            out.append({**data, "path": str(path)})
    return out


def approve(taxonomy_dir: str | Path | None, slug: str,
            threshold: float = 0.75) -> dict:
    """Promote a pending proposal into taxonomy/nodes/; the duplicate check
    runs again so a meanwhile-registered near-twin still blocks."""
    import yaml
    directory = resolve_dir(taxonomy_dir)
    if directory is None:
        raise TaxonomyError("no owned taxonomy/ — run `gitail taxonomy-init` first")
    path = _proposal_path(directory, slug)
    if not path.exists():
        raise TaxonomyError(f"no pending proposal: {slug}")
    proposal = _read_yaml(path)
    if not (proposal.get("definition") or "").strip():
        raise TaxonomyError(
            f"proposal {slug} still has a blank definition — write the "
            f"one-sentence definition into {path} first (8.7.4: arriving from "
            f"a resolution dialog does not bypass review)")
    tax = load_taxonomy(directory)
    dupes = tax.check_duplicate(proposal["label"], proposal.get("synonyms", []),
                                threshold)
    dupes = [d for d in dupes if d[0]["id"] != f"{proposal['level']}.{slug}"]
    if dupes:
        node, score, _name = dupes[0]
        raise TaxonomyError(
            f"blocked: `{node['id']}` now covers this (similarity {score}) — "
            f"merge the proposal into it instead")
    node = {"id": f"{proposal['level']}.{slug}", "level": proposal["level"],
            "label": proposal["label"], "slug": slug,
            "parents": proposal["parents"],
            "synonyms": proposal.get("synonyms", []),
            "definition": proposal["definition"], "status": "approved",
            "created_by": "registration",
            "created_at": proposal.get("created_at", date.today().isoformat()),
            "drawing_count": 0, "deprecated_by": None}
    if proposal["level"] == "attribute":
        node["value_type"] = proposal["value_type"]
        if proposal.get("unit"):
            node["unit"] = proposal["unit"]
        if proposal.get("enum_members"):
            node["enum_members"] = proposal["enum_members"]
    trial = dict(tax.nodes)
    trial[node["id"]] = node
    from .taxonomy import _validate
    _validate(trial)
    node_file = append_node(directory, node)
    # a value admitted under an enum attribute EXTENDS that enum (8.5).
    enum_touched = None
    if node["level"] == "value":
        for p in node["parents"]:
            attr = trial[p]
            if attr.get("value_type") == "enum":
                members = list(attr.get("enum_members") or [])
                if node["slug"] not in members:
                    members.append(node["slug"])
                    attr_file = _node_file_for(directory, attr)
                    data = _read_yaml(attr_file)
                    for n in data.get("nodes", []) or []:
                        if n.get("id") == p:
                            n["enum_members"] = sorted(members)
                    with open(attr_file, "w", encoding="utf-8", newline="\n") as f:
                        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
                    enum_touched = p
    path.unlink()
    # hook: reindex quarantine here once 8.7 exists — newly approved nodes may
    # resolve pending clusters with no manual re-tagging.
    return {"id": node["id"], "file": str(node_file), "enum_extended": enum_touched}


def _node_file_for(directory: Path, node: dict) -> Path:
    from .taxonomy import _node_file
    return _node_file(directory, node)


def merge_proposal(taxonomy_dir: str | Path | None, slug: str, into_id: str) -> dict:
    """Fold a proposal into an existing node: its label + synonyms join the
    target's synonyms so the same annotation resolves forever after."""
    import yaml
    from .taxonomy import normalize
    directory = resolve_dir(taxonomy_dir)
    if directory is None:
        raise TaxonomyError("no owned taxonomy/ — run `gitail taxonomy-init` first")
    path = _proposal_path(directory, slug)
    if not path.exists():
        raise TaxonomyError(f"no pending proposal: {slug}")
    proposal = _read_yaml(path)
    tax = load_taxonomy(directory)
    current = tax.resolve(into_id)
    if current not in tax.nodes:
        raise TaxonomyError(f"unknown node: {into_id}")
    target = tax.nodes[current]
    additions = [proposal["label"]] + list(proposal.get("synonyms", []))
    have = {normalize(s) for s in ([target.get("label")] + list(target.get("synonyms") or []))}
    merged = list(target.get("synonyms") or [])
    for s in additions:
        if normalize(s) not in have:
            merged.append(s)
            have.add(normalize(s))
    target["synonyms"] = sorted(merged)
    node_file = append_node(directory, target)  # upserts by id, keeps order
    path.unlink()
    return {"merged": slug, "into": current, "synonyms": target["synonyms"],
            "file": str(node_file)}
