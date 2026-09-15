"""Optional live server (stdlib only): search reads index.sqlite directly and
uploads save straight into drawings/<project>/ + extract + render + reindex.

The static report.html stays the CI/brief-compliant surface (file:// friendly).
`arcdiff serve` is the convenience alternative when you want the page itself
to write — no manual copy/extract commands.

Usage (from the DRAWINGS repo root):
    arcdiff serve --repo . --db index.sqlite --port 8000
then open http://localhost:8000
"""
from __future__ import annotations

import cgi
import html
import json
import re
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,48}$")
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,120}\.(dxf|dwg|pdf)$", re.IGNORECASE)


def _ctx(server) -> dict:
    return server.arcdiff_ctx


class Handler(BaseHTTPRequestHandler):
    server_version = "arcdiff-serve/0.1"

    # -- routing ---------------------------------------------------------
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        ctx = _ctx(self.server)
        if path in ("/", "/index.html"):
            return self._page()
        if path == "/api/meta":
            return self._json(_meta(ctx))
        if path == "/api/search":
            return self._json(_search(ctx, query))
        if path == "/api/history":
            return self._json(_history(ctx, query))
        if path.startswith("/pdf/"):
            return self._file(Path(ctx["pdf_dir"]) / path[len("/pdf/"):], "application/pdf")
        if path.startswith("/drawings/"):
            return self._file(Path(ctx["drawings_dir"]) / path[len("/drawings/"):], "image/vnd.dxf")
        return self._text(404, "not found")

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/api/upload":
            return self._text(404, "not found")
        return self._upload()

    # -- handlers --------------------------------------------------------
    def _page(self):
        self._send(200, "text/html; charset=utf-8", PAGE.encode("utf-8"))

    def _json(self, obj):
        self._send(200, "application/json", json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _text(self, code, msg):
        self._send(code, "text/plain; charset=utf-8", msg.encode("utf-8"))

    def _file(self, path: Path, ctype: str):
        try:
            rel = path.resolve().relative_to(Path.cwd().resolve())
        except ValueError:
            try:
                # allow absolute ctx dirs outside cwd
                data = Path(path).read_bytes()
            except OSError:
                return self._text(404, "not found")
            else:
                return self._send(200, ctype, data)
        _ = rel
        if not path.is_file():
            return self._text(404, "not found")
        try:
            return self._send(200, ctype, path.read_bytes())
        except OSError:
            return self._text(500, "read error")

    def _upload(self):
        ctx = _ctx(self.server)
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ctype:
            return self._text(400, "expected multipart/form-data")
        try:
            form = cgi.FieldStorage(fp=self.rfile, headers=self.headers,
                                    environ={"REQUEST_METHOD": "POST",
                                             "CONTENT_TYPE": ctype})
        except Exception as ex:
            return self._text(400, f"bad upload: {ex}")
        project = (form.getvalue("project") or "").strip()
        if project and not PROJECT_RE.match(project):
            return self._text(400, "bad project (letters, numbers, space, _ -)")
        if "file" not in form:
            return self._text(400, "missing file field")
        item = form["file"]
        filename = Path(getattr(item, "filename", "") or "").name
        if not SAFE_NAME_RE.match(filename):
            return self._text(400, "only .dxf / .dwg / .pdf files")
        data = item.file.read() if item.file else b""
        if not data:
            return self._text(400, "empty file")
        ext = filename.rsplit(".", 1)[1].lower()
        drawings_dir = Path(ctx["drawings_dir"])
        dest_dir = drawings_dir / project if project else drawings_dir
        dest_dir.mkdir(parents=True, exist_ok=True)
        if ext == "pdf":
            pdf_dir = Path(ctx["pdf_dir"])
            target_dir = pdf_dir / project if project else pdf_dir
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / filename
            target.write_bytes(data)
            resp = {"ok": True, "saved": str(target), "note": "PDF stored as view-only (no element state)."}
            return self._json(resp)
        target = dest_dir / filename
        target.write_bytes(data)
        if ext == "dwg":
            resp = {"ok": True, "saved": str(target),
                    "note": "DWG saved beside future DXF (never parsed). Export to ASCII DXF R2018+ then upload the .dxf."}
            return self._json(resp)
        # .dxf -> extract + render + reindex, all in-process
        try:
            summary = _ingest_dxf(ctx, target)
        except Exception as ex:
            return self._text(500, f"ingest failed: {ex}")
        summary.update({"ok": True, "saved": str(target)})
        return self._json(summary)

    def _send(self, code, ctype, data: bytes):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):  # quieter logs
        pass


def _meta(ctx) -> dict:
    from .query import distinct
    db = Path(ctx["db"])
    if not db.exists():
        return {"projects": [], "materials": [], "drawings": [], "texts": []}
    try:
        return {"projects": distinct(db, "project"),
                "materials": distinct(db, "material"),
                "drawings": distinct(db, "drawing"),
                "texts": distinct(db, "text_raw")}
    except Exception:
        return {"projects": [], "materials": [], "drawings": [], "texts": []}


def _search(ctx, query) -> list:
    from .query import find
    db = Path(ctx["db"])
    if not db.exists():
        return []
    get = lambda k: (query.get(k) or [""])[0] or None
    val = get("value")
    try:
        rows = find(db, material=get("material"),
                    value=float(val) if val not in (None, "") else None,
                    text=get("text"), drawing=get("drawing"),
                    project=get("project"),
                    ever=(get("ever") or "") in ("1", "true", "on"))
    except Exception:
        rows = []
    for r in rows:
        r["pdf_url"] = f"/pdf/{r['drawing']}.pdf"
        r["dxf_url"] = f"/drawings/{r['drawing']}.dxf"
    return rows[:1000]


def _history(ctx, query) -> list:
    from .query import history
    db = Path(ctx["db"])
    eid = ((query.get("element_id") or [""])[0])
    if not eid or not db.exists():
        return []
    try:
        return history(db, eid)
    except Exception:
        return []


def _ingest_dxf(ctx, dxf: Path) -> dict:
    """extract + render + reindex for one uploaded DXF (no git commit)."""
    import json as _json
    from .extract import extract_state
    from .identity import resolve
    from .index import build_index
    from .render import render_dxf_to_pdf
    from .semantics import load_materials

    drawings_dir = Path(ctx["drawings_dir"])
    state_dir = Path(ctx["state_dir"])
    pdf_dir = Path(ctx["pdf_dir"])
    try:
        drawing = dxf.resolve().relative_to(drawings_dir.resolve()).with_suffix("").as_posix()
    except ValueError:
        drawing = dxf.stem
    cfg_path = Path(ctx["config_dir"]) / "materials.yaml"
    cfg = load_materials(cfg_path) if cfg_path.exists() else {}
    raw = extract_state(dxf, cfg)
    sj, si = state_dir / (drawing + ".jsonl"), state_dir / (drawing + ".idmap.json")
    prev = [_json.loads(l) for l in sj.read_text(encoding="utf-8").splitlines() if l.strip()] if sj.exists() else []
    idmap = _json.loads(si.read_text(encoding="utf-8")) if si.exists() else {"drawing": drawing, "elements": {}}
    resolved, new_idmap, event, fuzzy = resolve(raw, prev, idmap)
    storable = [{k: v for k, v in r.items()
                 if k not in ("status", "match_tier", "match_confidence") and not k.startswith("_")}
                for r in resolved]
    storable.sort(key=lambda r: (r["layer"], r["type"], r["geom"]["x"], r["geom"]["y"], r.get("text_raw") or ""))
    sj.parent.mkdir(parents=True, exist_ok=True)
    sj.write_text("".join(_json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n" for r in storable),
                  encoding="utf-8", newline="\n")
    si.write_text(_json.dumps(new_idmap, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
                  encoding="utf-8", newline="\n")
    pdf_ok = render_dxf_to_pdf(dxf, pdf_dir / (drawing + ".pdf"))
    committed = False
    try:
        import subprocess
        repo_p = Path(ctx["repo"])
        if (repo_p / ".git").exists():
            paths = [str(Path(ctx["drawings_dir"]) / (drawing + ".dxf")),
                     str(sj), str(si), str(pdf_dir / (drawing + ".pdf"))]
            # git add only existing paths, relative to repo for safety
            rel = []
            for p in paths:
                pp = Path(p)
                if pp.exists():
                    try:
                        rel.append(pp.resolve().relative_to(repo_p.resolve()).as_posix())
                    except ValueError:
                        rel.append(p)
            if rel:
                subprocess.run(["git", "-C", str(repo_p), "add", "--", *rel],
                               check=True, capture_output=True)
                subprocess.run(["git", "-C", str(repo_p), "commit", "-m",
                                f"Upload {drawing} via arcdiff serve"],
                               check=True, capture_output=True)
                committed = True
    except Exception:
        committed = False
    idx = build_index(Path(ctx["repo"]), Path(ctx["db"]))
    from collections import Counter
    return {"drawing": drawing, "project": drawing.split("/")[0] if "/" in drawing else "",
            "elements": len(resolved), "statuses": dict(Counter(r.get("status", "?") for r in resolved)),
            "translation": event, "pdf": bool(pdf_ok), "committed": committed,
            "fuzzy": [{"element_id": f["element_id"], "confidence": f["confidence"]} for f in fuzzy],
            "index": idx,
            "next": "review state/ + pdf/ diffs, then: git add drawings state pdf && git commit"}


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>arcdiff — live search + upload</title>
<style>
body{font-family:system-ui,Segoe UI,Arial,sans-serif;margin:0;background:#fafafa;color:#222}
header{background:#111;color:#fff;padding:14px 18px}
main{padding:16px 18px;max-width:1150px}
.filters{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0}
input,select,button{padding:8px;border:1px solid #ccc;border-radius:6px;font-size:14px}
button{background:#111;color:#fff;cursor:pointer}
table{border-collapse:collapse;width:100%;background:#fff;font-size:13px}
th,td{border:1px solid #e3e3e3;padding:6px 8px;text-align:left}
th{background:#f0f0f0;position:sticky;top:0}
.count{color:#666;font-size:13px}
code{background:#eee;padding:1px 4px;border-radius:4px}
.card{background:#fff;border:1px solid #e3e3e3;border-radius:8px;padding:12px;margin:16px 0}
.hint{font-size:13px;color:#555}
a.dxf{font-weight:600}
pre{white-space:pre-wrap}
</style></head><body>
<header><h2 style="margin:0">arcdiff — live search + upload</h2>
<div style="opacity:.75;font-size:13px">Reads <code>index.sqlite</code> directly. Uploads save into <code>drawings/&lt;project&gt;/</code>, then extract + render + reindex automatically.</div></header>
<main>
<div class="card"><h3 style="margin-top:0">Add a drawing</h3>
<div class="filters">
<select id="uproj"><option value="">(no project — drawings/ root)</option></select>
<input id="unew" placeholder="or new project name" style="min-width:160px">
<input type="file" id="ufile" accept=".dxf,.dwg,.pdf">
<button id="ubtn">Upload</button>
</div>
<pre id="umsg" class="hint">Pick a .dxf and Upload — it lands in the repo + search index immediately.</pre>
</div>
<div class="filters">
<select id="proj"><option value="">All projects</option></select>
<input id="mat" list="dl-mat" placeholder="material (e.g. concrete)" style="flex:1;min-width:140px">
<input id="val" placeholder="value (e.g. 3)" style="width:110px">
<input id="q" list="dl-text" placeholder="text (e.g. TOUGHENED)" style="flex:2;min-width:180px">
<input id="drw" list="dl-drw" placeholder="drawing (e.g. D-101)" style="flex:1;min-width:140px">
<label style="align-self:center;font-size:13px"><input type="checkbox" id="ever"> ever</label>
</div>
<datalist id="dl-mat"></datalist><datalist id="dl-drw"></datalist><datalist id="dl-text"></datalist>
<div class="count" id="count"></div>
<table><thead><tr>
<th>project</th><th>drawing (PDF)</th><th>element</th><th>material</th><th>value</th><th>text</th><th>status</th><th>commit</th><th>date</th>
</tr></thead><tbody id="body"></tbody></table>
</main>
<script>
const $=id=>document.getElementById(id);
async function meta(){
  const m=await (await fetch('/api/meta')).json();
  const fill=(id,vals)=>{$(id).innerHTML=vals.map(v=>`<option value="${esc(v)}"></option>`).join('');};
  fill('dl-mat',m.materials||[]);fill('dl-drw',m.drawings||[]);fill('dl-text',(m.texts||[]).slice(0,300));
  $('proj').innerHTML='<option value="">All projects</option>'+(m.projects||[]).map(p=>`<option>${esc(p)}</option>`).join('');
  $('uproj').innerHTML='<option value="">(no project — drawings/ root)</option>'+(m.projects||[]).map(p=>`<option>${esc(p)}</option>`).join('');
}
async function search(){
  const p=new URLSearchParams({project:$('proj').value,material:$('mat').value,value:$('val').value,
    text:$('q').value,drawing:$('drw').value,ever:$('ever').checked?'1':''});
  const rows=await (await fetch('/api/search?'+p)).json();
  $('count').textContent=rows.length+' rows';
  $('body').innerHTML=rows.map(r=>`<tr><td>${esc(r.project||'—')}</td>`
    +`<td><a class="dxf" href="${esc(r.pdf_url)}">📄 ${esc(r.drawing)}</a> <a href="${esc(r.dxf_url)}" style="font-size:11px">dxf</a></td>`
    +`<td><code>${esc(r.element_id)}</code></td><td>${esc(r.material||'')}</td><td>${esc(r.value??'')}</td>`
    +`<td>${esc(r.text_raw||'')}</td><td>${esc(r.status||'')}</td>`
    +`<td><code>${esc((r.commit_sha||'').slice(0,7))}</code></td><td>${esc(r.commit_date||'')}</td></tr>`).join('');
}
function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
['proj','mat','val','q','drw'].forEach(id=>$(id).addEventListener('input',search));
$('ever').addEventListener('change',search);
$('ubtn').addEventListener('click',async()=>{
  const f=$('ufile').files[0];if(!f){$('umsg').textContent='Pick a file first.';return;}
  const fd=new FormData();
  fd.append('project',$('unew').value.trim()||$('uproj').value);
  fd.append('file',f,f.name);
  $('umsg').textContent='Uploading…';
  const r=await fetch('/api/upload',{method:'POST',body:fd});
  const t=await r.text();
  $('umsg').textContent=(r.ok?'OK ':'FAILED '+r.status+' ')+t;
  await meta();await search();
});
meta().then(search);
</script></body></html>
"""


def run(repo=".", db="index.sqlite", drawings_dir="drawings", state_dir="state",
        pdf_dir="pdf", config_dir="config", port=8000):
    ctx = {"repo": str(repo), "db": str(db), "drawings_dir": str(drawings_dir),
           "state_dir": str(state_dir), "pdf_dir": str(pdf_dir), "config_dir": str(config_dir)}
    # build the index on startup so search works immediately
    try:
        from .index import build_index
        if Path(repo, ".git").exists():
            build_index(Path(repo), Path(db))
    except Exception:
        pass
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.arcdiff_ctx = ctx
    print(f"arcdiff serve: http://localhost:{port}  (repo={repo} db={db})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
