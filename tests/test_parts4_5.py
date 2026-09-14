"""Parts 4-5: index (incremental) + query (find/history/at/changed)."""
import json
import subprocess
from pathlib import Path

import ezdxf

from arcdiff.cli import cli
from arcdiff.extract import extract_state
from arcdiff.identity import resolve
from arcdiff.index import build_index
from arcdiff.query import at, changed, find, history
from arcdiff.report import write_html, write_markdown
from arcdiff.semantics import load_materials
from click.testing import CliRunner

CFG = Path(__file__).parent.parent / "config" / "materials.yaml"


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "drw"
    repo.mkdir()
    (repo / "drawings").mkdir()
    (repo / "state").mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "t@t.t")
    git(repo, "config", "user.name", "t")
    return repo


def write_dxf(path: Path, glass="3mm GLASS"):
    doc = ezdxf.new("R2018")
    msp = doc.modelspace()
    msp.add_mtext(glass, dxfattribs={"layer": "A-DETL-ANNO"}).dxf.insert = (0, 0)
    msp.add_line((0, 0), (10, 0), dxfattribs={"layer": "A-DETL-STEL"})
    doc.saveas(str(path))


def extract_to_repo(dxf: Path, repo: Path):
    from arcdiff.cli import _load_idmap, _load_jsonl
    cfg = load_materials(CFG)
    drawing = dxf.stem
    raw = extract_state(dxf, cfg)
    prev = _load_jsonl(repo / "state" / f"{drawing}.jsonl")
    idmap = _load_idmap(repo / "state" / f"{drawing}.idmap.json", drawing)
    resolved, new_idmap, _, _ = resolve(raw, prev, idmap)
    storable = [{k: v for k, v in r.items()
                 if k not in ("status", "match_tier", "match_confidence") and not k.startswith("_")}
                for r in resolved]
    with open(repo / "state" / f"{drawing}.jsonl", "w", encoding="utf-8", newline="\n") as f:
        for r in storable:
            f.write(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n")
    with open(repo / "state" / f"{drawing}.idmap.json", "w", encoding="utf-8") as f:
        json.dump(new_idmap, f, sort_keys=True, indent=2)
        f.write("\n")
    return storable[0]["element_id"] if storable else None


def test_index_incremental_and_query(tmp_path):
    repo = make_repo(tmp_path)
    dxf = repo / "drawings" / "D-101.dxf"
    write_dxf(dxf, "3mm GLASS")
    extract_to_repo(dxf, repo)
    git(repo, "add", ".")
    git(repo, "commit", "-m", "Rev A glazing 3mm")
    db = tmp_path / "index.sqlite"
    r1 = build_index(repo, db)
    assert r1["commits_processed"] == 1

    # re-run with no new commits -> zero work
    r2 = build_index(repo, db)
    assert r2["commits_processed"] == 0

    # Rev B: 3mm -> 2mm
    write_dxf(dxf, "2mm GLASS")
    extract_to_repo(dxf, repo)
    git(repo, "add", ".")
    git(repo, "commit", "-m", "Rev B glazing 2mm")
    r3 = build_index(repo, db)
    assert r3["commits_processed"] == 1

    # THE brief query: ever 3mm
    hits = find(db, material="glass", value=3, ever=True)
    assert len(hits) == 1
    assert hits[0]["text_raw"] == "3mm GLASS"
    assert hits[0]["drawing"] == "D-101"
    # current state is 2mm
    cur = find(db, material="glass", ever=False)
    assert len(cur) == 1 and cur[0]["value"] == 2

    eid = hits[0]["element_id"]
    h = history(db, eid)
    assert [x["value"] for x in h] == [3, 2]
    assert h[0]["status"] == "new" and h[1]["status"] == "value_changed"

    log = subprocess.run(["git", "-C", str(repo), "log", "--reverse", "--format=%H"],
                         capture_output=True, text=True, check=True).stdout.split()
    ch = changed(db, log[0], log[1])
    assert len(ch) == 1
    assert ch[0]["before"]["value"] == 3 and ch[0]["after"]["value"] == 2

    snap = at(db, log[0][:7], "D-101")
    assert any(r["value"] == 3 for r in snap)

    html = write_html(db, tmp_path / "r.html")
    md = write_markdown(db, tmp_path / "r.md")
    assert html.exists() and md.exists()
    assert "3mm" in html.read_text(encoding="utf-8") or "glass" in html.read_text(encoding="utf-8")


def test_cli_extract_check(tmp_path):
    repo = make_repo(tmp_path)
    dxf = repo / "drawings" / "D-101.dxf"
    write_dxf(dxf, "3mm GLASS")
    runner = CliRunner()
    r = runner.invoke(cli, ["extract", str(dxf), "--state-dir", str(repo / "state"),
                            "--config-dir", str(CFG.parent)])
    assert r.exit_code == 0
    # in sync now
    r = runner.invoke(cli, ["extract", str(dxf), "--state-dir", str(repo / "state"),
                            "--config-dir", str(CFG.parent), "--check"])
    assert r.exit_code == 0
    # modify without extract -> check fails
    write_dxf(dxf, "5mm GLASS")
    r = runner.invoke(cli, ["extract", str(dxf), "--state-dir", str(repo / "state"),
                            "--config-dir", str(CFG.parent), "--check"])
    assert r.exit_code == 1
