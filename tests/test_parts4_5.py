"""Parts 4-5: index (incremental) + query (find/history/at/changed)."""
import json
import subprocess
from pathlib import Path

import ezdxf

from gitail.cli import cli
from gitail.extract import extract_state
from gitail.identity import resolve
from gitail.index import build_index
from gitail.query import at, changed, find, history, search_stacked
from gitail.report import write_html, write_markdown
from gitail.semantics import load_materials
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
    from gitail.cli import _load_idmap, _load_jsonl
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


def test_current_snapshot_never_duplicates(tmp_path):
    """Commit 2 touches only drawing B; drawing A rows must still appear once."""
    repo = make_repo(tmp_path)
    write_dxf(repo / "drawings" / "A.dxf", "3mm GLASS")
    extract_to_repo(repo / "drawings" / "A.dxf", repo)
    git(repo, "add", ".")
    git(repo, "commit", "-m", "A only")
    write_dxf(repo / "drawings" / "B.dxf", "12 THK PLYWOOD")
    extract_to_repo(repo / "drawings" / "B.dxf", repo)
    git(repo, "add", ".")
    git(repo, "commit", "-m", "add B, A untouched")
    db = tmp_path / "index.sqlite"
    build_index(repo, db)
    cur = find(db, ever=False)
    assert sorted((r["drawing"], r["element_id"]) for r in cur) == sorted(set(
        (r["drawing"], r["element_id"]) for r in cur))
    assert sum(1 for r in cur if r["drawing"] == "A") == 2  # 1 MTEXT + 1 LINE
    stacked = search_stacked(db, [("material", "glass")])
    assert [d["drawing"] for d in stacked["drawings"]] == ["A"]
    assert sum(1 for r in stacked["rows"] if r["drawing"] == "A") == 2


def test_stacked_and_across_chips(tmp_path):
    repo = make_repo(tmp_path)
    doc = ezdxf.new("R2018")
    msp = doc.modelspace()
    msp.add_mtext("3mm GLASS", dxfattribs={"layer": "A"}).dxf.insert = (0, 0)
    msp.add_mtext("12 THK PLYWOOD", dxfattribs={"layer": "A"}).dxf.insert = (0, 10)
    doc.saveas(str(repo / "drawings" / "MIX.dxf"))
    extract_to_repo(repo / "drawings" / "MIX.dxf", repo)
    write_dxf(repo / "drawings" / "PLAIN.dxf", "3mm GLASS")
    extract_to_repo(repo / "drawings" / "PLAIN.dxf", repo)
    git(repo, "add", ".")
    git(repo, "commit", "-m", "mix + plain")
    db = tmp_path / "index.sqlite"
    build_index(repo, db)
    res = search_stacked(db, [("material", "glass"), ("material", "plywood")])
    assert [d["drawing"] for d in res["drawings"]] == ["MIX"]  # PLAIN lacks plywood
    assert {r["element_id"] for r in res["rows"]} == \
        {r["element_id"] for r in find(db, drawing="MIX", ever=False)}
    assert any(r["matched"] for r in res["rows"])


def test_stacked_lists_latest_deletions(tmp_path):
    repo = make_repo(tmp_path)
    write_dxf(repo / "drawings" / "A.dxf", "3mm GLASS")
    extract_to_repo(repo / "drawings" / "A.dxf", repo)
    git(repo, "add", ".")
    git(repo, "commit", "-m", "A with glass")
    # wipe the drawing's content: element deleted in latest revision
    doc = ezdxf.new("R2018")
    doc.saveas(str(repo / "drawings" / "A.dxf"))
    extract_to_repo(repo / "drawings" / "A.dxf", repo)
    git(repo, "add", ".")
    git(repo, "commit", "-m", "A emptied")
    db = tmp_path / "index.sqlite"
    build_index(repo, db)
    res = search_stacked(db, [("material", "glass")])
    assert [d["drawing"] for d in res["drawings"]] == ["A"]  # found via its deletion
    assert res["rows"] == []
    assert len(res["deleted"]) == 2  # MTEXT + LINE tombstones
    assert all(x["status"] == "deleted" for x in res["deleted"])


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


def test_part_facet_end_to_end(tmp_path):
    """'INTERNAL TIMBER LINING' is found via material=timber AND part=lining."""
    repo = make_repo(tmp_path)
    doc = ezdxf.new("R2018")
    msp = doc.modelspace()
    msp.add_mtext("INTERNAL TIMBER LINING", dxfattribs={"layer": "A"}).dxf.insert = (0, 0)
    msp.add_mtext("SELECTED TIMBER DECKING", dxfattribs={"layer": "A"}).dxf.insert = (0, 10)
    msp.add_mtext("3mm GLASS", dxfattribs={"layer": "A"}).dxf.insert = (0, 20)
    doc.saveas(str(repo / "drawings" / "F.dxf"))
    extract_to_repo(repo / "drawings" / "F.dxf", repo)
    git(repo, "add", ".")
    git(repo, "commit", "-m", "finishes")
    db = tmp_path / "index.sqlite"
    build_index(repo, db)
    assert {r["text_raw"] for r in find(db, material="timber", ever=False)} == \
        {"INTERNAL TIMBER LINING", "SELECTED TIMBER DECKING"}
    assert [r["text_raw"] for r in find(db, part="lining", ever=False)] == \
        ["INTERNAL TIMBER LINING"]
    res = search_stacked(db, [("material", "timber"), ("part", "lining")])
    assert [d["drawing"] for d in res["drawings"]] == ["F"]
    # matched = satisfies >=1 chip: lining matches both, decking matches timber
    assert {r["text_raw"] for r in res["rows"] if r["matched"]} == \
        {"INTERNAL TIMBER LINING", "SELECTED TIMBER DECKING"}
    res2 = search_stacked(db, [("part", "lining"), ("part", "decking")])
    assert [d["drawing"] for d in res2["drawings"]] == ["F"]  # drawing holds both parts
    # CLI parity incl. stacked auto-switch
    runner = CliRunner()
    r = runner.invoke(cli, ["find", "--db", str(db), "--part", "lining", "--json"])
    assert r.exit_code == 0
    assert json.loads(r.output)[0]["text_raw"] == "INTERNAL TIMBER LINING"
    r = runner.invoke(cli, ["find", "--db", str(db), "--material", "timber",
                            "--part", "decking", "--json"])
    assert r.exit_code == 0
    got = json.loads("\n".join(l for l in r.output.splitlines() if not l.startswith("note:")))
    assert [d["drawing"] for d in got["drawings"]] == ["F"]
