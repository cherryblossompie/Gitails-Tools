"""Static HTML + markdown views (no server). The search interface."""
from __future__ import annotations

import html
import json
import sqlite3
from pathlib import Path

from .query import COLS


def _rows(db: Path) -> list[dict]:
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(f"SELECT rowid, {','.join(COLS)} FROM element_state ORDER BY rowid DESC LIMIT 5000")]
    # latest commit per drawing = greatest rowid (insert order = git log oldest->newest)
    latest: dict[str, str] = {}
    for r in rows:
        if r["drawing"] not in latest:
            latest[r["drawing"]] = r["commit_sha"]
    for r in rows:
        r["is_current"] = (r["commit_sha"] == latest.get(r["drawing"]) and r["status"] != "deleted")
        r.pop("rowid", None)
    con.close()
    return rows


def write_markdown(db: Path, out: Path) -> Path:
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    out = Path(out)
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write("# arcdiff report\n\n")
        for d in con.execute("SELECT DISTINCT drawing FROM element_state ORDER BY drawing"):
            f.write(f"## {d['drawing']}\n\n")
            f.write("| element | material | value | text | status | commit | author |\n")
            f.write("|---|---|---|---|---|---|---|\n")
            for r in con.execute(
                    "SELECT * FROM element_state WHERE drawing=? AND commit_sha="
                    "(SELECT commit_sha FROM element_state WHERE drawing=? ORDER BY commit_date DESC LIMIT 1)"
                    " AND status!='deleted' ORDER BY element_id", (d["drawing"], d["drawing"])):
                f.write(f"| {r['element_id']} | {r['material'] or ''} | {r['value'] or ''} "
                        f"| {(r['text_raw'] or '').replace('|', '/')} | {r['status']} "
                        f"| {r['commit_sha'][:7]} | {r['author'] or ''} |\n")
            f.write("\n")
    con.close()
    return out


def write_html(db: Path, out: Path) -> Path:
    rows = _rows(db)
    payload = json.dumps(rows, ensure_ascii=False).replace("</", "<\\/")
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>arcdiff — element-state search</title>
<style>
body{{font-family:system-ui,Segoe UI,Arial,sans-serif;margin:0;background:#fafafa;color:#222}}
header{{background:#111;color:#fff;padding:14px 18px}}
main{{padding:16px 18px;max-width:1100px}}
.filters{{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0}}
input,select{{padding:8px;border:1px solid #ccc;border-radius:6px;font-size:14px}}
table{{border-collapse:collapse;width:100%;background:#fff;font-size:13px}}
th,td{{border:1px solid #e3e3e3;padding:6px 8px;text-align:left}}
th{{background:#f0f0f0;position:sticky;top:0}}
.count{{color:#666;font-size:13px}}
code{{background:#eee;padding:1px 4px;border-radius:4px}}
</style></head><body>
<header><h2 style="margin:0">arcdiff — search element history</h2>
<div style="opacity:.75;font-size:13px">Static report. No server. Filter locally — e.g. material <code>glass</code>, value <code>3</code>.</div></header>
<main>
<div class="filters">
<input id="q" placeholder="text search (e.g. TOUGHENED)" style="flex:2;min-width:200px">
<input id="mat" placeholder="material (e.g. glass)" style="flex:1;min-width:120px">
<input id="val" placeholder="value (e.g. 3)" style="width:110px">
<input id="drw" placeholder="drawing (e.g. D-101)" style="flex:1;min-width:120px">
<label style="align-self:center;font-size:13px"><input type="checkbox" id="ever"> ever (history, not just current)</label>
</div>
<div class="count" id="count"></div>
<table><thead><tr>
<th>drawing</th><th>element</th><th>material</th><th>value</th><th>text</th><th>status</th><th>commit</th><th>date</th><th>author</th>
</tr></thead><tbody id="body"></tbody></table>
<script>
const ROWS = {payload};
const q=document.getElementById('q'),mat=document.getElementById('mat'),
      val=document.getElementById('val'),drw=document.getElementById('drw'),
      ever=document.getElementById('ever'),body=document.getElementById('body'),
      count=document.getElementById('count');
// current = server-computed is_current flag (rowid order, not dates)
function render(){{
  const qt=q.value.toLowerCase(),mt=mat.value.toLowerCase(),vl=val.value.trim(),dw=drw.value.toLowerCase(),ev=ever.checked;
  const out=ROWS.filter(r=>{{
    if(!ev && !r.is_current) return false;
    if(mt && (r.material||'').toLowerCase()!==mt) return false;
    if(vl!=='' && String(r.value??'')!==vl) return false;
    if(dw && (r.drawing||'').toLowerCase()!==dw) return false;
    if(qt && !(r.text_raw||'').toLowerCase().includes(qt)) return false;
    return true;
  }}).slice(0,1000);
  count.textContent=out.length+' of '+ROWS.length+' rows (capped at 1000)';
  body.innerHTML=out.map(r=>'<tr><td>'+esc(r.drawing)+'</td><td><code>'+esc(r.element_id)+'</code></td><td>'+esc(r.material||'')+'</td><td>'+esc(r.value??'')+'</td><td>'+esc(r.text_raw||'')+'</td><td>'+esc(r.status||'')+'</td><td><code>'+esc((r.commit_sha||'').slice(0,7))+'</code></td><td>'+esc(r.commit_date||'')+'</td><td>'+esc(r.author||'')+'</td></tr>').join('');
}}
function esc(s){{return String(s).replace(/[&<>"]/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}}[c]));}}
[q,mat,val,drw].forEach(el=>el.addEventListener('input',render));ever.addEventListener('change',render);render();
</script></main></body></html>
"""
    out = Path(out)
    out.write_text(page, encoding="utf-8", newline="\n")
    return out
