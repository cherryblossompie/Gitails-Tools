"""8.1 taxonomy graph + 8.6 duplicate check (Part 8 foundation).

The vocabulary is a DIRECTED ACYCLIC GRAPH, not a tree: a part may have
several parents (glazing_unit lives under window, curtain_wall, door and
balustrade). The tree the user sees is a VIEW rendered over the graph — any
node may appear in several paths, and results deduplicate by node id.

Drawings are never tree nodes: they attach to VALUE nodes by reference via
classifications[] (7.5). Node ids are permanent; renames change label only
(old label moves into synonyms); merges set deprecated_by + aliases.yaml and
rewrite references — a referenced node is never deleted.

Layout (brief C1), owned per drawing-repo so vocabulary changes are pull
requests; the seed ships with the tool as the starting copy:
  taxonomy/nodes/{components,parts,attributes}.yaml
  taxonomy/nodes/values/{materials,finishes,fixing_methods}.yaml
  taxonomy/proposals/<slug>.yaml   taxonomy/ignore.yaml   taxonomy/aliases.yaml
"""
from __future__ import annotations

import difflib
import re
import shutil
from pathlib import Path

LEVELS = ("component", "part", "attribute", "value")
ID_PREFIX = {"component": "component", "part": "part",
             "attribute": "attr", "value": "value"}
VALUE_TYPES = ("enum", "numeric", "structured", "free_text")

NODE_FILES = ("nodes/components.yaml", "nodes/parts.yaml",
              "nodes/attributes.yaml")
VALUES_DIR = "nodes/values"


class TaxonomyError(ValueError):
    """Loader/validation failure with the offending path named."""


def normalize(text: str) -> str:
    """Case-folded, punctuation-light, whitespace-collapsed form used for all
    matching: 'Coping', 'coping' and 'COPING ' are one string."""
    s = str(text or "").casefold()
    s = re.sub(r"[._\-/]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def similarity(a: str, b: str) -> float:
    """Normalised string similarity in [0,1]; 1.0 is an exact match."""
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


def seed_dir() -> Path:
    from importlib import resources
    return Path(str(resources.files("gitail") / "taxonomy_seed"))


def resolve_dir(given: str | Path | None) -> Path | None:
    """Explicit --taxonomy-dir wins; else ./taxonomy when it owns nodes/;
    else None meaning the bundled seed (read-only)."""
    if given:
        return Path(given)
    local = Path("taxonomy")
    if (local / "nodes").is_dir():
        return local
    return None


def _read_yaml(path: Path):
    import yaml
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _node_files(taxonomy_dir: Path) -> list[Path]:
    files = [taxonomy_dir / f for f in NODE_FILES]
    values = taxonomy_dir / VALUES_DIR
    if values.is_dir():
        files += sorted(values.glob("*.yaml"))
    return [f for f in files if f.is_file()]


class Taxonomy:
    """Loaded, validated graph. nodes: id -> record; children: id -> [ids]."""

    def __init__(self, nodes: dict, aliases: dict, ignore: list,
                 source: str, directory: Path | None,
                 ignore_patterns: list | None = None):
        self.nodes = nodes
        self.aliases = aliases
        self.ignore = ignore
        self.ignore_patterns = list(ignore_patterns or [])
        self.source = source
        self.directory = directory
        self.children: dict[str, list[str]] = {}
        for nid, node in nodes.items():
            for p in node.get("parents", []) or []:
                self.children.setdefault(p, []).append(nid)
        for cid in self.children:
            self.children[cid] = sorted(self.children[cid])

    # -- navigation ----------------------------------------------------
    def resolve(self, node_id: str) -> str:
        """Follow aliases.yaml + deprecated_by to the current id (loop-safe)."""
        seen = []
        cur = node_id
        while True:
            if cur in seen:
                return cur
            seen.append(cur)
            nxt = self.aliases.get(cur)
            if nxt is None and cur in self.nodes:
                nxt = self.nodes[cur].get("deprecated_by")
            if not nxt or nxt == cur:
                return cur
            cur = nxt

    def is_deprecated(self, node_id: str) -> bool:
        return self.resolve(node_id) != node_id or \
            bool((self.nodes.get(node_id) or {}).get("deprecated_by"))

    def parents_of(self, node_id: str) -> list[str]:
        return list((self.nodes.get(node_id) or {}).get("parents", []) or [])

    def children_of(self, node_id: str) -> list[str]:
        return list(self.children.get(node_id, []))

    def ancestors(self, node_id: str) -> set[str]:
        out, stack = set(), [node_id]
        while stack:
            cur = stack.pop()
            for p in self.parents_of(cur):
                if p not in out:
                    out.add(p)
                    stack.append(p)
        return out

    def descendants(self, node_id: str) -> set[str]:
        out, stack = set(), [node_id]
        while stack:
            cur = stack.pop()
            for c in self.children_of(cur):
                if c not in out:
                    out.add(c)
                    stack.append(c)
        return out

    def paths(self, node_id: str) -> list[list[str]]:
        """Every root-to-node path (multi-parent nodes yield several)."""
        memo: dict[str, list[list[str]]] = {}

        def _paths(nid: str) -> list[list[str]]:
            if nid in memo:
                return memo[nid]
            parents = self.parents_of(nid)
            if not parents:
                memo[nid] = [[nid]]
            else:
                memo[nid] = [p + [nid] for p in parents for p in _paths(p)]
            return memo[nid]

        return _paths(node_id)

    # -- rendering ------------------------------------------------------
    def render(self, root: str | None = None) -> list[dict]:
        """Tree view over the graph. A node with several parents appears in
        every path (dedup by id happens on RESULT lists in 8.8, not here)."""
        if root is not None:
            if root not in self.nodes:
                raise TaxonomyError(f"unknown node: {root}")
            return [self._render_node(root)]
        roots = sorted(
            (n["id"] for n in self.nodes.values()
             if not n.get("parents") and n.get("level") == "component"
             and not n.get("deprecated_by")),
            key=lambda i: self.nodes[i]["label"])
        return [self._render_node(r) for r in roots]

    def _render_node(self, node_id: str) -> dict:
        node = self.nodes[node_id]
        return {"id": node_id, "label": node.get("label"),
                "synonyms": list(node.get("synonyms") or []),
                "deprecated_by": node.get("deprecated_by"),
                "children": [self._render_node(c)
                             for c in self.children_of(node_id)
                             if not self.nodes[c].get("deprecated_by")]}

    # -- matching --------------------------------------------------------
    def _names(self, node: dict) -> list[str]:
        return [node.get("label") or ""] + list(node.get("synonyms") or [])

    def search(self, query: str, include_deprecated: bool = False,
               limit: int = 25) -> list[tuple[dict, float]]:
        """Synonyms and definition text both match: 'cill' finds 'sill',
        'coping' finds 'capping'."""
        nq = normalize(query)
        if not nq:
            return []
        scored = []
        for node in self.nodes.values():
            if node.get("deprecated_by") and not include_deprecated:
                continue
            best, why = 0.0, ""
            for name in self._names(node):
                nn = normalize(name)
                if not nn:
                    continue
                if nn == nq:
                    s = 1.0
                elif nq in nn or nn in nq:
                    s = 0.85
                else:
                    toks_q, toks_n = set(nq.split()), set(nn.split())
                    overlap = len(toks_q & toks_n) / max(len(toks_q), 1)
                    s = max(overlap * 0.9, similarity(query, name) * 0.7)
                if s > best:
                    best, why = s, name
            if best < 0.5:
                defn = normalize(node.get("definition") or "")
                if nq and nq in defn:
                    best = max(best, 0.5)
            if best >= 0.5:
                scored.append((node, round(best, 3)))
        scored.sort(key=lambda t: (-t[1], t[0]["label"]))
        return scored[:limit]

    def check_duplicate(self, label: str, synonyms: list[str] | None = None,
                        threshold: float = 0.75) -> list[tuple[dict, float, str]]:
        """8.6 gate: proposed label+synonyms vs every node's label+synonyms.
        Returns [(node, score, matched_name)] above threshold, best first.
        Submission is BLOCKED when this is non-empty."""
        proposed = [label] + list(synonyms or [])
        hits = []
        for node in self.nodes.values():
            if node.get("deprecated_by"):
                continue
            best, name = 0.0, ""
            for p in proposed:
                for n in self._names(node):
                    s = similarity(p, n)
                    if s > best:
                        best, name = s, n
            if best > threshold:
                hits.append((node, round(best, 3), name))
        hits.sort(key=lambda t: (-t[1], t[0]["id"]))
        return hits


def _validate(nodes: dict) -> None:
    for nid, node in nodes.items():
        level = node.get("level")
        if level not in LEVELS:
            raise TaxonomyError(f"{nid}: bad level {level!r}")
        if not node.get("label"):
            raise TaxonomyError(f"{nid}: label is required")
        if nid.split(".", 1)[0] != ID_PREFIX[level]:
            raise TaxonomyError(f"{nid}: id prefix must be {ID_PREFIX[level]!r} "
                                f"for level {level!r}")
        if level == "value":
            # values may omit definitions in seed files; default idempotently
            # so re-validating an already-loaded graph never fails.
            node.setdefault("definition", "")
        elif not node.get("definition"):
            raise TaxonomyError(f"{nid}: definition is required "
                                f"(rejecting undefined nodes prevents taxonomy rot)")
        parents = node.get("parents", []) or []
        if level == "component" and parents:
            raise TaxonomyError(f"{nid}: components take no parents")
        if level in ("part", "value") and not parents:
            raise TaxonomyError(f"{nid}: {level}s need at least one parent")
        if level == "attribute" and parents:
            raise TaxonomyError(f"{nid}: attributes are a shared global level, no parents")
        for p in parents:
            if p not in nodes:
                raise TaxonomyError(f"{nid}: unknown parent {p}")
            pl = nodes[p]["level"]
            if level == "part" and pl not in ("component", "part"):
                raise TaxonomyError(f"{nid}: part parent {p} must be a component or part")
            if level == "value" and pl != "attribute":
                raise TaxonomyError(f"{nid}: value parent {p} must be an attribute")
    # enum membership: a value under an enum attribute must be admitted there.
    for nid, node in nodes.items():
        if node.get("level") != "value":
            continue
        for p in node.get("parents", []) or []:
            attr = nodes[p]
            if attr.get("value_type") == "enum":
                members = [normalize(m) for m in (attr.get("enum_members") or [])]
                if normalize(node.get("slug") or nid.split(".", 1)[1]) not in members:
                    raise TaxonomyError(
                        f"{nid}: not admitted under enum {p} — new enum members "
                        f"arrive by proposal only (8.5)")
    # cycle check over parent edges, naming the path (8.9 #8).
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {nid: WHITE for nid in nodes}

    def visit(nid: str, stack: list[str]):
        color[nid] = GRAY
        for p in nodes[nid].get("parents", []) or []:
            if color[p] == GRAY:
                # p is on the current path by construction — slice it out.
                cycle = stack[stack.index(p):] + [p]
                raise TaxonomyError("taxonomy cycle rejected: " + " -> ".join(cycle))
            if color[p] == WHITE:
                visit(p, stack + [p])
        color[nid] = BLACK

    for nid in nodes:
        if color[nid] == WHITE:
            visit(nid, [nid])


def load_taxonomy(taxonomy_dir: str | Path | None = None) -> Taxonomy:
    """Load + validate. Falls back to the bundled seed (read-only source)."""
    resolved = resolve_dir(taxonomy_dir)
    base = resolved if resolved is not None else seed_dir()
    nodes: dict = {}
    for path in _node_files(base):
        data = _read_yaml(path)
        for node in data.get("nodes", []) or []:
            nid = node.get("id")
            if not nid:
                raise TaxonomyError(f"{path}: node without id")
            if nid in nodes:
                raise TaxonomyError(f"{nid}: duplicated node (one node, many parents — "
                                    f"never duplicate a part for a second parent)")
            node.setdefault("parents", [])
            node.setdefault("synonyms", [])
            node.setdefault("status", "approved")
            node.setdefault("deprecated_by", None)
            nodes[nid] = node
    aliases = dict((_read_yaml(base / "aliases.yaml").get("aliases", {}) or {}))
    ignore_data = _read_yaml(base / "ignore.yaml")
    ignore = list(ignore_data.get("ignored", []) or [])
    ignore_patterns = list(ignore_data.get("patterns", []) or [])
    _validate(nodes)
    return Taxonomy(nodes, aliases, ignore,
                    source=str(base), directory=resolved,
                    ignore_patterns=ignore_patterns)


def load_settings(config_dir: str | Path | None = None) -> dict:
    """Thresholds from config/taxonomy.yaml with bundled fallback (mirrors
    semantics.load_config_dir so serve/CLI behave identically anywhere)."""
    defaults = {"similarity_block_threshold": 0.75,
                "cluster_similarity_threshold": 0.85,
                "upload_prompt_cap": 5,
                "queue_abandon_threshold": 50,
                "digest_min_occurrences": 10,
                "auto_promote": False,
                "auto_promote_min_occurrences": 10,
                "auto_promote_min_sheets": 3}
    custom = Path(config_dir) / "taxonomy.yaml" if config_dir else None
    if custom is not None and custom.exists():
        data = _read_yaml(custom)
        return {**defaults, **{k: data[k] for k in defaults if k in data}}
    try:
        data = _read_yaml(Path(str(seed_dir().parent / "config" / "taxonomy.yaml")))
        return {**defaults, **{k: data[k] for k in defaults if k in data}}
    except OSError:
        return defaults


def slugify(label: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", str(label or "").casefold()).strip("_")
    return slug or "node"


_taxonomy_cache: dict = {}


def load_taxonomy_cached(taxonomy_dir: str | Path | None = None):
    """load_taxonomy with mtime-keyed caching. Indexing walks every commit per
    drawing — re-parsing 211 nodes of YAML each time would dominate runtime.
    Any on-disk vocabulary change busts the cache via max mtime."""
    resolved = resolve_dir(taxonomy_dir)
    base = resolved if resolved is not None else seed_dir()
    try:
        stamp = max([p.stat().st_mtime_ns for p in _node_files(base)
                     if p.exists()] +
                    [(base / "aliases.yaml").stat().st_mtime_ns
                     if (base / "aliases.yaml").exists() else 0,
                     (base / "ignore.yaml").stat().st_mtime_ns
                     if (base / "ignore.yaml").exists() else 0])
    except OSError:
        stamp = -1
    key = str(base)
    hit = _taxonomy_cache.get(key)
    if hit is not None and hit[0] == stamp:
        return hit[1]
    tax = load_taxonomy(taxonomy_dir)
    _taxonomy_cache[key] = (stamp, tax)
    return tax


def init_taxonomy(target: str | Path) -> Path:
    """Copy the bundled seed into a repo-owned taxonomy/ so vocabulary changes
    become version-controlled pull requests. Refuses to overwrite."""
    target = Path(target)
    if (target / "nodes").is_dir():
        raise TaxonomyError(f"{target} already owns nodes/ — refusing to overwrite")
    shutil.copytree(seed_dir(), target,
                    ignore=shutil.ignore_patterns("__pycache__"))
    return target


# -- persistence ----------------------------------------------------------
def _node_file(directory: Path, node: dict) -> Path:
    if node["level"] == "component":
        return directory / "nodes" / "components.yaml"
    if node["level"] == "part":
        return directory / "nodes" / "parts.yaml"
    if node["level"] == "attribute":
        return directory / "nodes" / "attributes.yaml"
    parent = (node.get("parents") or ["attr"])[0]
    return directory / "nodes" / "values" / f"{parent.split('.', 1)[1]}.yaml"


def append_node(directory: Path, node: dict) -> Path:
    """Append one approved node to its file (sorted by id for determinism)."""
    import yaml
    path = _node_file(directory, node)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _read_yaml(path) if path.exists() else {}
    current = [n for n in (data.get("nodes", []) or []) if n.get("id") != node["id"]]
    current.append(node)
    current.sort(key=lambda n: n["id"])
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        yaml.safe_dump({"nodes": current}, f, sort_keys=False, allow_unicode=True)
    return path


def rewrite_parents(directory: Path, old_id: str, new_id: str) -> list[Path]:
    """Point every parents[] entry at old_id to new_id (merge support)."""
    import yaml
    touched = []
    for path in _node_files(directory):
        data = _read_yaml(path)
        changed = False
        for node in data.get("nodes", []) or []:
            parents = node.get("parents", []) or []
            if old_id in parents:
                node["parents"] = sorted({new_id if p == old_id else p for p in parents})
                changed = True
        if changed:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
            touched.append(path)
    return touched


def write_aliases(directory: Path, aliases: dict) -> Path:
    import yaml
    path = directory / "aliases.yaml"
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        yaml.safe_dump({"aliases": dict(sorted(aliases.items()))}, f,
                       sort_keys=False, allow_unicode=True)
    return path


def merge_node(directory: Path, loser_id: str, winner_id: str,
               actor: str = "cli") -> dict:
    """Deprecate loser into winner: deprecated_by + alias + parents rewritten.
    Never deletes the loser — drawings may reference it (8.9 #10)."""
    import yaml
    tax = load_taxonomy(directory)
    if loser_id not in tax.nodes:
        raise TaxonomyError(f"unknown node: {loser_id}")
    if winner_id not in tax.nodes:
        raise TaxonomyError(f"unknown node: {winner_id}")
    if tax.nodes[loser_id].get("level") != tax.nodes[winner_id].get("level"):
        raise TaxonomyError("merge needs matching levels: "
                            f"{loser_id} vs {winner_id}")
    loser_file = _node_file(directory, tax.nodes[loser_id])
    data = _read_yaml(loser_file)
    for node in data.get("nodes", []) or []:
        if node.get("id") == loser_id:
            node["deprecated_by"] = winner_id
    with open(loser_file, "w", encoding="utf-8", newline="\n") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
    touched = rewrite_parents(directory, loser_id, winner_id)
    aliases = dict(tax.aliases)
    aliases[loser_id] = winner_id
    write_aliases(directory, aliases)
    return {"deprecated": loser_id, "into": winner_id, "by": actor,
            "parents_rewritten_in": [str(p) for p in touched]}
