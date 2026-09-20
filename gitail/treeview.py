"""8.8 tree-view search: the navigable library surface.

A search for a sub-element returns, per matching drawing, ONLY the path from
root to the matched node — siblings along that path collapse to counts — with
the full tree one expand away (firm libraries return hundreds of results;
shipping every full tree makes search feel broken).

Matching rule: a node at any level matches all drawings classified under it
OR ANY DESCENDANT (searching component.window finds value-level glazing
classifications). Synonyms and definition text both match (coping finds
capping). A drawing matching in several branches appears ONCE with all
matched nodes highlighted. ``unclassified.*`` hits list separately below —
never interleaved. Every leaf links to its element_id and hence to revision
history (Parts 1-2).
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

CONF_RANK = {"high": 0, "medium": 1, "low": 2}
STATUS_RANK = {"as_built": 0, "approved": 1, "concept": 2}


def _con(db: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    return con


def current_details(db: Path) -> dict:
    """Latest detail record per detail_id: the searchable library snapshot."""
    con = _con(db)
    try:
        names = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "detail" not in names:
            return {}
        rows = con.execute(
            "SELECT detail_id, drawing, title, record, commit_date, commit_sha"
            " FROM detail ORDER BY commit_date DESC, rowid DESC").fetchall()
    finally:
        con.close()
    out = {}
    for r in rows:
        did = r["detail_id"]
        if did in out:
            continue
        try:
            rec = json.loads(r["record"] or "{}")
        except ValueError:
            continue
        out[did] = {"detail_id": did, "drawing": r["drawing"],
                    "title": r["title"] or (rec.get("title") or did),
                    "record": rec, "commit_date": r["commit_date"],
                    "commit_sha": r["commit_sha"]}
    return out


def _label(tax, node_id: str) -> str:
    node = tax.nodes.get(node_id) if tax else None
    if node and node.get("label"):
        return node["label"]
    if "." in node_id:
        return node_id.split(".", 1)[1].replace("_", " ")
    return node_id


def build_forest(record: dict, tax) -> list[dict]:
    """Nest a detail's classification paths into trees. Leaves carry their
    classifications (confidence, exact value, element links)."""
    roots: dict[str, dict] = {}
    order: list[str] = []
    for c in record.get("classifications", []) or []:
        path = [e for e in (c.get("path") or []) if e]
        if not path:
            continue
        trail: list[dict] = []
        for depth, nid in enumerate(path):
            if depth == 0:
                if nid not in roots:
                    roots[nid] = {"id": nid, "label": _label(tax, nid),
                                  "children": [], "classifications": []}
                    order.append(nid)
                entry = roots[nid]
            else:
                nxt = next((ch for ch in trail[-1]["children"]
                            if not ch.get("collapsed") and ch["id"] == nid), None)
                if nxt is None:
                    nxt = {"id": nid, "label": _label(tax, nid),
                           "children": [], "classifications": []}
                    trail[-1]["children"].append(nxt)
                entry = nxt
            trail.append(entry)
        leaf = trail[-1]
        leaf["classifications"].append(
            {k: c.get(k) for k in ("confidence", "exact_value", "qualifier",
                                   "attribution_chain", "element_id",
                                   "review_needed") if c.get(k) is not None})
    forest = [roots[nid] for nid in order]
    _sort_forest(forest)
    return forest


def _sort_forest(entries: list[dict]) -> None:
    entries.sort(key=lambda e: (e.get("collapsed", False), e.get("label", "")))
    for e in entries:
        if not e.get("collapsed"):
            _sort_forest(e["children"])


def _matches(tax, query_id: str, element_id: str) -> bool:
    if element_id == query_id:
        return True
    try:
        return query_id in tax.ancestors(element_id)
    except (KeyError, AttributeError):
        return False


def _subtree_counts(entry: dict) -> tuple[int, int, int]:
    """(part_nodes, other_nodes, classifications) under entry inclusive."""
    parts = 1 if entry["id"].startswith("part.") else 0
    others = 0 if entry["id"].startswith("part.") else 1
    total = len(entry.get("classifications", []) or [])
    for ch in entry.get("children", []) or []:
        if ch.get("collapsed"):
            parts += ch.get("parts", 0)
            others += ch.get("nodes", 0)
            total += ch.get("classifications", 0)
        else:
            p, o, t = _subtree_counts(ch)
            parts, others, total = parts + p, others + o, total + t
    return parts, others, total


def _mark_subtree(entry: dict, tax, query_id: str) -> dict:
    """Full copy of a matched node's subtree: everything below a match is
    displayed context (the brief shows material/finish/thickness leaves under
    a matched part), each level still flagged when it matches too."""
    return {"id": entry["id"], "label": entry["label"],
            "matched": _matches(tax, query_id, entry["id"]),
            "children": [_mark_subtree(ch, tax, query_id)
                         for ch in entry.get("children", []) or []
                         if not ch.get("collapsed")],
            "classifications": list(entry.get("classifications", []) or [])}


def _prune(entry: dict, tax, query_id: str) -> dict | None:
    """Root-to-match chains survive; off-chain subtrees collapse to counts;
    matched nodes display their full subtree as context."""
    self_hit = _matches(tax, query_id, entry["id"])
    if self_hit:
        return _mark_subtree(entry, tax, query_id)
    pruned_children = []
    dropped_parts, dropped_nodes, dropped_cl = 0, 0, 0
    for ch in entry.get("children", []) or []:
        sub = _prune(ch, tax, query_id)
        if sub is None:
            # off-chain subtree collapses: part nodes count as sibling parts,
            # everything else as generic nodes, classifications in full.
            p, o, t = _subtree_counts(ch)
            if ch["id"].startswith("part."):
                dropped_parts += p
                dropped_nodes += o
            else:
                dropped_nodes += p + o
            dropped_cl += t
        else:
            pruned_children.append(sub)
    if not pruned_children:
        return None
    out = {"id": entry["id"], "label": entry["label"], "matched": self_hit,
           "children": pruned_children,
           "classifications": list(entry.get("classifications", []) or [])}
    if dropped_parts or dropped_nodes:
        out["children"] = pruned_children + [
            {"collapsed": True, "parts": dropped_parts, "nodes": dropped_nodes,
             "classifications": dropped_cl}]
    return out


def _matched_nodes(tax, query_id: str, classifications: list) -> list[str]:
    """Every element of every path that carries the match — the full
    highlighted chain from the matched node down to its leaves."""
    out = set()
    for c in classifications:
        path = c.get("path", []) or []
        if any(_matches(tax, query_id, el) for el in path):
            out.update(path)
    return sorted(out)


def _best_confidence(classifications: list, query_id: str, tax) -> str:
    ranks = [CONF_RANK.get(c.get("confidence"), 3) for c in classifications
             if any(_matches(tax, query_id, el) for el in (c.get("path") or []))]
    inv = {v: k for k, v in CONF_RANK.items()}
    return inv.get(min(ranks)) if ranks else "low"


def resolve_query(tax, query: str) -> tuple[str | None, list]:
    """Free text -> best node. Exact label/synonym matches win (parts before
    properties: "glazing" is the unit, not the material); otherwise the top
    fuzzy hit; no hits -> (None, []) and only unclassified results."""
    hits = tax.search(query or "", limit=5)
    if not hits:
        return None, []
    from .taxonomy import normalize
    nq = normalize(query)
    exact = [h for h in hits
             if any(normalize(n) == nq
                    for n in [h[0].get("label") or ""]
                    + list(h[0].get("synonyms") or []))]
    if exact:
        level_rank = {"part": 0, "component": 1, "value": 2, "attribute": 3}
        exact.sort(key=lambda h: (level_rank.get(h[0]["level"], 9),
                                  h[0]["label"]))
        return tax.resolve(exact[0][0]["id"]), hits
    node, score = hits[0]
    if score >= 1.0 or len(hits) == 1:
        return tax.resolve(node["id"]), hits
    return tax.resolve(hits[0][0]["id"]), hits


def search_tree(db: Path, tax, query: str | None = None,
                node_id: str | None = None) -> dict:
    """Brief 8.8 response shape: pruned trees eager, full trees lazy."""
    if node_id is None and query:
        node_id, _hits = resolve_query(tax, query)
    if node_id is not None:
        node_id = tax.resolve(node_id)
        if node_id not in tax.nodes:
            node_id = None
    details = current_details(db)
    results = []
    seen_comps: dict[str, set] = {}
    if node_id is not None:
        for did, info in details.items():
            rec = info["record"]
            cl = rec.get("classifications", []) or []
            if not any(_matches(tax, node_id, el)
                       for c in cl for el in (c.get("path") or [])):
                continue
            forest = build_forest(rec, tax)
            pruned = [p for p in (_prune(r, tax, node_id) for r in forest)
                      if p is not None]
            matched = _matched_nodes(tax, node_id, cl)
            conf = _best_confidence(cl, node_id, tax)
            sib_parts = sum(ch.get("parts", 0) for r in pruned
                            for ch in r.get("children", [])
                            if ch.get("collapsed"))
            results.append({
                "detail_id": did, "title": info["title"],
                "source_sheet": info["drawing"],
                "thumbnail": rec.get("thumbnail"),
                "confidence": conf, "commit_date": info["commit_date"],
                "matched_nodes": matched, "pruned_tree": pruned,
                "full_tree_available": True,
                "collapsed_counts": {"sibling_parts": sib_parts,
                                     "total_classifications": len(cl)}})
            for c in cl:
                comps = set()
                for el in c.get("path", []) or []:
                    if el.startswith("component."):
                        comps.add(el)
                    else:
                        try:
                            comps.update(a for a in tax.ancestors(el)
                                         if a.startswith("component."))
                        except (KeyError, AttributeError):
                            pass
                for comp in comps:
                    seen_comps.setdefault(comp, set()).add(info["drawing"])
    # rank per 7.8: confidence, then provenance status, then reusable above
    # reference-only, then recency; within equal rank prefer well-annotated
    # drawings (more high-confidence classifications). Two stable passes:
    # recency first (ISO dates sort lexicographically), then the rest.
    def status_of(did: str) -> int:
        status = (details[did]["record"].get("provenance") or {}).get("status")
        return STATUS_RANK.get(status or "unknown", 3)

    def highs_of(did: str) -> int:
        return sum(1 for c in details[did]["record"].get("classifications", []) or []
                   if c.get("confidence") == "high")

    results.sort(key=lambda r: r.get("commit_date") or "", reverse=True)
    results.sort(key=lambda r: (CONF_RANK.get(r["confidence"], 3),
                                status_of(r["detail_id"]),
                                0 if details[r["detail_id"]]["record"].get("reusable") else 1,
                                -highs_of(r["detail_id"])))
    facet_counts = {k: len(v) for k, v in sorted(seen_comps.items())}
    unclassified = _unclassified_hits(db, tax, query or "")
    return {"query": {"text": query, "node": node_id},
            "results": results,
            "facet_counts": facet_counts,
            "unclassified": unclassified}


def _unclassified_hits(db: Path, tax, query: str) -> list[dict]:
    """Pending quarantine occurrences matching the query text — the separate
    section below tree results (never interleaved)."""
    from .resolve import effective_occurrences
    if not query:
        return []
    nq = query.casefold().strip()
    if not nq:
        return []
    occs = [o for o in effective_occurrences(db, tax)
            if nq in (o.get("raw_string") or "").casefold()
            or nq in (o.get("normalised") or "")]
    titles = {d: current_details(db).get(d, {}).get("title") for d in
              {o.get("detail_id") for o in occs}}
    grouped: dict[tuple, dict] = {}
    for o in occs:
        key = (o.get("drawing"), o.get("detail_id"))
        g = grouped.setdefault(key, {"detail_id": o.get("detail_id"),
                                     "drawing": o.get("drawing"),
                                     "title": titles.get(o.get("detail_id")),
                                     "matches": [], "cluster_ids": []})
        if o.get("raw_string") not in g["matches"]:
            g["matches"].append(o.get("raw_string"))
    # cluster ids for the "assign" jump: recompute cheaply per group
    from .resolve import group_occurrences
    for c in group_occurrences(occs, tax):
        for d in {o.get("detail_id") for o in c["occurrences"]}:
            for g in grouped.values():
                if g["detail_id"] == d and c["cluster_id"] not in g["cluster_ids"]:
                    g["cluster_ids"].append(c["cluster_id"])
    return sorted(grouped.values(),
                  key=lambda g: (g.get("drawing") or "", g.get("detail_id") or ""))


def expand_detail(db: Path, tax, detail_id: str) -> dict:
    """Full tree for one result, lazily (8.8: pruned eager, full on expand)."""
    details = current_details(db)
    info = details.get(detail_id)
    if info is None:
        raise KeyError(f"unknown detail: {detail_id}")
    rec = info["record"]
    return {"detail_id": detail_id, "title": info["title"],
            "source_sheet": info["drawing"],
            "full_tree": build_forest(rec, tax),
            "classifications": rec.get("classifications", []) or []}
