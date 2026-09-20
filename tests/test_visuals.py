"""7.6 thumbnails/phash/near-duplicates, evidence crops, node-merge (8.9 #10),
facet confirm (7.7) — the remaining ingest-surface behaviours."""
import json
import shutil
import subprocess
from pathlib import Path

import ezdxf
from click.testing import CliRunner

from gitail.cli import cli
from gitail.render import finalize_visuals, hamming, phash_hex, render_window

CFG = Path(__file__).parent.parent / "config" / "materials.yaml"
FIXTURE = Path(__file__).parent / "fixtures" / "architrave.dxf"


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


def test_phash_separates_identical_shifted_different():
    a = Path(__import__("tempfile").mkdtemp())
    render_window(FIXTURE, [0, -12, 173, 175], a / "a1.png")
    render_window(FIXTURE, [0, -12, 173, 175], a / "a1b.png")
    render_window(FIXTURE, [2, -10, 175, 177], a / "a1s.png")
    render_window(FIXTURE, [200, 0, 320, 300], a / "a2.png")
    h1, h1b = phash_hex(a / "a1.png"), phash_hex(a / "a1b.png")
    h1s, h2 = phash_hex(a / "a1s.png"), phash_hex(a / "a2.png")
    assert len(h1) == 64  # 256-bit hex
    assert hamming(h1, h1b) == 0
    assert hamming(h1, h1s) <= 32
    assert hamming(h1, h2) > 32


def test_extract_writes_thumbs_and_phash(tmp_path):
    """7.9 #10 (shape): two distinct records with measured similarity, flagged
    only when actually near — never merged. The reference pair differs
    substantially (hash distance ~60), so both list no duplicates."""
    dxf = tmp_path / "A.204.dxf"
    shutil.copy(FIXTURE, dxf)
    runner = CliRunner()
    r = runner.invoke(cli, ["extract", str(dxf),
                            "--state-dir", str(tmp_path / "state"),
                            "--details-dir", str(tmp_path / "details"),
                            "--drawings-dir", str(tmp_path),
                            "--thumbs-dir", str(tmp_path / "thumbs"),
                            "--crops-dir", str(tmp_path / "crops"),
                            "--config-dir", str(CFG.parent)])
    assert r.exit_code == 0, r.output
    thumbs = sorted((tmp_path / "thumbs").rglob("*.png"))
    assert len(thumbs) == 2
    payload = json.loads((tmp_path / "details" / "A.204.json")
                         .read_text(encoding="utf-8"))
    assert len(payload["details"]) == 2  # distinct records, never merged
    for d in payload["details"]:
        assert d["thumbnail"] and d["thumbnail"].startswith("thumbs/")
        assert d["phash"] and len(d["phash"]) == 64
        assert d["near_duplicates"] == []
    h = [d["phash"] for d in payload["details"]]
    assert hamming(h[0], h[1]) > 32  # measured, not assumed


def test_finalize_dry_run_writes_nothing(tmp_path):
    from gitail.segment import build_sheet_details
    from gitail.extract import extract_state
    from gitail.identity import resolve
    from gitail.semantics import load_materials
    cfg = load_materials(CFG)
    raw = extract_state(FIXTURE, cfg)
    resolved, _, _, _ = resolve(raw, [], {"drawing": "A.204", "elements": {}})
    doc = ezdxf.readfile(str(FIXTURE))
    payload = build_sheet_details("A.204", resolved, cfg, {}, doc)
    by_eid = {r["element_id"]: r for r in resolved}
    out = finalize_visuals(FIXTURE, payload, by_eid, tmp_path / "t",
                           tmp_path / "c", "A.204", dry_run=True)
    assert len(out["files"]) >= 2  # thumbs at minimum
    assert not (tmp_path / "t").exists() and not (tmp_path / "c").exists()


def test_node_merge_rewrites_classifications(tmp_path):
    """8.9 #10 (full): deprecating a node rewrites every referencing
    classification and leaves no drawing unreachable."""
    from gitail.taxonomy import init_taxonomy, load_taxonomy
    repo = tmp_path / "repo"
    (repo / "details").mkdir(parents=True)
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "t@t.t")
    git(repo, "config", "user.name", "t")
    taxdir = repo / "taxonomy"
    init_taxonomy(taxdir)
    (repo / "details" / "D.json").write_text(json.dumps({
        "drawing": "D", "segmentation": "certain", "details": [{
            "detail_id": "d_1", "title": "T", "detail_tag": "SHEET",
            "source_sheet": "D", "classifications": [
                {"path": ["component.roof", "part.capping", "attr.material",
                           "value.steel"],
                 "confidence": "high", "attribution_chain": "leader",
                 "element_id": "e_1"}]}]}), encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "init")
    runner = CliRunner()
    # register a near-twin, approve it, then merge it back into capping
    r = runner.invoke(cli, ["register", "--level", "part", "--label", "Drip Channel",
                            "--parents", "component.roof",
                            "--definition", "Channel shedding water.",
                            "--example-detail", "d_1",
                            "--taxonomy-dir", str(taxdir),
                            "--config-dir", str(CFG.parent)])
    assert r.exit_code == 0, r.output
    r = runner.invoke(cli, ["proposals", "approve", "drip_channel",
                            "--taxonomy-dir", str(taxdir),
                            "--config-dir", str(CFG.parent)])
    assert r.exit_code == 0, r.output
    # point the drawing at the twin, then merge the twin away
    data = json.loads((repo / "details" / "D.json").read_text(encoding="utf-8"))
    data["details"][0]["classifications"][0]["path"][1] = "part.drip_channel"
    (repo / "details" / "D.json").write_text(json.dumps(data), encoding="utf-8")
    r = runner.invoke(cli, ["node-merge", "part.drip_channel",
                            "--into", "part.capping",
                            "--repo", str(repo),
                            "--taxonomy-dir", str(taxdir)])
    assert r.exit_code == 0, r.output
    data = json.loads((repo / "details" / "D.json").read_text(encoding="utf-8"))
    assert data["details"][0]["classifications"][0]["path"][1] == "part.capping"
    tax = load_taxonomy(taxdir)
    assert tax.resolve("part.drip_channel") == "part.capping"


def test_details_confirm_human_facets(tmp_path):
    """7.7: the uploader confirms/corrects pre-filled facets; component
    prefixes re-derive, classified_by flips to human."""
    dxf = tmp_path / "A.204.dxf"
    shutil.copy(FIXTURE, dxf)
    repo = tmp_path / "repo"
    (repo / "details").mkdir(parents=True)
    runner = CliRunner()
    r = runner.invoke(cli, ["extract", str(dxf),
                            "--state-dir", str(tmp_path / "state"),
                            "--details-dir", str(repo / "details"),
                            "--drawings-dir", str(tmp_path),
                            "--thumbs-dir", str(tmp_path / "thumbs"),
                            "--crops-dir", str(tmp_path / "crops"),
                            "--config-dir", str(CFG.parent)])
    assert r.exit_code == 0, r.output
    data = json.loads(next((repo / "details").glob("*.json"))
                      .read_text(encoding="utf-8"))
    did = next(d["detail_id"] for d in data["details"]
               if d["detail_tag"] == "AR 2")
    r = runner.invoke(cli, ["details", "confirm", did,
                            "--assembly", "door",
                            "--context", "interior",
                            "--repo", str(repo)])
    assert r.exit_code == 0, r.output
    data = json.loads(next((repo / "details").glob("*.json"))
                      .read_text(encoding="utf-8"))
    rec = next(d for d in data["details"] if d["detail_id"] == did)
    assert rec["classified_by"] == "human"
    assert rec["assembly"] == "door" and rec["context"] == "interior"
    assert "component.door" in rec["facet_nodes"]
    # material-only aluminium evidence now hangs under the confirmed component
    assert ["component.door", "attr.material", "value.aluminium"] in \
        [c["path"] for c in rec["classifications"]]


def _ingest_repo(tmp_path):
    repo = tmp_path / "repo"
    for d in ("drawings", "state", "details", "pdf", "thumbs", "crops"):
        (repo / d).mkdir(parents=True)
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "t@t.t")
    git(repo, "config", "user.name", "t")
    return repo


def test_ingest_dxf_png_dwg(tmp_path):
    """7.6 per-format ingest: DXF extracts+commits, PNG view-only, DWG waits
    for ODA export (never parsed)."""
    import base64
    repo = _ingest_repo(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    doc = ezdxf.new("R2018")
    doc.modelspace().add_mtext("3mm GLASS",
                               dxfattribs={"layer": "A"}).dxf.insert = (0, 0)
    doc.saveas(str(src / "I-1.dxf"))
    (src / "site.png").write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="))
    (src / "I-1.dwg").write_bytes(b"AutoCAD Binary DWG fake")
    runner = CliRunner()
    common = ["--repo", str(repo),
              "--drawings-dir", str(repo / "drawings"),
              "--state-dir", str(repo / "state"),
              "--pdf-dir", str(repo / "pdf"),
              "--details-dir", str(repo / "details"),
              "--thumbs-dir", str(repo / "thumbs"),
              "--crops-dir", str(repo / "crops"),
              "--config-dir", str(CFG.parent),
              "--db", str(repo / "index.sqlite")]
    r = runner.invoke(cli, ["ingest", str(src / "I-1.dxf")] + common)
    assert r.exit_code == 0, r.output
    assert (repo / "drawings" / "I-1.dxf").exists()
    assert (repo / "state" / "I-1.jsonl").exists()
    assert (repo / "details" / "I-1.json").exists()
    log = subprocess.run(["git", "-C", str(repo), "log", "--oneline"],
                         capture_output=True, text=True).stdout
    assert "Upload I-1 via gitail serve" in log
    r = runner.invoke(cli, ["ingest", str(src / "site.png")] + common)
    assert r.exit_code == 0 and "view-only" in r.output
    r = runner.invoke(cli, ["ingest", str(src / "I-1.dwg")] + common)
    assert r.exit_code == 0 and "never parsed" in r.output
