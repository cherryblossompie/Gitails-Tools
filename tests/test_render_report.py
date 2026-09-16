"""Render (DXF->PDF) + report PDF links."""
from pathlib import Path

import ezdxf

from gitail.render import check_all, render_all, render_dxf_to_pdf
from gitail.report import write_html


def _dxf(path: Path, text="3mm GLASS"):
    doc = ezdxf.new("R2018")
    msp = doc.modelspace()
    msp.add_mtext(text, dxfattribs={"layer": "A-DETL-ANNO"}).dxf.insert = (0, 0)
    msp.add_line((0, 0), (10, 0), dxfattribs={"layer": "A-DETL-STEL"})
    doc.saveas(str(path))


def test_render_and_check(tmp_path):
    drw = tmp_path / "drawings"
    pdf = tmp_path / "pdf"
    drw.mkdir()
    _dxf(drw / "D-1.dxf")
    (drw / "StageC").mkdir()
    _dxf(drw / "StageC" / "D-2.dxf", "AR-CONC")
    assert check_all(drw, pdf) != []
    res = render_all(drw, pdf)
    assert all(r["action"] == "rendered" for r in res)
    assert (pdf / "D-1.pdf").exists() and (pdf / "StageC" / "D-2.pdf").exists()
    assert check_all(drw, pdf) == []
    # up-to-date second run
    res2 = render_all(drw, pdf)
    assert {r["action"] for r in res2} == {"up-to-date"}


def test_report_links_pdf(tmp_path):
    import sqlite3
    db = tmp_path / "i.sqlite"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE element_state (element_id TEXT, commit_sha TEXT, commit_date TEXT,"
                " author TEXT, commit_message TEXT, drawing TEXT, project TEXT, type TEXT, layer TEXT,"
                " material TEXT, value REAL, unit TEXT, text_raw TEXT, x REAL, y REAL, status TEXT,"
                " match_tier TEXT, match_confidence REAL, PRIMARY KEY (element_id, commit_sha))")
    con.execute("INSERT INTO element_state VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("e_1", "a" * 40, "2026-01-01", "a", "m", "StageC/D-1", "StageC", "MTEXT",
                 "A", "concrete", None, None, "X", 0, 0, "new", "new", 1.0))
    con.commit()
    con.close()
    (tmp_path / "pdf").mkdir()
    html = write_html(db, tmp_path / "r.html", pdf_dir=tmp_path / "pdf")
    text = html.read_text(encoding="utf-8")
    assert "pdf/StageC/D-1.pdf" in text  # PDF-first link, not bare DXF
    assert 'id="bar"' in text and "concrete" in text  # single stacked bar + suggestions
    assert "must contain ALL" in text  # stacked AND semantics
