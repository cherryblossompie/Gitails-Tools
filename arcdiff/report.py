"""Static HTML + markdown views (no server). The search interface.

Search page features:
- project dropdown (from drawings/<project>/... layout) + autocomplete on
  every field via <datalist> (type 'c' -> concrete, ...).
- drawing names are links: relative drawings/<drawing>.dxf (works when the
  report sits at the repo root) + GitHub blob link.
- 'Add a drawing' helper: pick project + file, get the exact copy/extract/commit
  commands (static pages cannot write files, so this prints commands to run).
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

GITHUB_DRAWINGS_BASE = "https://github.com/cherryblossompie/Gitails-DRAWINGS/blob/main/drawings"


def _cols(con) -> list[str]:
    return [r[1] for r in con.execute("PRAGMA table_info(element_state)").fetchall()]


def _rows(db: Path) -> list[dict]:
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    have = set(_cols(con))
    cols = ["element_id", "commit_sha", "commit_date", "author", "commit_message",
            "drawing", "type", "layer", "material", "value", "unit",
            "text_raw", "x", "y", "status", "match_tier", "match_confidence"]
    if "project" in have:
        cols.insert(6, "project")
    rows = [dict(r) for r in con.execute(
        f"SELECT rowid, {','.join(cols)} FROM element_state ORDER BY rowid DESC LIMIT 5000")]
    for r in rows:
        if not r.get("project"):
            d = r.get("drawing") or ""
            r["project"] = d.split("/")[0] if "/" in d else ""
    latest: dict[str, str] = {}
    for r in rows:
        if r["drawing"] not in latest:
            latest[r["drawing"]] = r["commit_sha"]
    for r in rows:
        r["is_current"] = (r["commit_sha"] == latest.get(r["drawing"]) and r["status"] != "deleted")
        r["dxf_rel"] = f"drawings/{r['drawing']}.dxf"
        r["dxf_github"] = f"{GITHUB_DRAWINGS_BASE}/{r['drawing']}.dxf"
        r.pop("rowid", None)
    projects = sorted({r["project"] for r in rows if r["project"]})
    materials = sorted({str(r["material"]) for r in rows if r["material"]})
    drawings = sorted({str(r["drawing"]) for r in rows})
    texts = sorted({str(r["text_raw"]) for r in rows if r["text_raw"]})[:300]
    con.close()
    return rows, projects, materials, drawings, texts


def write_markdown(db: Path, out: Path) -> Path:
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    out = Path(out)
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write("# arcdiff report\n\n")
        for d in con.execute("SELECT DISTINCT drawing FROM element_state ORDER BY drawing"):
            f.write(f"## {d['drawing']}\n\n")
            f.write(f"Drawing file: `drawings/{d['drawing']}.dxf`\n\n")
            f.write("| element | material | value | text | status | commit | author |\n")
            f.write("|---|---|---|---|---|---|---|\n")
            for r in con.execute(
                    "SELECT * FROM element_state WHERE drawing=? AND commit_sha="
                    "(SELECT commit_sha FROM element_state WHERE drawing=? ORDER BY rowid DESC LIMIT 1)"
                    " AND status!='deleted' ORDER BY element_id", (d["drawing"], d["drawing"])):
                f.write(f"| {r['element_id']} | {r['material'] or ''} | {r['value'] or ''} "
                        f"| {(r['text_raw'] or '').replace('|', '/')} | {r['status']} "
                        f"| {r['commit_sha'][:7]} | {r['author'] or ''} |\n")
            f.write("\n")
    con.close()
    return out


def write_html(db: Path, out: Path) -> Path:
    rows, projects, materials, drawings, texts = _rows(db)
    payload = json.dumps(rows, ensure_ascii=False).replace("</", "<\\/")

    def opts(vals):
        return "\n".join(f'<option value="{v}"></option>' for v in vals)

    proj_opts = '<option value="">All projects</option>\n' + "\n".join(
        f'<option value="{p}">{p}</option>' for p in projects)
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>arcdiff — element-state search</title>
<style>
body{{font-family:system-ui,Segoe UI,Arial,sans-serif;margin:0;background:#fafafa;color:#222}}
header{{background:#111;color:#fff;padding:14px 18px}}
main{{padding:16px 18px;max-width:1150px}}
.filters{{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0}}
input,select{{padding:8px;border:1px solid #ccc;border-radius:6px;font-size:14px}}
table{{border-collapse:collapse;width:100%;background:#fff;font-size:13px}}
th,td{{border:1px solid #e3e3e3;padding:6px 8px;text-align:left}}
th{{background:#f0f0f0;position:sticky;top:0}}
.count{{color:#666;font-size:13px}}
code{{background:#eee;padding:1px 4px;border-radius:4px}}
.card{{background:#fff;border:1px solid #e3e3e3;border-radius:8px;padding:12px;margin:16px 0}}
.hint{{font-size:13px;color:#555}}
a.dxf{{font-weight:600}}
</style></head><body>
<header><h2 style="margin:0">arcdiff — search element history</h2>
<div style="opacity:.75;font-size:13px">Static report, no server. Type to get suggestions — e.g. material <code>c</code> → concrete. Click a drawing to open its DXF.</div></header>
<main>
<div class="filters">
<select id="proj">{proj_opts}</select>
<input id="mat" list="dl-mat" placeholder="material (e.g. concrete)" style="flex:1;min-width:140px">
<input id="val" placeholder="value (e.g. 3)" style="width:110px">
<input id="q" list="dl-text" placeholder="text (e.g. TOUGHENED)" style="flex:2;min-width:180px">
<input id="drw" list="dl-drw" placeholder="drawing (e.g. D-101)" style="flex:1;min-width:140px">
<label style="align-self:center;font-size:13px"><input type="checkbox" id="ever"> ever (history)</label>
</div>
<datalist id="dl-mat">{opts(materials)}</datalist>
<datalist id="dl-drw">{opts(drawings)}</datalist>
<datalist id="dl-text">{opts(texts)}</datalist>
<div class="count" id="count"></div>
<table><thead><tr>
<th>project</th><th>drawing</th><th>element</th><th>material</th><th>value</th><th>text</th><th>status</th><th>commit</th><th>date</th>
</tr></thead><tbody id="body"></tbody></table>

<div class="card">
<h3 style="margin-top:0">Add a drawing (where do I input?)</h3>
<div class="hint">Static pages cannot save files — pick the project + file here, run the 3 commands it prints in <code>Gitails-DRAWINGS</code>.</div>
<div class="filters">
<select id="add-proj"><option value="">(no project — drawings/ root)</option>{''.join(f'<option>{p}</option>' for p in projects)}<option value="__new__">+ New project…</option></select>
<input id="add-newproj" placeholder="new project name" style="display:none;min-width:160px">
<input type="file" id="add-file" accept=".dxf">
</div>
<pre id="add-cmds" class="hint">Select a .dxf file above.</pre>
</div>

<script>
const ROWS = {payload};
const proj=document.getElementById('proj'),mat=document.getElementById('mat'),
      val=document.getElementById('val'),q=document.getElementById('q'),drw=document.getElementById('drw'),
      ever=document.getElementById('ever'),body=document.getElementById('body'),
      count=document.getElementById('count');
function render(){{
  const pj=proj.value.toLowerCase(),mt=mat.value.toLowerCase(),vl=val.value.trim(),
        qt=q.value.toLowerCase(),dw=drw.value.toLowerCase(),ev=ever.checked;
  const out=ROWS.filter(r=>{{
    if(!ev && !r.is_current) return false;
    if(pj && (r.project||'').toLowerCase()!==pj) return false;
    // prefix-friendly: 'c' matches concrete (contains), autocomplete list shows options
    if(mt && !(r.material||'').toLowerCase().includes(mt)) return false;
    if(vl!=='' && String(r.value??'')!==vl) return false;
    if(dw && !(r.drawing||'').toLowerCase().includes(dw)) return false;
    if(qt && !(r.text_raw||'').toLowerCase().includes(qt)) return false;
    return true;
  }}).slice(0,1000);
  count.textContent=out.length+' of '+ROWS.length+' rows (capped at 1000)';
  body.innerHTML=out.map(r=>'<tr><td>'+esc(r.project||'—')+'</td>'
    +'<td><a class="dxf" href="'+esc(r.dxf_rel)+'">'+esc(r.drawing)+'</a> '
    +'<a href="'+esc(r.dxf_github)+'" title="Open on GitHub">↗</a></td>'
    +'<td><code>'+esc(r.element_id)+'</code></td><td>'+esc(r.material||'')+'</td><td>'+esc(r.value??'')+'</td>'
    +'<td>'+esc(r.text_raw||'')+'</td><td>'+esc(r.status||'')+'</td>'
    +'<td><code>'+esc((r.commit_sha||'').slice(0,7))+'</code></td><td>'+esc(r.commit_date||'')+'</td></tr>').join('');
}}
function esc(s){{return String(s).replace(/[&<>"]/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}}[c]));}}
[proj,mat,val,q,drw].forEach(el=>el.addEventListener('input',render));ever.addEventListener('change',render);render();
// add-drawing helper
const addProj=document.getElementById('add-proj'),addNew=document.getElementById('add-newproj'),
      addFile=document.getElementById('add-file'),addCmds=document.getElementById('add-cmds');
addProj.addEventListener('change',()=>{{addNew.style.display=addProj.value==='__new__'?'inline-block':'none';hint();}});
addNew.addEventListener('input',hint);addFile.addEventListener('change',hint);
function hint(){{
  const f=addFile.files[0];
  if(!f){{addCmds.textContent='Select a .dxf file above.';return;}}
  let p=addProj.value==='__new__'?addNew.value.trim():addProj.value;
  const dest=p?('drawings/'+p+'/'+f.name):('drawings/'+f.name);
  addCmds.textContent='1. copy file → '+dest+'\\n'
    +'2. C:\\\\AI\\\\python.exe -m arcdiff.cli extract '+dest+' --state-dir state --config-dir ..\\\\Gitails-Tools\\\\config\\n'
    +'3. git add '+dest+' state\\n   git commit -m "Add '+f.name+(p?(' to '+p):'')+'"';
}}
</script></main></body></html>
"""
    out = Path(out)
    out.write_text(page, encoding="utf-8", newline="\n")
    return out
