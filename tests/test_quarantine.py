"""8.7 quarantine + resolution flow (acceptance 8.9 #11-26, 7.9 #7-adjacent).

An upload with unrecognised vocabulary completes, stays full-text searchable,
queues ONE cluster per novel string family, and resolves through assign /
unsure / ignore / new / dismiss — every decision reviewable and revertible.
"""
import json
import subprocess
from pathlib import Path

import ezdxf
from click.testing import CliRunner

from gitail.cli import cli
from gitail.index import build_index
from gitail.query import find
from gitail.semantics import load_materials
from gitail.taxonomy import init_taxonomy, load_taxonomy

CFG = Path(__file__).parent.parent / "config" / "materials.yaml"
EZY = "EZY CAV FLANGE"
EZY_LONG = "EXTENDABLE EZY CAV, ALUMINIUM CSD FLANGE FIXED THROUGH CLAMP"


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "drawings").mkdir(parents=True)
    for d in ("state", "details", "pdf", "thumbs", "crops"):
        (repo / d).mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "t@t.t")
    git(repo, "config", "user.name", "t")
    (repo / "x.txt").write_text("x")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "init")
    return repo


def write_dxf(path: Path, texts: list[str]):
    doc = ezdxf.new("R2018")
    msp = doc.modelspace()
    for i, t in enumerate(texts):
        msp.add_mtext(t, dxfattribs={"layer": "A-DETL-ANNO"}).dxf.insert = (0, i * 30)
    # a witness line so evidence crops always carry ink (SHX text may not
    # rasterise in headless environments — geometry always does)
    msp.add_line((0, -10), (60, -10), dxfattribs={"layer": "A-DETL-ANNO"})
    doc.saveas(str(path))


def owned(repo: Path) -> Path:
    target = repo / "taxonomy"
    if not (target / "nodes").is_dir():
        init_taxonomy(target)
    return target


def extract_commit_index(repo: Path, names: list[str], db: Path,
                         taxdir: Path | None = None):
    """CLI extract (with visuals) + commit + index for each drawing name."""
    runner = CliRunner()
    for name in names:
        r = runner.invoke(cli, ["extract", str(repo / "drawings" / f"{name}.dxf"),
                                "--state-dir", str(repo / "state"),
                                "--details-dir", str(repo / "details"),
                                "--drawings-dir", str(repo / "drawings"),
                                "--thumbs-dir", str(repo / "thumbs"),
                                "--crops-dir", str(repo / "crops"),
                                "--config-dir", str(CFG.parent)] +
                               (["--taxonomy-dir", str(taxdir)] if taxdir else []))
        assert r.exit_code == 0, r.output
    git(repo, "add", ".")
    git(repo, "commit", "-m", "ingest")
    build_index(repo, db, taxonomy_dir=str(taxdir) if taxdir else None)
    return repo


def fresh_tax(repo: Path):
    from gitail.taxonomy import _taxonomy_cache
    _taxonomy_cache.clear()
    return load_taxonomy(owned(repo))


# --------------------------------------------------------------------------
# 11/13/22: upload completes, searchable, ONE cluster, badge counts clusters

def test_11_13_22_upload_clusters_badge(tmp_path):
    from gitail.resolve import clusters, upload_notification
    repo = make_repo(tmp_path)
    taxdir = owned(repo)
    variants = [EZY, EZY_LONG, "EZY-CAV FLANGE", "EZY CAV FLANGES",
                "EZY CAV FLANGE, FIXED", "EZY CAV FLANGE TYP.",
                "EZY CAV FLANGE (TYP)", "EZY CAV-FLANGE", "EZY CAV  FLANGE",
                "EZY CAV FLANGE.", "EZY CAV FLANGE A", "EZY CAV FLANGE B"]
    for i, v in enumerate(variants):
        write_dxf(repo / "drawings" / f"Q-{i:02d}.dxf", [v, "3mm GLASS"])
    db = tmp_path / "i.sqlite"
    extract_commit_index(repo, [f"Q-{i:02d}" for i in range(12)], db, taxdir)
    tax = fresh_tax(repo)
    # full-text searchable immediately, upload never blocked
    assert len(find(db, text="EZY", ever=True)) == 12
    assert len(find(db, material="glass", ever=True)) == 12
    # twelve variants -> ONE cluster ...
    groups = clusters(db, tax)
    assert len(groups) == 1, [g["label"] for g in groups]
    assert groups[0]["occurrence_count"] == 12
    assert groups[0]["level_guess"] == "part"
    assert groups[0]["crop"] and groups[0]["crop"].endswith(".png")
    # ... so the badge counts 1 decision, not 12 drawings
    note = upload_notification(db, tax, "Q-00", "web")
    assert note["pending_total"] == 1 and not note["banner"]
    assert len(note["clusters"]) == 1


def test_12_notification_capped_at_five(tmp_path):
    from gitail.resolve import upload_notification
    repo = make_repo(tmp_path)
    taxdir = owned(repo)
    words = ["QUX ALPHA FLANGE", "ZYX BRAVO GIRDER", "WULF CHARLIE COUPLER",
             "VEX DELTA DOWEL", "JUK ECHO GUSSET", "PAX FOXTROT FERRULE",
             "KEX GOLF COLLAR"]
    write_dxf(repo / "drawings" / "N-0.dxf", words)
    db = tmp_path / "i.sqlite"
    extract_commit_index(repo, ["N-0"], db, taxdir)
    tax = fresh_tax(repo)
    note = upload_notification(db, tax, "N-0", "web")
    assert note["pending_total"] == 7
    assert len(note["clusters"]) == 5 and note["more"] == 2


def test_25_banner_when_queue_abandoned(tmp_path):
    from gitail.resolve import upload_notification
    repo = make_repo(tmp_path)
    taxdir = owned(repo)
    for i, w in enumerate(["QUX ALPHA", "QUX BETA", "QUX GAMMA"]):
        write_dxf(repo / "drawings" / f"B-{i}.dxf", [f"{w} FLANGE"])
    db = tmp_path / "i.sqlite"
    extract_commit_index(repo, [f"B-{i}" for i in range(3)], db, taxdir)
    tax = fresh_tax(repo)
    note = upload_notification(db, tax, "B-0", "web", abandon_threshold=2)
    assert note["banner"] is True
    note = upload_notification(db, tax, "B-0", "web", abandon_threshold=50)
    assert note["banner"] is False


# --------------------------------------------------------------------------
# 14/15: crops exist; sibling context ranks door parts above roof parts

def test_14_crop_generated_with_context_highlight(tmp_path):
    from gitail.resolve import clusters
    repo = make_repo(tmp_path)
    taxdir = owned(repo)
    write_dxf(repo / "drawings" / "C-1.dxf", [EZY])
    db = tmp_path / "i.sqlite"
    extract_commit_index(repo, ["C-1"], db, taxdir)
    tax = fresh_tax(repo)
    groups = clusters(db, tax)
    assert len(groups) == 1
    rel = groups[0]["crop"]
    assert rel is not None
    crop = repo / rel
    assert crop.exists(), rel
    import matplotlib.image as mpimg
    img = mpimg.imread(str(crop))
    assert float(img.mean()) < 0.999  # ink on the page, not blank


def test_15_sibling_context_boosts_door_parts(tmp_path):
    from gitail.resolve import suggestions
    repo = make_repo(tmp_path)
    tax = fresh_tax(repo)
    door = suggestions(tax, "LINING", ["component.door", "part.jamb"])
    plain = suggestions(tax, "LINING", [])
    assert door and plain
    # without context the exact label wins; with door context the door part
    # (matched via synonym) outranks it
    assert plain[0]["node"] == "part.lining"
    assert door[0]["node"] == "part.jamb_liner"
    assert "sibling" in door[0]["why"]


# --------------------------------------------------------------------------
# 16/17/21/23/24: assign, auto-classify, unsure, git commit, undo

def _ezy_repo(tmp_path, n=2):
    repo = make_repo(tmp_path)
    taxdir = owned(repo)
    for i in range(n):
        write_dxf(repo / "drawings" / f"E-{i}.dxf", [EZY, "3mm GLASS"])
    db = tmp_path / "i.sqlite"
    extract_commit_index(repo, [f"E-{i}" for i in range(n)], db, taxdir)
    return repo, db, taxdir


def test_16_assign_reclassifies_and_synonymises(tmp_path):
    from gitail.resolve import assign, clusters
    repo, db, taxdir = _ezy_repo(tmp_path)
    tax = fresh_tax(repo)
    key = clusters(db, tax)[0]["cluster_key"]
    out = assign(db, taxdir, key, "part.jamb_liner", actor="t",
                 repo=repo)
    assert out["reclassified"] == 2  # one occurrence per drawing x2
    assert EZY in out["synonyms_added"]
    tax = fresh_tax(repo)
    assert clusters(db, tax) == []  # queue clear
    assert "ezy cav flange" in {s.casefold()
                                for s in tax.nodes["part.jamb_liner"]["synonyms"]}
    # committed detail files gained the tree classification
    found = False
    for path in sorted((repo / "details").rglob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        for d in data["details"]:
            for c in d["classifications"]:
                if "part.jamb_liner" in c["path"]:
                    found = True
                    assert c["confidence"] == "medium"
                    assert "review_needed" not in c
    assert found
    # attributable, reviewable git commit
    log = subprocess.run(["git", "-C", str(repo), "log", "--oneline", "-3"],
                         capture_output=True, text=True).stdout
    assert "resolve:" in log and "part.jamb_liner" in log


def test_17_reupload_auto_classifies_no_notification(tmp_path):
    from gitail.resolve import assign, clusters, upload_notification
    from gitail.segment import build_sheet_details
    repo, db, taxdir = _ezy_repo(tmp_path, n=1)
    tax = fresh_tax(repo)
    key = clusters(db, tax)[0]["cluster_key"]
    assign(db, taxdir, key, "part.jamb_liner", actor="t", repo=repo)
    # same annotation again: resolves via the new synonym, no quarantine
    tax = fresh_tax(repo)
    from gitail.extract import extract_state
    from gitail.identity import resolve as _resolve
    from gitail.semantics import load_materials
    import ezdxf as _ez
    cfg = load_materials(CFG)
    raw = extract_state(repo / "drawings" / "E-0.dxf", cfg)
    resolved, _, _, _ = _resolve(raw, [], {"drawing": "E-0", "elements": {}})
    payload = build_sheet_details("E-0", resolved, cfg, {},
                                  _ez.readfile(str(repo / "drawings" / "E-0.dxf")),
                                  tax=tax)
    assert [c for c in payload["quarantine_candidates"]
            if c["normalised"] == "ezy cav flange"] == []
    assert any("part.jamb_liner" in c["path"]
               for d in payload["details"] for c in d["classifications"])
    note = upload_notification(db, tax, "E-0", "web")
    assert note["pending_total"] == 0


def test_21_unsure_flags_review(tmp_path):
    from gitail.resolve import assign, clusters
    repo, db, taxdir = _ezy_repo(tmp_path, n=1)
    tax = fresh_tax(repo)
    key = clusters(db, tax)[0]["cluster_key"]
    out = assign(db, taxdir, key, "part.jamb_liner", actor="t",
                 unsure=True, repo=repo)
    assert out["action"] == "unsure"
    flagged = False
    for path in sorted((repo / "details").rglob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        for d in data["details"]:
            for c in d["classifications"]:
                if "part.jamb_liner" in c["path"]:
                    assert c["confidence"] == "low" and c["review_needed"] is True
                    flagged = True
    assert flagged


def test_24_undo_reopens_and_cleans(tmp_path):
    from gitail.resolve import assign, clusters, undo_resolution
    repo, db, taxdir = _ezy_repo(tmp_path, n=1)
    tax = fresh_tax(repo)
    key = clusters(db, tax)[0]["cluster_key"]
    out = assign(db, taxdir, key, "part.jamb_liner", actor="t", repo=repo)
    undo = undo_resolution(db, taxdir, out["rid"], actor="t", repo=repo)
    assert undo["reverts"] == out["rid"] and undo["occurrences_reopened"] == 1
    tax = fresh_tax(repo)
    assert len(clusters(db, tax)) == 1  # back in quarantine
    assert "ezy cav flange" not in {s.casefold()
                                    for s in tax.nodes["part.jamb_liner"]["synonyms"]}
    for path in sorted((repo / "details").rglob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        for d in data["details"]:
            assert all(c.get("attribution_chain") != "resolution"
                       for c in d["classifications"])


# --------------------------------------------------------------------------
# 18/19/20/26: new-prefill, ignore, dismiss, approve-reclassifies

def test_18_new_prefill_then_definition_required(tmp_path):
    from gitail.register import approve
    from gitail.resolve import clusters, new_from_cluster
    from gitail.taxonomy import TaxonomyError
    repo, db, taxdir = _ezy_repo(tmp_path, n=1)
    tax = fresh_tax(repo)
    key = clusters(db, tax)[0]["cluster_key"]
    # no sibling context on a bare sheet: the form needs explicit parents
    import pytest
    with pytest.raises(Exception, match="no parent inferable"):
        new_from_cluster(db, taxdir, key, actor="t")
    out = new_from_cluster(db, taxdir, key, actor="t",
                           parents=["component.window"])
    assert out["prefill"]["level"] == "part"
    assert out["prefill"]["parents"] == ["component.window"]
    assert EZY in out["prefill"]["synonyms"] or \
        out["prefill"]["label"] == EZY
    assert (taxdir / "proposals" / f"{out['slug']}.yaml").exists()
    import pytest
    with pytest.raises(TaxonomyError, match="blank definition"):
        approve(taxdir, out["slug"])


def test_19_ignore_skips_quarantine_keeps_search(tmp_path):
    from gitail.resolve import clusters, ignore_cluster
    from gitail.segment import build_sheet_details
    repo, db, taxdir = _ezy_repo(tmp_path, n=1)
    tax = fresh_tax(repo)
    key = clusters(db, tax)[0]["cluster_key"]
    out = ignore_cluster(db, taxdir, key, actor="t")
    assert out["ignored"] == ["ezy cav flange"]
    tax = fresh_tax(repo)
    assert clusters(db, tax) == []
    # next upload: no notification ...
    from gitail.extract import extract_state
    from gitail.identity import resolve as _resolve
    from gitail.semantics import load_materials
    cfg = load_materials(CFG)
    raw = extract_state(repo / "drawings" / "E-0.dxf", cfg)
    resolved, _, _, _ = _resolve(raw, [], {"drawing": "E-0", "elements": {}})
    payload = build_sheet_details("E-0", resolved, cfg, {}, None, tax=tax)
    assert payload["quarantine_candidates"] == []
    # ... but still full-text searchable (ignoring != deleting)
    assert len(find(db, text="EZY", ever=True)) == 1


def test_20_dismiss_hides_only_for_dismisser(tmp_path):
    from gitail.resolve import clusters, dismiss_cluster
    repo, db, taxdir = _ezy_repo(tmp_path, n=1)
    tax = fresh_tax(repo)
    key = clusters(db, tax)[0]["cluster_key"]
    dismiss_cluster(db, key, "t")
    assert clusters(db, tax, actor="t") == []
    assert len(clusters(db, tax, actor="other")) == 1


def test_26_approve_new_node_clears_quarantine(tmp_path):
    from gitail.register import approve, propose
    from gitail.resolve import effective_occurrences
    repo, db, taxdir = _ezy_repo(tmp_path, n=1)
    tax = fresh_tax(repo)
    assert len(effective_occurrences(db, tax)) == 1
    propose(taxdir, "part", "Flange", ["component.window"],
            "Flat rim for bolting.", "d_0000", submitted_by="t")
    approve(taxdir, "flange")
    tax = fresh_tax(repo)
    assert effective_occurrences(db, tax) == []  # no manual re-tagging


# --------------------------------------------------------------------------
# CLI layer + serve end-to-end

def test_cli_unresolved_resolve_resolutions_digest(tmp_path):
    repo, db, taxdir = _ezy_repo(tmp_path, n=1)
    runner = CliRunner()
    r = runner.invoke(cli, ["unresolved", "--db", str(db),
                            "--taxonomy-dir", str(taxdir),
                            "--config-dir", str(CFG.parent)])
    assert r.exit_code == 0, r.output
    assert "EZY CAV FLANGE" in r.output
    import re
    key = re.search(r"c_[0-9a-f]+(?:-\d+)?", r.output).group(0)
    r = runner.invoke(cli, ["unresolved", "show", key, "--db", str(db),
                            "--taxonomy-dir", str(taxdir),
                            "--config-dir", str(CFG.parent)])
    assert r.exit_code == 0 and "suggestions" in r.output
    r = runner.invoke(cli, ["resolve", key, "--assign", "part.jamb_liner",
                            "--actor", "t", "--repo", str(repo),
                            "--db", str(db),
                            "--taxonomy-dir", str(taxdir),
                            "--config-dir", str(CFG.parent)])
    assert r.exit_code == 0, r.output
    r = runner.invoke(cli, ["resolutions", "--db", str(db)])
    assert r.exit_code == 0 and "assign" in r.output and "part.jamb_liner" in r.output
    rid = [l.split()[0] for l in r.output.splitlines() if "assign" in l][0]
    r = runner.invoke(cli, ["digest", "--db", str(db),
                            "--taxonomy-dir", str(taxdir),
                            "--config-dir", str(CFG.parent)])
    assert r.exit_code == 0 and "quarantine" in r.output
    r = runner.invoke(cli, ["resolutions", "undo", rid, "--actor", "t",
                            "--repo", str(repo), "--db", str(db),
                            "--taxonomy-dir", str(taxdir)])
    assert r.exit_code == 0, r.output


def test_serve_upload_notification_queue_resolve(tmp_path):
    import json as _json
    import threading
    import urllib.parse
    import urllib.request
    from http.server import ThreadingHTTPServer
    from gitail.serve import Handler
    repo = make_repo(tmp_path)
    taxdir = owned(repo)
    ctx = {"repo": str(repo), "db": str(repo / "index.sqlite"),
           "drawings_dir": str(repo / "drawings"),
           "state_dir": str(repo / "state"),
           "pdf_dir": str(repo / "pdf"),
           "details_dir": str(repo / "details"),
           "thumbs_dir": str(repo / "thumbs"),
           "crops_dir": str(repo / "crops"),
           "taxonomy_dir": str(taxdir),
           "config_dir": str(CFG.parent)}
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.gitail_ctx = ctx
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        doc = ezdxf.new("R2018")
        msp = doc.modelspace()
        msp.add_mtext(EZY, dxfattribs={"layer": "A-DETL-ANNO"}).dxf.insert = (0, 0)
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".dxf", delete=False) as f:
            doc.saveas(f.name)
            data = Path(f.name).read_bytes()
        boundary = "BOUNDARY123"
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"project\"\r\n\r\n\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"S-1.dxf\"\r\n"
                f"Content-Type: application/octet-stream\r\n\r\n").encode() + data + \
            f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(
            base + "/api/upload", data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req) as resp:
            summary = _json.loads(resp.read())
        assert summary["committed"] is True
        note = summary["notification"]
        assert note["pending_total"] == 1 and len(note["clusters"]) == 1
        with urllib.request.urlopen(base + "/api/queue?actor=web") as resp:
            queue = _json.loads(resp.read())
        assert queue["pending_total"] == 1
        key = queue["clusters"][0]["cluster_id"]
        assert queue["clusters"][0]["crop"]
        with urllib.request.urlopen(base + "/api/tree-search?" +
                                    urllib.parse.urlencode({"q": "ezy"})) as resp:
            tree = _json.loads(resp.read())
        assert tree["results"] == []  # no node: absent from tree ...
        assert len(tree["unclassified"]) == 1  # ... but listed separately
        req = urllib.request.Request(
            base + "/api/resolve", data=_json.dumps(
                {"op": "assign", "key": key, "target": "part.jamb_liner",
                 "actor": "web"}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as resp:
            done = _json.loads(resp.read())
        assert done["ok"] is True
        with urllib.request.urlopen(base + "/api/queue?actor=web") as resp:
            assert _json.loads(resp.read())["pending_total"] == 0
    finally:
        srv.shutdown()


def test_auto_promote_gated_off_by_default(tmp_path):
    """8.7.6: off by default (no-op); enabled, only value-level free-text
    candidates at 10+ occurrences across 3+ sheets promote — and land flagged
    review_needed, never silent. Components/parts can never auto-create."""
    import sqlite3
    from gitail.resolve import maybe_auto_promote
    from gitail.taxonomy import load_settings
    repo = make_repo(tmp_path)
    taxdir = owned(repo)
    settings = load_settings(str(CFG.parent))
    assert settings["auto_promote"] is False
    db = tmp_path / "i.sqlite"
    con = sqlite3.connect(str(db))
    con.executescript("CREATE TABLE quarantine (uid TEXT PRIMARY KEY,"
                      " raw_string TEXT, normalised TEXT, level_guess TEXT,"
                      " attribute TEXT, material TEXT, part TEXT, value REAL,"
                      " qualifier TEXT, element_id TEXT, detail_id TEXT,"
                      " drawing TEXT, project TEXT, commit_sha TEXT,"
                      " commit_date TEXT, evidence TEXT, crop TEXT, reason TEXT,"
                      " status TEXT DEFAULT 'pending', resolved_by TEXT,"
                      " updated_at TEXT);"
                      "CREATE TABLE resolutions (id INTEGER PRIMARY KEY,"
                      " rid TEXT UNIQUE, created_at TEXT, actor TEXT,"
                      " action TEXT, cluster_key TEXT, target TEXT,"
                      " detail_ids TEXT, synonyms_added TEXT, occurrences TEXT,"
                      " taxonomy_commit TEXT, note TEXT, undone INTEGER DEFAULT 0);"
                      "CREATE TABLE dismissals (cluster_key TEXT, actor TEXT,"
                      " dismissed_at TEXT, PRIMARY KEY (cluster_key, actor));")
    for sheet in range(3):
        for i in range(4):
            uid = f"u_q{sheet}{i}"
            con.execute(
                "INSERT INTO quarantine (uid,raw_string,normalised,level_guess,"
                " attribute,material,part,element_id,detail_id,drawing,"
                " commit_sha,commit_date,evidence,status) VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (uid, "CHARTREUSE", "chartreuse", "value", "attr.colour",
                 "chartreuse", None, f"e_{i}", f"d_{sheet}", f"S-{sheet}",
                 "c" * 40, "2026-09-18T00:00:00", "{}", "pending"))
    # part-level noise must never promote even when enabled
    con.execute(
        "INSERT INTO quarantine (uid,raw_string,normalised,level_guess,"
        " element_id,detail_id,drawing,commit_sha,evidence,status) VALUES "
        "(?,?,?,?,?,?,?,?,?,?)",
        ("u_zzz", "WIDGET", "widget", "part", "e_9", "d_0", "S-0",
         "c" * 40, "{}", "pending"))
    con.commit()
    con.close()
    assert maybe_auto_promote(db, taxdir, settings) == []
    on = dict(settings, auto_promote=True)
    out = maybe_auto_promote(db, taxdir, on)
    assert len(out) == 1 and out[0]["id"] == "value.chartreuse"
    assert out[0]["review_needed"] is True and out[0]["occurrences"] == 12
    tax = fresh_tax(repo)
    assert tax.nodes["value.chartreuse"]["created_by"] == "auto"
    assert tax.nodes["value.chartreuse"]["review_needed"] is True
