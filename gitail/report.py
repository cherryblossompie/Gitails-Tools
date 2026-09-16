"""Static HTML + markdown views (no server). The search interface.

Search page features:
- one stacked-filters bar (chips combine with AND at drawing level);
  suggestions appear grouped by kind as you type.
- collapsed drawing groups (click to expand matched rows); latest-commit
  deletions render struck-through so nothing vanishes silently.
- drawing names link their rendered PDF preview (+ GitHub links).
- 'Add a drawing' helper: pick project + file, get the exact copy/extract/commit
  commands (static pages cannot write files — use `gitail serve` for one-click).
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

GITHUB_DRAWINGS_BASE = "https://github.com/cherryblossompie/Gitails-DRAWINGS/blob/main/drawings"
GITHUB_PDF_BASE = "https://github.com/cherryblossompie/Gitails-DRAWINGS/blob/main/pdf"


def _cols(con) -> list[str]:
    return [r[1] for r in con.execute("PRAGMA table_info(element_state)").fetchall()]


def _rows(db: Path, pdf_base: str = GITHUB_PDF_BASE,
          dxf_base: str = GITHUB_DRAWINGS_BASE) -> tuple[list, list, list, list, list]:
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
        r["is_deleted_latest"] = (r["commit_sha"] == latest.get(r["drawing"]) and r["status"] == "deleted")
        # PDF first (renders the drawing), DXF second (text source).
        r["pdf_rel"] = f"pdf/{r['drawing']}.pdf"
        r["pdf_github"] = f"{pdf_base}/{r['drawing']}.pdf"
        r["dxf_rel"] = f"drawings/{r['drawing']}.dxf"
        r["dxf_github"] = f"{dxf_base}/{r['drawing']}.dxf"
        r.pop("rowid", None)
    projects = sorted({r["project"] for r in rows if r["project"]})
    materials = sorted({str(r["material"]) for r in rows if r["material"]})
    drawings = sorted({str(r["drawing"]) for r in rows})
    texts = sorted({str(r["text_raw"]) for r in rows if r["text_raw"]})[:300]
    values = sorted({r["value"] for r in rows if r["value"] is not None})
    con.close()
    return rows, projects, materials, drawings, texts, values


def _unindexed_pdfs(pdf_dir: Path, drawings_known: set[str]) -> list[dict]:
    """PDFs (incl. PDF-only inputs with no DXF) that have no indexed elements."""
    out = []
    pdf_dir = Path(pdf_dir)
    if not pdf_dir.exists():
        return out
    for pdf in sorted(pdf_dir.rglob("*.pdf")):
        try:
            did = pdf.relative_to(pdf_dir).with_suffix("").as_posix()
        except ValueError:
            continue
        if did not in drawings_known:
            out.append({"drawing": did,
                        "project": did.split("/")[0] if "/" in did else "",
                        "pdf_rel": f"pdf/{did}.pdf"})
    return out


def write_markdown(db: Path, out: Path) -> Path:
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    out = Path(out)
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write("# gitail report\n\n")
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


def write_html(db: Path, out: Path, pdf_dir: Path = Path("pdf"),
               drawings_dir: Path = Path("drawings"),
               github_base: str | None = None) -> Path:
    pdf_base = github_base or GITHUB_PDF_BASE
    rows, projects, materials, drawings, texts, values = _rows(db, pdf_base=pdf_base)
    unindexed = _unindexed_pdfs(Path(pdf_dir), {r["drawing"] for r in rows})
    payload = json.dumps(rows, ensure_ascii=False).replace("</", "<\\/")
    meta = {"materials": materials, "projects": projects, "drawings": drawings,
            "texts": texts, "values": [str(v) for v in values]}
    metajs = json.dumps(meta, ensure_ascii=False).replace("</", "<\\/")
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>gitail — element-state search</title>
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
#chips{{display:flex;gap:6px;flex-wrap:wrap;margin:8px 0}}
.chip{{background:#111;color:#fff;border-radius:20px;padding:4px 6px 4px 12px;font-size:13px;display:flex;gap:6px;align-items:center}}
.chip button{{background:#444;color:#fff;border:none;border-radius:50%;width:20px;height:20px;cursor:pointer;padding:0;line-height:1}}
.chip .k{{opacity:.65}}
#pick{{position:relative;flex:1;min-width:220px}}
#bar{{width:100%;box-sizing:border-box}}
#sugg{{position:absolute;top:100%;left:0;right:0;background:#fff;border:1px solid #ccc;border-radius:6px;max-height:260px;overflow:auto;display:none;z-index:10;box-shadow:0 4px 12px rgba(0,0,0,.15)}}
#sugg .g{{background:#f0f0f0;font-size:11px;text-transform:uppercase;padding:4px 8px;color:#666;position:sticky;top:0}}
#sugg .o{{padding:6px 8px;cursor:pointer;font-size:14px}}
#sugg .o:hover{{background:#e8f0ff}}
tr.hit td{{background:#fffbe8}}
tr.del td{{opacity:.6;text-decoration:line-through}}
.dhead td{{background:#eef;font-weight:600;cursor:pointer}}
.dhead td:first-child{{white-space:nowrap}}
.badge{{font-size:11px;background:#ffd;border:1px solid #cc9;border-radius:4px;padding:1px 5px;margin-left:6px}}
.arrow{{display:inline-block;width:1.2em}}
.linkbtn{{background:none;border:none;text-decoration:underline;cursor:pointer;font-size:13px;padding:8px 4px}}
</style></head><body>
<header><h2 style="margin:0">gitail — search element history</h2>
<div style="opacity:.75;font-size:13px">Static report, no server. One bar, stacked filters — type <code>concrete</code>, pick it, type <code>steel</code>, pick it: only drawings containing <b>both</b> remain. Click a drawing for its <b>PDF</b>.</div></header>
<main>
<div id="chips"></div>
<div class="filters">
<div id="pick"><input id="bar" placeholder="type to stack filters — e.g. concrete, steel, StageC… (Enter adds)" autocomplete="off"><div id="sugg"></div></div>
<label style="align-self:center;font-size:13px"><input type="checkbox" id="ever"> ever (history counts)</label>
<button id="clear" style="background:#fff">Clear</button>
<button id="expall" class="linkbtn">Expand all</button>
<button id="colall" class="linkbtn">Collapse all</button>
</div>
<div class="count" id="count"></div>
<table><thead><tr>
<th>project</th><th>drawing</th><th>element</th><th>material</th><th>value</th><th>text</th><th>status</th><th>commit</th><th>date</th>
</tr></thead><tbody id="body"></tbody></table>
UNINDEXED_SECTION
<div class="card">
<h3 style="margin-top:0">Add a drawing</h3>
<div class="hint">Prefer one-click upload? Run <code>gitail serve</code> in the drawings repo and open <code>http://localhost:8000</code> — files + index update directly. Manual fallback below.</div>
<div class="filters">
<select id="add-proj"><option value="">(no project — drawings/ root)</option>{''.join(f'<option>{p}</option>' for p in projects)}<option value="__new__">+ New project…</option></select>
<input id="add-newproj" placeholder="new project name" style="display:none;min-width:160px">
<input type="file" id="add-file" accept=".dxf,.dwg,.pdf">
</div>
<pre id="add-cmds" class="hint">Select a .dxf file above (.dwg works too — export to DXF first; .pdf alone is view-only).</pre>
</div>

<script>
const ROWS = {payload};
const META = {metajs};
const $=id=>document.getElementById(id);
let CHIPS=[];
function matchRow(r,k,v){{
  v=v.toLowerCase();
  if(k==='material')return (r.material||'').toLowerCase()===v;
  if(k==='project')return (r.project||'').toLowerCase()===v;
  if(k==='drawing')return (r.drawing||'').toLowerCase()===v;
  if(k==='value')return String(r.value??'')===v;
  return (r.text_raw||'').toLowerCase().includes(v);
}}
function drawingHas(rows,k,v){{return rows.some(r=>matchRow(r,k,v));}}
function options(){{
  const o=[];
  META.materials.forEach(v=>o.push({{k:'material',v}}));
  META.projects.forEach(v=>o.push({{k:'project',v}}));
  META.drawings.forEach(v=>o.push({{k:'drawing',v}}));
  (META.values||[]).forEach(v=>o.push({{k:'value',v}}));
  META.texts.forEach(v=>o.push({{k:'text',v}}));
  return o;
}}
function renderChips(){{
  $('chips').innerHTML=CHIPS.map((c,i)=>`<span class="chip"><span class="k">${{esc(c.k)}}</span>${{esc(c.v)}}<button data-i="${{i}}">×</button></span>`).join('')
    +(CHIPS.length?'':'<span class="hint">No filters — showing everything. Type below; each pick stacks (drawings must contain ALL).</span>');
  $('chips').querySelectorAll('button').forEach(b=>b.onclick=()=>{{CHIPS.splice(+b.dataset.i,1);renderChips();render();}});
}}
function suggest(){{
  const t=$('bar').value.toLowerCase().trim(),box=$('sugg');
  if(!t){{box.style.display='none';return;}}
  const hits=options().filter(o=>(o.k+':'+o.v).toLowerCase().includes(t)
    && !CHIPS.some(c=>c.k===o.k&&c.v===o.v)).slice(0,60);
  if(!hits.length){{box.style.display='none';return;}}
  let g='';
  box.innerHTML=hits.map((o,i)=>{{
    const h=o.k!==g?`<div class="g">${{esc(o.k)}}</div>`:'';
    g=o.k;
    return h+`<div class="o" data-i="${{i}}">${{esc(o.k)}}:<b>${{esc(o.v)}}</b></div>`;
  }}).join('');
  box.style.display='block';
  box.querySelectorAll('.o').forEach(el=>el.onclick=()=>{{
    const o=hits[+el.dataset.i];
    CHIPS.push(o);$('bar').value='';box.style.display='none';renderChips();render();
  }});
}}
$('bar').addEventListener('input',suggest);
$('bar').addEventListener('keydown',e=>{{
  if(e.key==='Enter'){{
    const first=$('sugg').querySelector('.o');
    if(first){{first.click();}}
    else if($('bar').value.trim()){{CHIPS.push({{k:'text',v:$('bar').value.trim()}});$('bar').value='';renderChips();render();}}
  }}
  if(e.key==='Escape'){{$('sugg').style.display='none';}}
}});
document.addEventListener('click',e=>{{if(!$('pick').contains(e.target))$('sugg').style.display='none';}});
$('clear').onclick=()=>{{CHIPS=[];renderChips();render();}};
function render(){{
  const ev=$('ever').checked;
  const cur=ROWS.filter(r=>r.is_current);
  const del=ROWS.filter(r=>r.is_deleted_latest);
  const curByD={{}}, delByD={{}};
  cur.forEach(r=>{{(curByD[r.drawing]=curByD[r.drawing]||[]).push(r);}});
  del.forEach(r=>{{(delByD[r.drawing]=delByD[r.drawing]||[]).push(r);}});
  // qualification scope: full history when ever, else current + latest deletions
  const scope=ev?ROWS:cur.concat(del);
  const scopeByD={{}};
  scope.forEach(r=>{{(scopeByD[r.drawing]=scopeByD[r.drawing]||[]).push(r);}});
  const qual=Object.keys(Object.assign({{}},curByD,delByD))
    .filter(d=>CHIPS.every(c=>drawingHas(scopeByD[d]||[],c.k,c.v)));
  qual.sort();
  let htm='',nshow=0,ndel=0;
  qual.forEach(d=>{{
    const all=(curByD[d]||[]).slice().sort((a,b)=>String(a.element_id).localeCompare(String(b.element_id)));
    const vis=CHIPS.length?all.filter(r=>CHIPS.some(c=>matchRow(r,c.k,c.v))):all;
    const dz=(delByD[d]||[]).slice().sort((a,b)=>String(a.element_id).localeCompare(String(b.element_id)));
    const open=EXPANDED.has(d);
    const first=all[0]||dz[0]||{{}};
    const hist=ev&&CHIPS.length&&!vis.length;
    nshow+=open?vis.length:0; ndel+=open?dz.length:0;
    htm+=`<tr class="dhead" data-d="${{esc(d)}}"><td><span class="arrow">${{open?'▼':'▶'}}</span> ${{esc(first.project||'—')}}</td>`
      +`<td><a class="dxf" href="${{esc(first.pdf_rel||('pdf/'+d+'.pdf'))}}">📄 ${{esc(d)}}</a>${{hist?'<span class="badge">via history</span>':''}}</td>`
      +`<td colspan="7">${{vis.length}} of ${{all.length}} shown${{dz.length?` (+${{dz.length}} deleted)`:''}}</td></tr>`;
    if(open)vis.forEach(r=>{{
      htm+=`<tr class="hit"><td></td><td></td><td><code>${{esc(r.element_id)}}</code></td>`
        +`<td>${{esc(r.material||'')}}</td><td>${{esc(r.value??'')}}</td>`
        +`<td>${{esc(r.text_raw||'')}}</td><td>${{esc(r.status||'')}}</td>`
        +`<td><code>${{esc((r.commit_sha||'').slice(0,7))}}</code></td><td>${{esc(r.commit_date||'')}}</td></tr>`;
    }});
    if(open)dz.forEach(r=>{{
      htm+=`<tr class="del"><td></td><td></td><td><code>${{esc(r.element_id)}}</code></td>`
        +`<td>${{esc(r.material||'')}}</td><td>${{esc(r.value??'')}}</td>`
        +`<td>${{esc(r.text_raw||'')}}</td><td>deleted</td>`
        +`<td><code>${{esc((r.commit_sha||'').slice(0,7))}}</code></td><td>${{esc(r.commit_date||'')}}</td></tr>`;
    }});
  }});
  $('count').textContent=qual.length+' drawing(s)'
    +(CHIPS.length?' — must contain ALL '+CHIPS.length+' filter(s). Click a drawing to expand matching rows.':' — click a drawing to expand')
    +(ndel?` (+${{ndel}} deleted)`:'');
  $('body').innerHTML=htm||'<tr><td colspan="9">No drawings contain all stacked filters.</td></tr>';
  $('body').querySelectorAll('tr.dhead').forEach(tr=>tr.onclick=e=>{{
    if(e.target.tagName==='A')return;
    const d=tr.dataset.d;
    EXPANDED.has(d)?EXPANDED.delete(d):EXPANDED.add(d);
    render();
  }});
}}
let EXPANDED=new Set();
$('expall').onclick=()=>{{
  document.querySelectorAll('#body tr.dhead').forEach(tr=>EXPANDED.add(tr.dataset.d));
  render();
}};
$('colall').onclick=()=>{{EXPANDED.clear();render();}};
function esc(s){{return String(s).replace(/[&<>"]/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}}[c]));}}
$('ever').addEventListener('change',render);renderChips();render();
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
    +'   (.dwg? export to ASCII DXF R2018+ first — DWG is never parsed. .pdf alone is view-only.)\\n'
    +'2. C:\\\\AI\\\\python.exe -m gitail.cli extract '+dest+' --state-dir state --config-dir ..\\\\Gitails-Tools\\\\config\\n'
    +'3. C:\\\\AI\\\\python.exe -m gitail.cli render --drawings-dir drawings --pdf-dir pdf'
    +'   (makes pdf/… .pdf so links open the drawing, not code)\\n'
    +'4. git add '+dest+' state\\ pdf\\n   git commit -m "Add '+f.name+(p?(' to '+p):'')+'"';
}}
</script></main></body></html>
"""
    unindexed_html = ""
    if unindexed:
        items = "\n".join(
            f'<li><a class="dxf" href="pdf/{u["drawing"]}.pdf">📄 {u["drawing"]}</a> '
            f'<span class="hint">(PDF only — no DXF state indexed; add a .dxf to make it searchable)</span></li>'
            for u in unindexed)
        unindexed_html = (f'<div class="card"><h3 style="margin-top:0">Drawings with PDF but no indexed elements ({len(unindexed)})</h3>'
                          f'<ul>{items}</ul></div>')
    page = page.replace("UNINDEXED_SECTION", unindexed_html)
    out = Path(out)
    out.write_text(page, encoding="utf-8", newline="\n")
    return out
