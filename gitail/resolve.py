"""8.7 quarantine, notification and inline resolution.

Uploads NEVER block on unresolved elements: unclassifiable values attach to
``unclassified.<level>`` as occurrence rows, drawings stay full-text
searchable immediately, and a non-modal notification invites the uploader —
who knows the drawing best — to resolve. Twelve drawings saying ``EZY CAV
JAMB LINER`` form ONE cluster: one decision, twelve drawings resolved.

Storage: the ``quarantine`` table holds one row per candidate occurrence per
commit (stable uid). Clusters form at READ time by deterministic greedy
grouping, so approving a node or adding a synonym reclassifies matching
occurrences with no manual re-tagging and no reindex (8.6.4/8.7.5).

Every resolution is a reviewable event: ``resolutions`` rows plus a
best-effort Git commit over the touched taxonomy/details files, with
``undo`` reversing both the vocabulary edit and the occurrence statuses.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

QUARANTINE_SCHEMA = """
CREATE TABLE IF NOT EXISTS quarantine (
  uid            TEXT PRIMARY KEY,
  raw_string     TEXT,
  normalised     TEXT,
  level_guess    TEXT,
  attribute      TEXT,
  material       TEXT,
  part           TEXT,
  value          REAL,
  qualifier      TEXT,
  measure        TEXT DEFAULT 'thickness',
  element_id     TEXT,
  detail_id      TEXT,
  drawing        TEXT,
  project        TEXT,
  commit_sha     TEXT,
  commit_date    TEXT,
  evidence       TEXT,
  crop           TEXT,
  reason         TEXT,
  status         TEXT DEFAULT 'pending',
  resolved_by    TEXT,
  updated_at     TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_quar_drawing ON quarantine(drawing);
CREATE INDEX IF NOT EXISTS idx_quar_commit ON quarantine(commit_sha);
CREATE INDEX IF NOT EXISTS idx_quar_status ON quarantine(status);
CREATE INDEX IF NOT EXISTS idx_quar_norm ON quarantine(normalised);
CREATE TABLE IF NOT EXISTS resolutions (
  id INTEGER PRIMARY KEY,
  rid            TEXT UNIQUE,
  created_at     TEXT,
  actor          TEXT,
  action         TEXT,
  cluster_key    TEXT,
  target         TEXT,
  detail_ids     TEXT,
  synonyms_added TEXT,
  occurrences    TEXT,
  taxonomy_commit TEXT,
  note           TEXT,
  undone         INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS dismissals (
  cluster_key TEXT,
  actor       TEXT,
  dismissed_at TEXT DEFAULT (datetime('now')),
  PRIMARY KEY (cluster_key, actor)
);
"""

CLUSTER_SIMILARITY = 0.85  # 8.7 clustering threshold (settings-overridable)
STOPWORDS = frozenset("""
    the and for with from fixed through into onto per via all any are was
    notes note typical nom tbc min max typ ref refer detail details dwg
""".split())


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().casefold())


def occurrence_uid(candidate: dict, commit_sha: str) -> str:
    """Deterministic occurrence id: same candidate + commit always collides."""
    base = "|".join([candidate.get("normalised") or _norm(candidate.get("raw_string")),
                     candidate.get("level_guess") or "",
                     candidate.get("element_id") or "",
                     commit_sha or ""])
    return "u_" + hashlib.sha1(base.encode("utf-8")).hexdigest()[:6]


def _con(db: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    return con


def insert_occurrences(con: sqlite3.Connection, payload: dict, drawing: str,
                       project: str, sha: str, cdate: str | None) -> int:
    """Store payload quarantine_candidates as occurrence rows. Existing rows
    (same uid) keep their human-decided status — reindexing never resurrects
    resolved work or drops it."""
    n = 0
    for cand in payload.get("quarantine_candidates", []) or []:
        uid = occurrence_uid(cand, sha)
        row = con.execute("SELECT status FROM quarantine WHERE uid=?", (uid,)).fetchone()
        if row is not None:
            continue
        con.execute(
            "INSERT INTO quarantine (uid,raw_string,normalised,level_guess,"
            " attribute,material,part,value,qualifier,measure,element_id,detail_id,"
            " drawing,project,commit_sha,commit_date,evidence,crop,reason,status)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (uid, cand.get("raw_string"), cand.get("normalised"),
             cand.get("level_guess"), cand.get("attribute"),
             cand.get("material"), cand.get("part"), cand.get("value"),
             cand.get("qualifier"), cand.get("measure") or "thickness",
             cand.get("element_id"),
             cand.get("detail_id"), drawing, project, sha, cdate,
             json.dumps(cand.get("evidence") or {}, ensure_ascii=False),
             cand.get("crop"), cand.get("reason"), "pending"))
        n += 1
    return n


def load_occurrences(db: Path, drawing: str | None = None,
                     latest_only: bool = True,
                     statuses: tuple = ("pending",)) -> list[dict]:
    """Occurrence rows as dicts (evidence parsed). Default scope: pending
    rows at each drawing's latest commit — the honest review queue."""
    con = _con(db)
    try:
        names = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "quarantine" not in names:
            return []
        q = ("SELECT * FROM quarantine WHERE status IN "
             f"({','.join('?' for _ in statuses)})")
        args: list = list(statuses)
        if drawing:
            q += " AND drawing=?"
            args.append(drawing)
        if latest_only:
            q += (" AND commit_sha IN (SELECT commit_sha FROM quarantine AS _l"
                  " WHERE _l.drawing=quarantine.drawing"
                  " ORDER BY _l.commit_date DESC, _l.rowid DESC LIMIT 1)")
        q += " ORDER BY drawing, detail_id, element_id, commit_sha"
        rows = [dict(r) for r in con.execute(q, args)]
    finally:
        con.close()
    for r in rows:
        try:
            r["evidence"] = json.loads(r.get("evidence") or "{}")
        except ValueError:
            r["evidence"] = {}
    return rows


def effective_occurrences(db: Path, tax, drawing: str | None = None) -> list[dict]:
    """Pending occurrences still unresolvable under the CURRENT taxonomy —
    the live quarantine set (approvals and synonyms auto-clear, 8.6.4)."""
    return [o for o in load_occurrences(db, drawing) if still_pending(o, tax)]


# --------------------------------------------------------------------------
# pending evaluation against CURRENT taxonomy (no reindex needed)

def still_pending(occ: dict, tax) -> bool:
    """A pending occurrence clears automatically once the vocabulary covers
    it (synonym added or node approved) — 8.6.4 with no manual re-tagging."""
    from .classify import (is_component_word, is_material_word, resolve_material,
                           resolve_part)
    if occ.get("status") != "pending":
        return False
    guess = occ.get("level_guess")
    if guess == "value":
        return resolve_material(tax, occ.get("material") or "") is None
    if guess == "part":
        part = occ.get("part")
        if not part:
            return True  # unparsed text: only a human assignment clears it
        if resolve_part(tax, part) is not None:
            return False
        if is_material_word(tax, part) or is_component_word(tax, part):
            return False
        return True
    return True


# --------------------------------------------------------------------------
# clustering

def _vocab_words(tax) -> set[str]:
    words = set()
    for node in tax.nodes.values():
        for name in [node.get("label") or ""] + list(node.get("synonyms") or []):
            words.update(re.findall(r"[a-z0-9]+", _norm(name)))
    return words


def _content_words(norm: str, vocab: set[str]) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", norm)
            if len(w) >= 3 and w not in STOPWORDS and w not in vocab]


def _bigrams(words: list[str]) -> set[str]:
    return {" ".join(p) for p in zip(words, words[1:])}


def group_occurrences(occurrences: list[dict], tax,
                      threshold: float = CLUSTER_SIMILARITY) -> list[dict]:
    """Greedy deterministic clusters: same level_guess and (normalised
    similarity >= threshold OR a shared content-word bigram, so ``EZY CAV
    JAMB LINER`` and its long-winded variant land together)."""
    from .taxonomy import similarity
    vocab = _vocab_words(tax)
    groups: list[dict] = []  # {key, rep, rep_words, occurrences}
    for occ in sorted(occurrences,
                      key=lambda o: (o.get("drawing") or "", o.get("detail_id") or "",
                                     o.get("element_id") or "", o.get("commit_sha") or "")):
        norm = occ.get("normalised") or _norm(occ.get("raw_string"))
        words = _content_words(norm, vocab)
        placed = False
        for g in groups:
            if g["level_guess"] != (occ.get("level_guess") or "part"):
                continue
            if similarity(norm, g["rep"]) >= threshold or \
                    (_bigrams(words) & _bigrams(g["rep_words"])):
                g["occurrences"].append(occ)
                placed = True
                break
        if not placed:
            groups.append({"key": f"{occ.get('level_guess') or 'part'}|{norm}",
                           "rep": norm, "rep_words": words,
                           "level_guess": occ.get("level_guess") or "part",
                           "occurrences": [occ]})
    # stable cluster ids from the key; disambiguate truncations deterministically
    seen: dict[str, int] = {}
    out = []
    for g in groups:
        base = "c_" + hashlib.sha1(g["key"].encode("utf-8")).hexdigest()[:3]
        n = seen.get(base, 0)
        seen[base] = n + 1
        out.append({**g, "cluster_id": base if n == 0 else f"{base}-{n + 1}"})
    return out


def _detail_titles(db: Path) -> dict[str, str]:
    con = _con(db)
    try:
        names = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "detail" not in names:
            return {}
        rows = con.execute(
            "SELECT detail_id, title, commit_sha FROM detail").fetchall()
    finally:
        con.close()
    latest: dict[str, tuple] = {}
    for r in rows:
        key = r["detail_id"]
        if key not in latest:
            latest[key] = (r["commit_sha"], r["title"])
    return {k: v[1] for k, v in latest.items()}


def suggestions(tax, raw_string: str, sibling_context: list[str] | None,
                limit: int = 3) -> list[dict]:
    """Ranked existing-node suggestions. sibling_context (taxonomy nodes
    already on the detail) boosts door parts above roof parts for a door
    detail — the ranking signal that makes the dialog one click (8.7.4)."""
    sibs = set(sibling_context or [])
    hits = tax.search(raw_string or "", limit=8)
    scored = []
    for node, base in hits:
        score, why = float(base), ""
        names = [node.get("label") or ""] + list(node.get("synonyms") or [])
        nn = _norm(raw_string)
        best_name = next((n for n in names if _norm(n) == nn), "")
        if not best_name:
            best_name = next((n for n in names
                              if nn and nn in _norm(n)), names[0] if names else "")
        if best_name and best_name != node.get("label"):
            why = f"synonym {best_name!r}"
        nid = node["id"]
        if nid in sibs:
            score = min(1.0, score + 0.25)
            why = (why + " + " if why else "") + "same detail context"
        elif tax.ancestors(nid) & sibs:
            score = min(1.0, score + 0.15)
            ctx = sorted(tax.ancestors(nid) & sibs)
            why = (why + " + " if why else "") + \
                f"sibling context {'/'.join(x.split('.')[-1] for x in ctx[:2])}"
        scored.append({"node": nid, "label": node.get("label"),
                       "score": round(score, 2),
                       "why": why or "string similarity"})
    scored.sort(key=lambda s: (-s["score"], s["node"]))
    return scored[:limit]


def clusters(db: Path, tax, drawing: str | None = None,
             actor: str | None = None) -> list[dict]:
    """The review queue: pending, still-unresolvable occurrences grouped into
    clusters with suggestions. Dismissed clusters stay hidden from the actor
    who dismissed them (anti-nag); the badge counts clusters, not
    occurrences."""
    occs = [o for o in load_occurrences(db, drawing)
            if still_pending(o, tax)]
    groups = group_occurrences(occs, tax)
    dismissed: set[str] = set()
    if actor:
        con = _con(db)
        try:
            dismissed = {r[0] for r in con.execute(
                "SELECT cluster_key FROM dismissals WHERE actor=?", (actor,))}
        finally:
            con.close()
    titles = _detail_titles(db)
    out = []
    for g in groups:
        if actor and g["key"] in dismissed:
            continue
        occs_g = g["occurrences"]
        raws = sorted({o.get("raw_string") or "" for o in occs_g if o.get("raw_string")})
        label = max(raws, key=len) if raws else g["rep"]
        details = sorted({(o.get("drawing"), o.get("detail_id")) for o in occs_g})
        sibs = sorted({s for o in occs_g
                       for s in ((o.get("evidence") or {}).get("sibling_context") or [])})
        crop = next((o.get("crop") for o in occs_g if o.get("crop")), None)
        out.append({"cluster_key": g["key"], "cluster_id": g["cluster_id"],
                    "label": label, "variants": raws,
                    "level_guess": g["level_guess"],
                    "attribute": next((o.get("attribute") for o in occs_g
                                       if o.get("attribute")), None),
                    "occurrence_count": len(occs_g),
                    "details": [{"drawing": d, "detail_id": t,
                                 "title": titles.get(t)} for d, t in details],
                    "occurrences": [o["uid"] for o in occs_g],
                    "crop": crop, "sibling_context": sibs,
                    "suggestions": suggestions(tax, label, sibs)})
    out.sort(key=lambda c: (-c["occurrence_count"], c["label"]))
    return out


# --------------------------------------------------------------------------
# notification + digest (8.7.3)

def upload_notification(db: Path, tax, drawing: str, actor: str = "uploader",
                        cap: int = 5, abandon_threshold: int = 50) -> dict:
    """Post-upload prompt payload (8.7.3A): capped at `cap` clusters, plus a
    banner flag when the queue is abandoned. Never blocks the upload.
    Chain D pendings ride along under "dimensions" (addendum B.3)."""
    mine = [c for c in clusters(db, tax, drawing, actor)]
    all_open = clusters(db, tax)
    total = len(all_open)
    dims = [d for d in list_dim_pending(db, drawing)][:cap]
    return {"drawing": drawing,
            "details_indexed": len({d["detail_id"] for c in mine for d in c["details"]}),
            "clusters": mine[:cap],
            "more": max(0, len(mine) - cap),
            "pending_total": total,
            "banner": total > abandon_threshold,
            "dimensions": dims}


def digest(db: Path, tax, days: int = 7,
           min_occurrences: int = 10) -> dict:
    """Weekly taxonomy-owner summary (8.7.3C): new clusters, total pending,
    oldest pending, high-impact above threshold."""
    from datetime import timedelta
    open_all = clusters(db, tax)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    con = _con(db)
    try:
        first_seen = {}
        for r in con.execute("SELECT uid, MIN(commit_date) FROM quarantine "
                             "WHERE status='pending' GROUP BY uid"):
            first_seen[r[0]] = r[1] or ""
    finally:
        con.close()
    new = [c for c in open_all
           if any((first_seen.get(u) or "") >= cutoff for u in c["occurrences"])]
    oldest = min((first_seen.get(u, "") for c in open_all for u in c["occurrences"]),
                 default="")
    return {"new_clusters": [{"cluster_id": c["cluster_id"], "label": c["label"],
                              "occurrence_count": c["occurrence_count"]}
                             for c in sorted(new, key=lambda c: -c["occurrence_count"])],
            "pending_total": len(open_all),
            "pending_occurrences": sum(c["occurrence_count"] for c in open_all),
            "oldest_pending": oldest,
            "high_impact": [{"cluster_id": c["cluster_id"], "label": c["label"],
                             "occurrence_count": c["occurrence_count"]}
                            for c in open_all
                            if c["occurrence_count"] >= min_occurrences]}


def maybe_auto_promote(db: Path, taxonomy_dir: str | Path,
                       settings: dict | None = None,
                       actor: str = "auto") -> list[dict]:
    """8.7.6 optional auto-promotion. OFF by default and narrow by design:
    value-level candidates under FREE_TEXT attributes only, at 10+
    occurrences across 3+ distinct sheets — proposed (never silently
    admitted) as created_by auto with review_needed, still requiring human
    approval. Components and parts are NEVER auto-created: a wrong component
    node restructures how the whole firm navigates the library.

    In practice nothing qualifies yet (attribution emits no free-text values),
    which is the safe default — this enforces the gate for when it does.
    """
    from .taxonomy import load_taxonomy_cached, resolve_dir
    settings = settings or {}
    if not settings.get("auto_promote", False):
        return []
    min_occ = int(settings.get("auto_promote_min_occurrences", 10))
    min_sheets = int(settings.get("auto_promote_min_sheets", 3))
    tax = load_taxonomy_cached(taxonomy_dir)
    directory = resolve_dir(taxonomy_dir)
    if directory is None:
        return []
    free_text_attrs = {nid for nid, n in tax.nodes.items()
                       if n.get("level") == "attribute"
                       and n.get("value_type") == "free_text"}
    out = []
    for cluster in clusters(db, tax):
        if cluster["level_guess"] != "value":
            continue
        if (cluster.get("attribute") or "") not in free_text_attrs:
            continue
        if cluster["occurrence_count"] < min_occ:
            continue
        sheets = {d["drawing"] for d in cluster["details"]}
        if len(sheets) < min_sheets:
            continue
        from .taxonomy import append_node, slugify
        slug = slugify(cluster["label"])
        nid = f"value.{slug}"
        if nid in tax.nodes:
            continue
        node = {"id": nid, "level": "value", "label": cluster["label"].strip(),
                "slug": slug, "parents": [cluster["attribute"]],
                "synonyms": sorted(set(cluster.get("variants", []))),
                "definition": f"Auto-promoted from {cluster['occurrence_count']} "
                              f"occurrences — define properly at review.",
                "status": "approved", "created_by": "auto",
                "created_at": date.today().isoformat(), "drawing_count": 0,
                "deprecated_by": None, "review_needed": True}
        try:
            from .taxonomy import _validate
            trial = dict(tax.nodes)
            trial[nid] = node
            _validate(trial)
            node_file = append_node(directory, node)
            tax.nodes[nid] = node
        except Exception:
            continue
        _git_commit([node_file],
                    f"auto-promote: {cluster['label']!r} -> {nid} "
                    f"({cluster['occurrence_count']} occurrences, review_needed)")
        con = _con(db)
        try:
            _log(con, actor, "new", cluster["cluster_key"], nid,
                 [d["detail_id"] for d in cluster["details"]],
                 cluster.get("variants", []), cluster["occurrences"], {},
                 note="auto-promoted (review_needed)")
        finally:
            con.close()
        out.append({"id": nid, "review_needed": True, "created_by": "auto",
                    "occurrences": cluster["occurrence_count"]})
    return out


# --------------------------------------------------------------------------
# actions (8.7.4)

# --------------------------------------------------------------------------
# Chain D review (addendum B.3): one-click confirm promotes a low vision
# attribution to medium/human; it stays out of default search until then.


def list_dim_pending(db: Path, drawing: str | None = None) -> list[dict]:
    """Pending Chain D attributions (with crops) for the review panel."""
    con = _con(db)
    try:
        names = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "dim_review" not in names:
            return []
        q = ("SELECT uid,drawing,detail_id,element_id,value,unit,label,anchor,"
             " regions,crop,material,region_id,chain,confidence,status,"
             " last_seen_commit FROM dim_review WHERE status='pending'")
        args: list = []
        if drawing:
            q += " AND drawing=?"
            args.append(drawing)
        q += " ORDER BY drawing, detail_id, element_id"
        rows = [dict(r) for r in con.execute(q, args)]
    finally:
        con.close()
    for r in rows:
        for key in ("anchor", "regions"):
            try:
                r[key] = json.loads(r.get(key) or ("[]" if key == "regions" else "null"))
            except ValueError:
                r[key] = [] if key == "regions" else None
    return rows


def confirm_dim(db: Path, taxonomy_dir: str | Path, uid: str,
                actor: str = "cli",
                repo: str | Path | None = None) -> dict:
    """Confirm a Chain D attribution: status confirmed, committed detail file
    gains the classification at medium/human, and the next index promotes the
    attribution row out of the low-confidence gate (test 105)."""
    from .classify import compose_path
    from .taxonomy import TaxonomyError, load_taxonomy_cached, resolve_dir
    tax = load_taxonomy_cached(taxonomy_dir)
    directory = resolve_dir(taxonomy_dir)
    if directory is None:
        raise TaxonomyError("resolutions need an owned taxonomy/ — "
                            "run `gitail taxonomy-init` first")
    con = _con(db)
    try:
        row = con.execute("SELECT * FROM dim_review WHERE uid=?", (uid,)).fetchone()
        row = dict(row) if row else None
    finally:
        con.close()
    if row is None:
        raise TaxonomyError(f"unknown dimension review: {uid}")
    if row.get("status") == "confirmed":
        return {"uid": uid, "already": True}
    if not row.get("material"):
        raise TaxonomyError(f"{uid} names no material — nothing to confirm")
    detail_ids = [row.get("detail_id")] if row.get("detail_id") else []
    comps = _facet_comps_by_detail(db, detail_ids).get(row.get("detail_id"), [])
    try:
        val = float(row["value"]) if row.get("value") is not None else None
    except (TypeError, ValueError):
        val = None
    entries = []
    for path in compose_path(tax, comps, None, row.get("material"), val):
        entry: dict = {"path": path, "confidence": "medium",
                       "attribution_chain": "vision+human",
                       "element_id": row.get("element_id")}
        if val is not None:
            entry["exact_value"] = val
        entries.append(entry)
    entries_by_detail = {row["detail_id"]: entries} if row.get("detail_id") else {}
    touched = _append_detail_classifications(
        Path(repo) if repo else None, entries_by_detail)
    commits = _git_commit(
        touched, f"confirm: Chain D dimension {row.get('label') or row.get('value')} "
                 f"-> {row.get('material')} (medium, human)"
                 f"\n\nConfirmed by: {actor}")
    con = _con(db)
    try:
        con.execute("UPDATE dim_review SET status='confirmed',"
                    " updated_at=datetime('now') WHERE uid=?", (uid,))
        con.commit()
        rid = _log(con, actor, "confirm-dim", uid, row.get("material"),
                   detail_ids, [], [uid], commits,
                   note=f"Chain D {uid} confirmed at medium/human")
    finally:
        con.close()
    return {"uid": uid, "rid": rid, "material": row.get("material"),
            "value": row.get("value"), "files": [str(p) for p in touched],
            "commits": commits}


def _append_detail_classifications(repo: Path | None,
                                   entries_by_detail: dict) -> list[Path]:
    """Append classification entries to committed details/*.json (shared by
    assign reclassification and Chain D confirmation)."""
    touched: list[Path] = []
    if repo is None or not entries_by_detail:
        return touched
    for path in sorted(Path(repo, "details").rglob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        changed = False
        for d in data.get("details", []) or []:
            add = entries_by_detail.get(d.get("detail_id"))
            if not add:
                continue
            have = {(tuple(c.get("path") or []), c.get("element_id"))
                    for c in d.get("classifications", []) or []}
            for e in add:
                if (tuple(e["path"]), e.get("element_id")) not in have:
                    d.setdefault("classifications", []).append(e)
                    have.add((tuple(e["path"]), e.get("element_id")))
                    changed = True
        if changed:
            for d in data.get("details", []) or []:
                d["classifications"] = sorted(
                    d.get("classifications", []) or [],
                    key=lambda c: (c.get("path") or [], c.get("element_id") or ""))
            path.write_text(json.dumps(data, sort_keys=True, indent=2,
                                       ensure_ascii=False) + "\n", encoding="utf-8")
            touched.append(path)
    return touched


def _git_commit(paths: list[Path], message: str) -> dict:
    """Commit touched files grouped by containing repo. Best-effort: outside
    a repo (or without git) the resolution still applies, commit is None."""
    import subprocess
    groups: dict[str, list[str]] = {}
    for p in paths:
        try:
            ap = Path(p).resolve()
        except OSError:
            continue
        repo = None
        for parent in [ap] + list(ap.parents):
            if (parent / ".git").is_dir():
                repo = parent
                break
        if repo is None:
            continue
        try:
            rel = ap.relative_to(repo).as_posix()
        except ValueError:
            continue
        groups.setdefault(str(repo), []).append(rel)
    commits = {}
    for repo, rels in groups.items():
        try:
            subprocess.run(["git", "-C", repo, "add", "--", *sorted(set(rels))],
                           check=True, capture_output=True)
            r = subprocess.run(
                ["git", "-C", repo, "-c", "user.name=gitail", "-c",
                 "user.email=gitail@local", "commit", "-m", message],
                capture_output=True, text=True)
            if r.returncode == 0:
                q = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"],
                                   capture_output=True, text=True)
                commits[repo] = q.stdout.strip()[:12]
            else:
                commits[repo] = None
        except Exception:
            commits[repo] = None
    return commits


def _next_rid(con: sqlite3.Connection) -> str:
    n = con.execute("SELECT COUNT(*) FROM resolutions").fetchone()[0]
    return f"r_{n + 1:04d}"


def _log(con: sqlite3.Connection, actor: str, action: str, cluster_key: str,
         target: str | None = None, detail_ids: list | None = None,
         synonyms: list | None = None, uids: list | None = None,
         commits: dict | None = None, note: str | None = None) -> str:
    rid = _next_rid(con)
    con.execute(
        "INSERT INTO resolutions (rid,created_at,actor,action,cluster_key,"
        " target,detail_ids,synonyms_added,occurrences,taxonomy_commit,note)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (rid, _utcnow(), actor, action, cluster_key, target,
         json.dumps(detail_ids or []), json.dumps(synonyms or []),
         json.dumps(uids or []), json.dumps(commits or {}), note))
    con.commit()
    return rid


def _find_cluster(db: Path, tax, key: str,
                  drawing: str | None = None) -> dict | None:
    for c in clusters(db, tax, drawing):
        if key in (c["cluster_key"], c["cluster_id"]):
            return c
    return None


def _scoped_uids(db: Path, cluster: dict,
                 only_detail: str | None) -> list[str]:
    if not only_detail:
        return list(cluster["occurrences"])
    con = _con(db)
    try:
        rows = con.execute(
            f"SELECT uid FROM quarantine WHERE uid IN "
            f"({','.join('?' for _ in cluster['occurrences'])}) AND detail_id=?",
            (*cluster["occurrences"], only_detail)).fetchall()
    finally:
        con.close()
    return [r[0] for r in rows]


def _patch_detail_classifications(db: Path, repo: Path | None,
                                    uids: list[str],
                                    entries_by_detail: dict) -> list[Path]:
    """Fold resolved occurrences into committed details/*.json files so the
    tree finds them before the next extract: append the reclassifications AND
    drop the now-classified candidates (otherwise the resolution commit would
    re-derive a fresh pending row for the same string — resolutions must stick
    across commits). Purely file-local; the index re-derives on next build."""
    touched: list[Path] = []
    if repo is None or not uids:
        return touched
    con = _con(db)
    try:
        rows = con.execute(
            f"SELECT detail_id, element_id, normalised FROM quarantine WHERE uid IN "
            f"({','.join('?' for _ in uids)})", uids).fetchall()
    finally:
        con.close()
    resolved_pairs = {(r["detail_id"], r["element_id"], r["normalised"]) for r in rows}
    if not resolved_pairs:
        return touched
    for path in sorted(Path(repo, "details").rglob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        changed = False
        for d in data.get("details", []) or []:
            add = entries_by_detail.get(d.get("detail_id"))
            if add:
                have = {(tuple(c.get("path") or []), c.get("element_id"))
                        for c in d.get("classifications", []) or []}
                for e in add:
                    if (tuple(e["path"]), e.get("element_id")) not in have:
                        d.setdefault("classifications", []).append(e)
                        have.add((tuple(e["path"]), e.get("element_id")))
                        changed = True
            kept_cands = [
                cand for cand in (data.get("quarantine_candidates", []) or [])
                if (cand.get("detail_id"), cand.get("element_id"),
                    cand.get("normalised")) not in resolved_pairs]
            if len(kept_cands) != len(data.get("quarantine_candidates", []) or []):
                data["quarantine_candidates"] = kept_cands
                changed = True
        if changed:
            for d in data.get("details", []) or []:
                d["classifications"] = sorted(
                    d.get("classifications", []) or [],
                    key=lambda c: (c.get("path") or [], c.get("element_id") or ""))
            path.write_text(json.dumps(data, sort_keys=True, indent=2,
                                       ensure_ascii=False) + "\n", encoding="utf-8")
            touched.append(path)
    return touched


def assign(db: Path, taxonomy_dir: str | Path, cluster_key: str, target: str,
           actor: str = "cli", only_detail: str | None = None,
           unsure: bool = False, repo: str | Path | None = None) -> dict:
    """8.7.4 action A (+C with unsure=True): assign a cluster to an existing
    branch. Raw strings join the node's synonyms (same annotation resolves
    forever after); occurrences leave quarantine in one action; committed
    detail files gain the classifications (flagged low + review_needed when
    unsure). Scope toggle: all details, or one via only_detail."""
    from .classify import compose_path, resolve_material, resolve_part
    from .taxonomy import (TaxonomyError, append_node, load_taxonomy_cached,
                           resolve_dir)
    tax = load_taxonomy_cached(taxonomy_dir)
    directory = resolve_dir(taxonomy_dir)
    if directory is None:
        raise TaxonomyError("resolutions need an owned taxonomy/ — "
                            "run `gitail taxonomy-init` first")
    cluster = _find_cluster(db, tax, cluster_key)
    if cluster is None:
        raise TaxonomyError(f"unknown or already-resolved cluster: {cluster_key}")
    current = tax.resolve(target)
    if current not in tax.nodes:
        raise TaxonomyError(f"unknown node: {target}")
    node = tax.nodes[current]
    uids = _scoped_uids(db, cluster, only_detail)
    if not uids:
        raise TaxonomyError("scope matched no pending occurrences")
    con = _con(db)
    try:
        rows = [dict(r) for r in con.execute(
            f"SELECT * FROM quarantine WHERE uid IN "
            f"({','.join('?' for _ in uids)})", uids)]
    finally:
        con.close()
    # synonyms: every distinct raw string in scope.
    have = {_norm(n) for n in [node.get("label") or ""]
            + list(node.get("synonyms") or [])}
    added = []
    for raw in sorted({(r.get("raw_string") or "").strip() for r in rows
                       if (r.get("raw_string") or "").strip()}):
        if _norm(raw) not in have:
            added.append(raw)
            have.add(_norm(raw))
    node["synonyms"] = sorted(list(node.get("synonyms") or []) + added)
    node_file = append_node(directory, node)
    # reclassify: compose tree paths for each occurrence in scope.
    entries_by_detail: dict[str, list] = {}
    detail_ids = sorted({r.get("detail_id") for r in rows if r.get("detail_id")})
    comps_by_detail = _facet_comps_by_detail(db, detail_ids)
    for r in rows:
        comps = comps_by_detail.get(r.get("detail_id"), [])
        if not comps:
            # fall back to component-level sibling context resolved on the detail
            try:
                ev = json.loads(r.get("evidence") or "{}")
            except ValueError:
                ev = {}
            comps = sorted({s for s in (ev.get("sibling_context") or [])
                            if str(s).startswith("component.")})
        if node["level"] == "part":
            part_id, mat = current, r.get("material")
            if mat and resolve_material(tax, mat) is None:
                mat = None
        elif node["level"] == "value":
            part_id = resolve_part(tax, r.get("part") or "") or None
            mat = node.get("slug") or current.split(".", 1)[1]
        else:
            part_id, mat = None, r.get("material")
        val = float(r["value"]) if r.get("value") is not None else None
        try:
            ev_chain = (json.loads(r.get("evidence") or "{}")
                        .get("attribution_chain"))
        except ValueError:
            ev_chain = None
        for path in compose_path(tax, comps, part_id, mat, val,
                                   r.get("measure") or "thickness"):
            entry: dict = {"path": path,
                           "confidence": "low" if unsure else "medium",
                           "attribution_chain": ev_chain or "resolution",
                           "element_id": r.get("element_id")}
            if val is not None:
                entry["exact_value"] = val
            if r.get("qualifier") is not None:
                entry["qualifier"] = r.get("qualifier")
            if unsure:
                entry["review_needed"] = True
            entries_by_detail.setdefault(r.get("detail_id"), []).append(entry)
    touched = _patch_detail_classifications(
        db, Path(repo) if repo else None, uids, entries_by_detail)
    commits = _git_commit(
        [node_file, *touched],
        f"resolve: {cluster['label']!r} -> {current} "
        f"({len(uids)} occurrence{'s' if len(uids) != 1 else ''})"
        f"\n\nAssigned by: {actor}"
        + (f"\nFlagged unsure — review_needed" if unsure else "")
        + (f"\nScope: {only_detail} only" if only_detail else ""))
    con = _con(db)
    try:
        con.execute(
            f"UPDATE quarantine SET status='resolved', resolved_by='pending',"
            f" updated_at='{_utcnow()}' WHERE uid IN "
            f"({','.join('?' for _ in uids)})", uids)
        con.commit()
        rid = _log(con, actor, "unsure" if unsure else "assign",
                   cluster["cluster_key"], current, detail_ids, added, uids,
                   commits)
        con.execute("UPDATE quarantine SET resolved_by=? WHERE resolved_by='pending'",
                    (rid,))
        con.commit()
    finally:
        con.close()
    return {"rid": rid, "action": "unsure" if unsure else "assign",
            "into": current, "reclassified": len(uids),
            "details": detail_ids, "synonyms_added": added,
            "files": [str(p) for p in touched], "commits": commits}


def _facet_comps_by_detail(db: Path, detail_ids: list[str]) -> dict:
    """Component-level facet nodes per detail, from the CURRENT detail rows
    (latest commit first wins — facets are human-confirmed and evolve)."""
    if not detail_ids:
        return {}
    con = _con(db)
    try:
        names = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "detail" not in names:
            return {}
        rows = con.execute(
            "SELECT detail_id, record, commit_date FROM detail ORDER BY "
            "commit_date DESC, rowid DESC").fetchall()
    finally:
        con.close()
    out: dict = {}
    for r in rows:
        did = r["detail_id"]
        if did not in detail_ids or did in out:
            continue
        try:
            rec = json.loads(r["record"] or "{}")
        except ValueError:
            continue
        out[did] = [n for n in (rec.get("facet_nodes") or [])
                    if str(n).startswith("component.")]
    return out


def ignore_cluster(db: Path, taxonomy_dir: str | Path, cluster_key: str,
                   actor: str = "cli",
                   repo: str | Path | None = None) -> dict:
    """8.7.4 action D: not an element. Normalised strings join ignore.yaml
    (the badge can reach zero); scoped candidates leave the committed detail
    files so the decision sticks across commits; future occurrences skip
    quarantine silently but stay full-text searchable."""
    import yaml
    from .taxonomy import TaxonomyError, load_taxonomy_cached, resolve_dir
    tax = load_taxonomy_cached(taxonomy_dir)
    directory = resolve_dir(taxonomy_dir)
    if directory is None:
        raise TaxonomyError("resolutions need an owned taxonomy/ — "
                            "run `gitail taxonomy-init` first")
    cluster = _find_cluster(db, tax, cluster_key)
    if cluster is None:
        raise TaxonomyError(f"unknown or already-resolved cluster: {cluster_key}")
    path = directory / "ignore.yaml"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError:
        data = {}
    entries = list(data.get("ignored", []) or [])
    have = {_norm(e) for e in entries}
    added = []
    for raw in sorted({(r.get("raw_string") or "").strip()
                       for r in _cluster_rows(db, cluster) if r.get("raw_string")}):
        norm = _norm(raw)
        if norm and norm not in have:
            entries.append(norm)
            have.add(norm)
            added.append(norm)
    entries.sort()
    data["ignored"] = entries
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
                    encoding="utf-8")
    con = _con(db)
    try:
        uids = cluster["occurrences"]
        con.execute(
            f"UPDATE quarantine SET status='ignored', resolved_by='pending',"
            f" updated_at='{_utcnow()}' WHERE uid IN "
            f"({','.join('?' for _ in uids)})", uids)
        con.commit()
        rid = _log(con, actor, "ignore", cluster["cluster_key"], None,
                   [d["detail_id"] for d in cluster["details"]], added, uids,
                   None)
    finally:
        con.close()
    touched = _patch_detail_classifications(
        db, Path(repo) if repo else None, uids, {})
    files = [path, *touched]
    commits = _git_commit(
        files, f"resolve: ignore {cluster['label']!r} (not an element)"
               f"\n\nIgnored by: {actor}")
    con = _con(db)
    try:
        con.execute("UPDATE resolutions SET taxonomy_commit=? WHERE rid=?",
                    (json.dumps(commits), rid))
        con.commit()
        con.execute("UPDATE quarantine SET resolved_by=? WHERE resolved_by='pending'",
                    (rid,))
        con.commit()
    finally:
        con.close()
    return {"rid": rid, "action": "ignore", "ignored": added, "commits": commits}


def _cluster_rows(db: Path, cluster: dict) -> list[dict]:
    con = _con(db)
    try:
        rows = [dict(r) for r in con.execute(
            f"SELECT * FROM quarantine WHERE uid IN "
            f"({','.join('?' for _ in cluster['occurrences'])})",
            cluster["occurrences"])]
    finally:
        con.close()
    for r in rows:
        try:
            r["evidence"] = json.loads(r.get("evidence") or "{}")
        except ValueError:
            r["evidence"] = {}
    return rows


def new_from_cluster(db: Path, taxonomy_dir: str | Path, cluster_key: str,
                     actor: str = "cli",
                     parents: list[str] | None = None) -> dict:
    """8.7.4 action B: open the registration form PRE-FILLED from the cluster
    — label from the cleanest variant, level from level_guess, parents from
    sibling context, synonyms from all variants, example from the first
    occurrence. Definition stays blank and REQUIRED (approve() enforces)."""
    from .register import propose
    from .taxonomy import TaxonomyError, load_taxonomy_cached, resolve_dir
    tax = load_taxonomy_cached(taxonomy_dir)
    directory = resolve_dir(taxonomy_dir)
    if directory is None:
        raise TaxonomyError("resolutions need an owned taxonomy/ — "
                            "run `gitail taxonomy-init` first")
    cluster = _find_cluster(db, tax, cluster_key)
    if cluster is None:
        raise TaxonomyError(f"unknown or already-resolved cluster: {cluster_key}")
    level = "value" if cluster["level_guess"] == "value" else "part"
    if parents is None:
        if level == "value" and cluster.get("attribute"):
            parents = [cluster["attribute"]]
        else:
            sibs = cluster.get("sibling_context", []) or []
            parents = [s for s in sibs
                       if s.startswith("component.") or s.startswith("part.")]
    if not parents:
        raise TaxonomyError("no parent inferable — pass explicit parents")
    label = max(cluster.get("variants", [cluster["label"]]),
                key=lambda s: (len(s), s))
    first = sorted(cluster["details"],
                   key=lambda d: (d.get("drawing") or "", d.get("detail_id") or ""))[0]
    res = propose(directory, level, label.strip(), parents, "",
                  first.get("detail_id") or "",
                  synonyms=[v for v in cluster.get("variants", []) if v != label],
                  submitted_by=f"resolution:{actor}",
                  allow_blank_definition=True)
    con = _con(db)
    try:
        rid = _log(con, actor, "new", cluster["cluster_key"], res["id"],
                   [d["detail_id"] for d in cluster["details"]], [],
                   cluster["occurrences"], {},
                   note=f"proposal {res['slug']} (definition blank — edit, then approve)")
    finally:
        con.close()
    return {**res, "rid": rid, "prefill": {
        "label": label, "level": level, "parents": parents,
        "synonyms": res.get("synonyms", []),
        "example_detail_id": first.get("detail_id")}}


def dismiss_cluster(db: Path, cluster_key: str, actor: str) -> dict:
    """Remind-me-later dismissal: this actor is never re-prompted about this
    cluster (anti-nag). The cluster stays pending for everyone else."""
    from .taxonomy import load_taxonomy_cached
    tax = load_taxonomy_cached(None)
    cluster = _find_cluster(db, tax, cluster_key)
    if cluster is None:
        # may already be resolved — still record the dismissal harmlessly
        key = cluster_key
    else:
        key = cluster["cluster_key"]
    con = _con(db)
    try:
        con.execute("INSERT OR REPLACE INTO dismissals (cluster_key, actor) VALUES (?,?)",
                    (key, actor))
        con.commit()
    finally:
        con.close()
    return {"dismissed": key, "actor": actor}


def resolutions_recent(db: Path, limit: int = 20) -> list[dict]:
    con = _con(db)
    try:
        names = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "resolutions" not in names:
            return []
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM resolutions ORDER BY id DESC LIMIT ?", (limit,))]
    finally:
        con.close()
    return rows


def undo_resolution(db: Path, taxonomy_dir: str | Path, rid: str,
                    actor: str = "cli",
                    repo: str | Path | None = None) -> dict:
    """Reverse a resolution: synonym/ignore-list edits reverted, occurrences
    back to pending. A wrong assignment is a one-command fix (8.7.5)."""
    import yaml
    from .taxonomy import (TaxonomyError, append_node, load_taxonomy_cached,
                           resolve_dir)
    try:
        row = next(r for r in resolutions_recent(db, 10000) if r["rid"] == rid)
    except StopIteration:
        raise TaxonomyError(f"unknown resolution: {rid}")
    if row.get("undone"):
        raise TaxonomyError(f"{rid} is already undone")
    tax = load_taxonomy_cached(taxonomy_dir)
    directory = resolve_dir(taxonomy_dir)
    if directory is None:
        raise TaxonomyError("resolutions need an owned taxonomy/ — "
                            "run `gitail taxonomy-init` first")
    action, target = row["action"], row.get("target")
    added = json.loads(row.get("synonyms_added") or "[]")
    uids = json.loads(row.get("occurrences") or "[]")
    touched: list[Path] = []
    if action in ("assign", "unsure") and target and target in tax.nodes:
        node = tax.nodes[target]
        drop = {_norm(s) for s in added}
        node["synonyms"] = sorted(s for s in (node.get("synonyms") or [])
                                  if _norm(s) not in drop)
        touched.append(append_node(directory, node))
        # patched detail classifications come out too.
        touched += _unpatch_detail_classifications(
            db, Path(repo) if repo else None, uids)
    elif action == "ignore":
        path = directory / "ignore.yaml"
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except OSError:
            data = {}
        drop = {_norm(s) for s in added}
        data["ignored"] = sorted(e for e in (data.get("ignored") or [])
                                 if _norm(e) not in drop)
        path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
                        encoding="utf-8")
        touched.append(path)
    elif action == "new":
        # the pre-filled proposal is still pending review — withdraw it.
        slug = (target or "").split(".", 1)[1] if target and "." in target else ""
        prop = directory / "proposals" / f"{slug}.yaml" if slug else None
        if prop is not None and prop.exists():
            try:
                import yaml
                if _read_pending(prop):
                    prop.unlink()
                    touched.append(prop)
            except OSError:
                pass
    commits = _git_commit(touched, f"revert resolution {rid} ({action})"
                                   f"\n\nReverted by: {actor}")
    con = _con(db)
    try:
        if uids:
            con.execute(
                f"UPDATE quarantine SET status='pending', resolved_by=NULL,"
                f" updated_at='{_utcnow()}' WHERE uid IN "
                f"({','.join('?' for _ in uids)})", uids)
        con.execute("UPDATE resolutions SET undone=1 WHERE rid=?", (rid,))
        con.commit()
        undo_rid = _log(con, actor, "undo", row.get("cluster_key") or "",
                        target, None, [], uids, commits,
                        note=f"reverts {rid}")
    finally:
        con.close()
    return {"rid": undo_rid, "reverts": rid, "occurrences_reopened": len(uids),
            "commits": commits}


def _read_pending(path: Path) -> bool:
    import yaml
    try:
        return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get(
            "status", "pending") == "pending"
    except (OSError, ValueError):
        return False


def _unpatch_detail_classifications(db: Path, repo: Path | None,
                                    uids: list[str]) -> list[Path]:
    """Reverse _patch_detail_classifications: strip resolution-chain entries
    for the undone occurrences' (detail, element) pairs AND restore their
    quarantine candidates, so the reopened rows show in the queue again."""
    if repo is None or not uids:
        return []
    con = _con(db)
    try:
        rows = con.execute(
            f"SELECT detail_id, element_id, raw_string, normalised, level_guess,"
            f" attribute, material, part, value, qualifier, evidence, crop, reason"
            f" FROM quarantine WHERE uid IN "
            f"({','.join('?' for _ in uids)})", uids).fetchall()
    finally:
        con.close()
    targets: dict[str, set] = {}
    cands_by_detail: dict[str, list] = {}
    for r in rows:
        if not r["detail_id"]:
            continue
        targets.setdefault(r["detail_id"], set()).add(r["element_id"])
        try:
            ev = json.loads(r["evidence"] or "{}")
        except ValueError:
            ev = {}
        cands_by_detail.setdefault(r["detail_id"], []).append({
            "raw_string": r["raw_string"], "normalised": r["normalised"],
            "level_guess": r["level_guess"], "attribute": r["attribute"],
            "material": r["material"], "part": r["part"], "value": r["value"],
            "qualifier": r["qualifier"], "element_id": r["element_id"],
            "detail_id": r["detail_id"], "evidence": ev, "crop": r["crop"],
            "reason": r["reason"]})
    if not targets:
        return []
    touched: list[Path] = []
    for path in sorted(Path(repo, "details").rglob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        changed = False
        for d in data.get("details", []) or []:
            els = targets.get(d.get("detail_id"))
            if els:
                kept = [c for c in (d.get("classifications") or [])
                        if not (c.get("attribution_chain") == "resolution"
                                and c.get("element_id") in els)]
                if len(kept) != len(d.get("classifications", []) or []):
                    d["classifications"] = kept
                    changed = True
            adds = cands_by_detail.get(d.get("detail_id"), [])
            if adds:
                have = {(c.get("element_id"), c.get("normalised"))
                        for c in (data.get("quarantine_candidates", []) or [])}
                for cand in adds:
                    if (cand.get("element_id"), cand.get("normalised")) not in have:
                        data.setdefault("quarantine_candidates", []).append(cand)
                        have.add((cand.get("element_id"), cand.get("normalised")))
                        changed = True
        if changed:
            data["quarantine_candidates"] = sorted(
                data.get("quarantine_candidates", []) or [],
                key=lambda c: (c.get("detail_id") or "", c.get("normalised") or "",
                               c.get("element_id") or ""))
            path.write_text(json.dumps(data, sort_keys=True, indent=2,
                                       ensure_ascii=False) + "\n", encoding="utf-8")
            touched.append(path)
    return touched
