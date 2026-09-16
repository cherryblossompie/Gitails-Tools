"""Optional live server (stdlib only): search reads index.sqlite directly and
uploads save straight into drawings/<project>/ + extract + render + reindex.

The static report.html stays the CI/brief-compliant surface (file:// friendly).
`gitail serve` is the convenience alternative when you want the page itself
to write — no manual copy/extract commands.

Usage (from the DRAWINGS repo root):
    gitail serve --repo . --db index.sqlite --port 8000
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
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,120}\.(dxf|dwg|pdf|png|jpg|jpeg)$", re.IGNORECASE)
# Raster images are view-only references (site photos, scanned markups):
# stored and shown, never parsed — pixels are not CAD entities.
IMAGE_EXTS = {"png", "jpg", "jpeg"}
IMAGE_CTYPES = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg"}


def image_urls(images_dir: Path, drawing: str) -> list[str]:
    """View-only images sharing the drawing's path stem: images/<drawing>.png|jpg."""
    out = []
    base = Path(images_dir)
    for ext in ("png", "jpg", "jpeg"):
        if (base / (drawing + "." + ext)).is_file():
            out.append(f"/images/{drawing}.{ext}")
    return out


def unindexed_images(images_dir: Path, drawings_known: set[str]) -> list[dict]:
    """Images with no matching drawing id — listed separately, still viewable."""
    out = []
    base = Path(images_dir)
    if not base.exists():
        return out
    for img in sorted(base.rglob("*")):
        if img.is_file() and img.suffix.lower().lstrip(".") in IMAGE_EXTS:
            try:
                did = img.relative_to(base).with_suffix("").as_posix()
            except ValueError:
                continue
            if did not in drawings_known:
                out.append({"drawing": did,
                            "project": did.split("/")[0] if "/" in did else "",
                            "url": f"/images/{did}{img.suffix.lower()}"})
    return out


def _ctx(server) -> dict:
    return server.gitail_ctx


def _images_dir(ctx) -> Path:
    if ctx.get("images_dir"):
        return Path(ctx["images_dir"])
    return Path(ctx["drawings_dir"]).parent / "images"


class Handler(BaseHTTPRequestHandler):
    server_version = "gitail-serve/0.1"

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
        if path == "/api/drawing_history":
            return self._json(_drawing_history(ctx, query))
        if path == "/api/blob":
            return self._blob(ctx, query)
        if path.startswith("/pdf/"):
            return self._file(Path(ctx["pdf_dir"]), path[len("/pdf/"):], "application/pdf")
        if path.startswith("/drawings/"):
            return self._file(Path(ctx["drawings_dir"]), path[len("/drawings/"):], "image/vnd.dxf")
        if path.startswith("/images/"):
            sub = urllib.parse.unquote(path[len("/images/"):])
            ctype = IMAGE_CTYPES.get(sub.rsplit(".", 1)[-1].lower(), "application/octet-stream")
            return self._file(Path(ctx["images_dir"]), path[len("/images/"):], ctype)
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

    def _file(self, base: Path, sub: str, ctype: str):
        # Browsers percent-encode spaces etc. (/pdf/test/test%2001.pdf) —
        # decode before touching disk, and jail the result inside base.
        base = Path(base).resolve()
        target = (base / urllib.parse.unquote(sub)).resolve()
        try:
            target.relative_to(base)
        except ValueError:
            return self._text(403, "forbidden")
        if not target.is_file():
            return self._text(404, "not found")
        try:
            return self._send(200, ctype, target.read_bytes())
        except OSError:
            return self._text(500, "read error")

    def _blob(self, ctx, query):
        """Archived file at a revision: /api/blob?sha=<sha>&path=pdf/D-101.pdf.

        Lets people open the old version of a drawing from the revisions list.
        Only committed paths under pdf/, drawings/, state/, images/; sha must
        be hex (prefix ok). Never touches the working tree.
        """
        import subprocess
        sha = ((query.get("sha") or [""])[0] or "")
        rel = urllib.parse.unquote((query.get("path") or [""])[0] or "")
        if not re.fullmatch(r"[0-9a-fA-F]{4,40}", sha):
            return self._text(400, "bad sha")
        if not rel or rel.startswith("/") or ".." in Path(rel).parts:
            return self._text(400, "bad path")
        top = rel.split("/")[0]
        if top not in ("pdf", "drawings", "state", "images"):
            return self._text(403, "forbidden")
        low = rel.lower()
        if low.endswith(".pdf"):
            ctype = "application/pdf"
        elif low.endswith(".dxf"):
            ctype = "image/vnd.dxf"
        elif low.endswith((".png",)):
            ctype = "image/png"
        elif low.endswith((".jpg", ".jpeg")):
            ctype = "image/jpeg"
        elif low.endswith((".jsonl", ".json")):
            ctype = "application/json"
        else:
            return self._text(403, "forbidden")
        try:
            full = subprocess.run(["git", "-C", str(ctx["repo"]), "rev-parse", "--verify",
                                   f"{sha}^{{commit}}"], capture_output=True, text=True)
            if full.returncode != 0:
                return self._text(404, "unknown revision")
            blob = subprocess.run(["git", "-C", str(ctx["repo"]), "show",
                                   f"{full.stdout.strip()}:{rel}"],
                                  capture_output=True)
        except Exception as ex:
            return self._text(500, f"git error: {ex}")
        if blob.returncode != 0:
            return self._text(404, "not in that revision (predates it, or never committed)")
        return self._send(200, ctype, blob.stdout)

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
            return self._text(400, "only .dxf / .dwg / .pdf / .png / .jpg files")
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
        if ext in IMAGE_EXTS:
            images_dir = _images_dir(ctx)
            target_dir = images_dir / project if project else images_dir
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / filename
            target.write_bytes(data)
            try:
                import subprocess
                repo_p = Path(ctx["repo"])
                if (repo_p / ".git").exists():
                    rel = target.resolve().relative_to(repo_p.resolve()).as_posix()
                    subprocess.run(["git", "-C", str(repo_p), "add", "--", rel],
                                   check=True, capture_output=True)
                    subprocess.run(["git", "-C", str(repo_p), "commit", "-m",
                                    f"Upload reference image {rel} (view-only)"],
                                   check=True, capture_output=True)
                    committed = True
                else:
                    committed = "not a git repo"
            except Exception as ex:
                committed = f"git error: {ex}"
            resp = {"ok": True, "saved": str(target), "committed": committed,
                    "images": [f"/images/{project + '/' if project else ''}{filename}"],
                    "note": "Image stored as view-only reference (never parsed — pixels are not CAD entities)."}
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
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):  # quieter logs
        pass


def _meta(ctx) -> dict:
    from .query import distinct
    db = Path(ctx["db"])
    meta = {"repo": str(Path(ctx["repo"]).resolve()),
            "git": bool((Path(ctx["repo"]) / ".git").exists()),
            "projects": [], "materials": [], "drawings": [], "texts": [], "values": []}
    if not db.exists():
        return meta
    try:
        meta.update({"projects": distinct(db, "project"),
                     "materials": distinct(db, "material"),
                     "drawings": distinct(db, "drawing"),
                     "texts": distinct(db, "text_raw"),
                     "values": distinct(db, "value")})
    except Exception:
        pass
    return meta


def _search(ctx, query):
    from .query import find, parse_chip, search_stacked
    db = Path(ctx["db"])
    imgdir = _images_dir(ctx)
    if not db.exists():
        # no index yet — still surface view-only reference images
        return {"drawings": [], "rows": [], "deleted": [],
                "ref_images": unindexed_images(imgdir, set())}
    get = lambda k: (query.get(k) or [""])[0] or None
    ever = (get("ever") or "") in ("1", "true", "on")
    raw_chips = query.get("chip") or []
    legacy = {k: get(k) for k in ("material", "value", "text", "drawing", "project")}
    if raw_chips or not any(v not in (None, "") for v in legacy.values()):
        # stacked single-bar mode (also the empty search: whole index incl.
        # deleted-only drawings, so removals never vanish from the list)
        try:
            chips = [parse_chip(c) for c in raw_chips if c.strip()]
        except ValueError:
            return {"drawings": [], "rows": [], "deleted": [], "error": "bad chip"}
        try:
            res = search_stacked(db, chips, ever=ever)
        except Exception:
            return {"drawings": [], "rows": [], "deleted": []}
    else:  # legacy single-filter mode
        val = get("value")
        try:
            rows = find(db, material=get("material"),
                        value=float(val) if val not in (None, "") else None,
                        text=get("text"), drawing=get("drawing"),
                        project=get("project"), ever=ever)
        except Exception:
            rows = []
        for r in rows:
            r["matched"] = True
        by_d = {}
        for r in rows:
            by_d.setdefault(r["drawing"], []).append(r)
        res = {"drawings": [{"drawing": d, "project": v[0].get("project", ""),
                             "elements": len(v), "via_history": False}
                            for d, v in sorted(by_d.items())], "rows": rows}
    for r in res["rows"]:
        r["pdf_url"] = f"/pdf/{r['drawing']}.pdf"
        r["dxf_url"] = f"/drawings/{r['drawing']}.dxf"
    for r in res.get("deleted", []):
        r["pdf_url"] = f"/pdf/{r['drawing']}.pdf"
        r["dxf_url"] = f"/drawings/{r['drawing']}.dxf"
    res["rows"] = res["rows"][:2000]
    res["deleted"] = res.get("deleted", [])[:500]
    # view-only reference images (never parsed): per-drawing thumbs + orphans
    try:
        from .query import distinct as _distinct
        known = set(_distinct(db, "drawing"))
    except Exception:
        known = {d["drawing"] for d in res.get("drawings", [])}
    imgdir = _images_dir(ctx)
    for d in res.get("drawings", []):
        d["images"] = image_urls(imgdir, d["drawing"])
    res["ref_images"] = unindexed_images(imgdir, known)
    return res


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


def _drawing_history(ctx, query) -> list:
    from .query import drawing_history
    db = Path(ctx["db"])
    drawing = ((query.get("drawing") or [""])[0])
    if not drawing or not db.exists():
        return []
    try:
        return drawing_history(db, drawing)
    except Exception:
        return []


def _ingest_dxf(ctx, dxf: Path) -> dict:
    """extract + render + auto-commit + reindex for one uploaded DXF."""
    import json as _json
    from .extract import dxf_version, extract_state, version_supported
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
    warnings: list[str] = []
    ver = dxf_version(dxf)
    if ver and not version_supported(ver):
        warnings.append(f"DXF {ver} < R2018 — re-export as ASCII R2018+ for reliable history")
    raw = extract_state(dxf, cfg)
    if not raw:
        warnings.append("0 extractable entities — check the file has LINE/LWPOLYLINE/ARC/CIRCLE/HATCH/TEXT/MTEXT/DIMENSION/MULTILEADER in modelspace")
    sj, si = state_dir / (drawing + ".jsonl"), state_dir / (drawing + ".idmap.json")
    existed = sj.exists()  # same project+filename before? then this is iteration N+1
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
    committed: bool | str = False
    try:
        import subprocess
        repo_p = Path(ctx["repo"])
        if not (repo_p / ".git").exists():
            committed = f"not a git repo ({repo_p}) — restart serve from the drawings repo root"
        else:
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
            if not rel:
                committed = "nothing to commit (unexpected)"
            else:
                subprocess.run(["git", "-C", str(repo_p), "add", "--", *rel],
                               check=True, capture_output=True, text=True)
                r = subprocess.run(["git", "-C", str(repo_p), "commit", "-m",
                                    f"Upload {drawing} via gitail serve"],
                                   capture_output=True, text=True)
                committed = True if r.returncode == 0 else f"git commit failed: {(r.stderr or r.stdout).strip()[:200]}"
    except Exception as ex:
        committed = f"git error: {ex}"
    idx = build_index(Path(ctx["repo"]), Path(ctx["db"]))
    from collections import Counter
    from .query import drawing_history
    try:
        revs = drawing_history(Path(ctx["db"]), drawing)
    except Exception:
        revs = []
    counts = Counter(r.get("status", "?") for r in resolved)
    prev_ids = {p.get("element_id") for p in prev if p.get("element_id")}
    live_ids = {r["element_id"] for r in resolved}
    n_deleted = len(prev_ids - live_ids)
    if n_deleted:
        counts["deleted"] = n_deleted
    changed_now = sum(1 for r in resolved if r.get("status") not in ("unchanged", "new"))
    if not existed:
        note = "new drawing (revision 1)"
    else:
        bits = []
        if changed_now:
            bits.append(f"{changed_now} changed element(s)")
        if n_deleted:
            bits.append(f"{n_deleted} deleted")
        note = f"iteration {len(revs)} of existing drawing" + (f" — {', '.join(bits)}" if bits else " — no element changes vs previous")
    nxt = ("git push to share" if committed is True
           else f"NOT committed ({committed}); run: git add drawings state pdf && git commit")
    return {"drawing": drawing, "project": drawing.split("/")[0] if "/" in drawing else "",
            "elements": len(resolved), "statuses": dict(counts),
            "revision": len(revs), "is_new_drawing": not existed,
            "iteration_note": note, "warnings": warnings,
            "translation": event, "pdf": bool(pdf_ok), "committed": committed,
            "fuzzy": [{"element_id": f["element_id"], "confidence": f["confidence"]} for f in fuzzy],
            "index": idx,
            "next": nxt}


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>gitail — live search + upload</title>
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
#chips{display:flex;gap:6px;flex-wrap:wrap;margin:8px 0}
.chip{background:#111;color:#fff;border-radius:20px;padding:4px 6px 4px 12px;font-size:13px;display:flex;gap:6px;align-items:center}
.chip button{background:#444;color:#fff;border:none;border-radius:50%;width:20px;height:20px;cursor:pointer;padding:0;line-height:1}
.chip .k{opacity:.65}
#pick{position:relative;flex:1;min-width:220px}
#bar{width:100%;box-sizing:border-box}
#sugg{position:absolute;top:100%;left:0;right:0;background:#fff;border:1px solid #ccc;border-radius:6px;max-height:260px;overflow:auto;display:none;z-index:10;box-shadow:0 4px 12px rgba(0,0,0,.15)}
#sugg .g{background:#f0f0f0;font-size:11px;text-transform:uppercase;padding:4px 8px;color:#666;position:sticky;top:0}
#sugg .o{padding:6px 8px;cursor:pointer;font-size:14px}
#sugg .o.sel,#sugg .o:hover{background:#e8f0ff}
tr.hit td{background:#fffbe8}
tr.del td{opacity:.6;text-decoration:line-through}
.dhead td{background:#eef;font-weight:600;cursor:pointer}
.dhead td:first-child{white-space:nowrap}
.badge{font-size:11px;background:#ffd;border:1px solid #cc9;border-radius:4px;padding:1px 5px;margin-left:6px}
.arrow{display:inline-block;width:1.2em}
.linkbtn{background:none;border:none;color:#111;text-decoration:underline;cursor:pointer;font-size:13px;padding:8px 4px}
.thumbs img{height:64px;border:1px solid #ccc;border-radius:4px;margin:2px;vertical-align:middle;background:#fff}
</style></head><body>
<header><h2 style="margin:0">gitail — live search + upload</h2>
<div style="opacity:.75;font-size:13px">Reads <code>index.sqlite</code> directly. Uploads save into <code>drawings/&lt;project&gt;/</code>, then extract + render + reindex automatically.</div>
<div id="repo" style="opacity:.6;font-size:12px;margin-top:4px"></div></header>
<main>
<div class="card"><h3 style="margin-top:0">Add a drawing</h3>
<div class="filters">
<select id="uproj"><option value="">(no project — drawings/ root)</option></select>
<input id="unew" placeholder="or new project name" style="min-width:160px">
<input type="file" id="ufile" accept=".dxf,.dwg,.pdf,.png,.jpg,.jpeg">
<button id="ubtn">Upload</button>
</div>
<pre id="umsg" class="hint">Pick a .dxf and Upload — it lands in the repo + search index immediately. (PNG/JPG are view-only references, never parsed.)</pre>
</div>
<div id="chips"></div>
<div class="filters">
<div id="pick"><input id="bar" placeholder="type to stack filters — e.g. concrete, steel, StageC… (Enter adds)" autocomplete="off"><div id="sugg"></div></div>
<label style="align-self:center;font-size:13px"><input type="checkbox" id="ever"> ever (history counts)</label>
<button id="clear" style="background:#fff;color:#111">Clear</button>
<button id="expall" class="linkbtn" title="expand all drawings">Expand all</button>
<button id="colall" class="linkbtn" title="collapse all drawings">Collapse all</button>
</div>
<div class="count" id="count"></div>
<table><thead><tr>
<th>project</th><th>drawing (PDF)</th><th>element</th><th>material</th><th>value</th><th>text</th><th>status</th><th>commit</th><th>date</th>
</tr></thead><tbody id="body"></tbody></table>
<div id="refsec"></div>
</main>
<script>
const $=id=>document.getElementById(id);
let META={materials:[],projects:[],drawings:[],texts:[],values:[]},CHIPS=[];
async function meta(){
  const m=await (await fetch('/api/meta')).json();
  META={materials:m.materials||[],projects:m.projects||[],drawings:m.drawings||[],texts:(m.texts||[]).slice(0,300),values:(m.values||[]).map(String)};
  $('uproj').innerHTML='<option value="">(no project — drawings/ root)</option>'+(m.projects||[]).map(p=>`<option>${esc(p)}</option>`).join('');
  $('repo').textContent='repo: '+(m.repo||'?')+(m.git?'':'  ⚠️ NOT A GIT REPO — restart serve from the drawings repo root');
}
function options(){
  const o=[];
  META.materials.forEach(v=>o.push({k:'material',v}));
  META.projects.forEach(v=>o.push({k:'project',v}));
  META.drawings.forEach(v=>o.push({k:'drawing',v}));
  (META.values||[]).forEach(v=>o.push({k:'value',v}));
  META.texts.forEach(v=>o.push({k:'text',v}));
  return o;
}
function renderChips(){
  $('chips').innerHTML=CHIPS.map((c,i)=>`<span class="chip"><span class="k">${esc(c.k)}</span>${esc(c.v)}<button data-i="${i}" title="remove">×</button></span>`).join('')
    +(CHIPS.length?'':'<span class="hint">No filters — showing everything. Type below; each pick stacks (drawings must contain ALL).</span>');
  $('chips').querySelectorAll('button').forEach(b=>b.onclick=()=>{CHIPS.splice(+b.dataset.i,1);renderChips();search();});
}
function suggest(){
  const t=$('bar').value.toLowerCase().trim(),box=$('sugg');
  if(!t){box.style.display='none';return;}
  const hits=options().filter(o=>(o.k+':'+o.v).toLowerCase().includes(t)
    && !CHIPS.some(c=>c.k===o.k&&c.v===o.v)).slice(0,60);
  if(!hits.length){box.style.display='none';return;}
  let g='';
  box.innerHTML=hits.map((o,i)=>{
    const h=o.k!==g?`<div class="g">${esc(o.k)}</div>`:'';
    g=o.k;
    return h+`<div class="o" data-i="${i}">${esc(o.k)}:<b>${esc(o.v)}</b></div>`;
  }).join('');
  box.style.display='block';
  box.querySelectorAll('.o').forEach(el=>el.onclick=()=>{
    const o=hits[+el.dataset.i];
    CHIPS.push(o);$('bar').value='';box.style.display='none';renderChips();search();
  });
}
$('bar').addEventListener('input',suggest);
$('bar').addEventListener('keydown',e=>{
  if(e.key==='Enter'){
    const first=$('sugg').querySelector('.o');
    if(first){first.click();}
    else if($('bar').value.trim()){CHIPS.push({k:'text',v:$('bar').value.trim()});$('bar').value='';renderChips();search();}
  }
  if(e.key==='Escape'){$('sugg').style.display='none';}
});
document.addEventListener('click',e=>{if(!$('pick').contains(e.target))$('sugg').style.display='none';});
$('clear').onclick=()=>{CHIPS=[];renderChips();search();};
async function search(){
  const p=new URLSearchParams({ever:$('ever').checked?'1':''});
  CHIPS.forEach(c=>p.append('chip',c.k+':'+c.v));
  const res=await (await fetch('/api/search?'+p)).json();
  const rows=res.rows||[],drws=res.drawings||[],del=res.deleted||[];
  const shown=rows.filter(r=>r.matched).length;
  const delByD={};
  del.forEach(r=>{(delByD[r.drawing]=delByD[r.drawing]||[]).push(r);});
  $('count').textContent=drws.length+' drawing(s)'
    +(CHIPS.length?`, ${shown} matching row(s) — must contain ALL ${CHIPS.length} filter(s)`:' — click a drawing to expand')
    +(del.length?` (+${del.length} deleted)`:'');
  const byD={};
  rows.forEach(r=>{(byD[r.drawing]=byD[r.drawing]||[]).push(r);});
  let htm='';
  drws.forEach(d=>{
    const all=(byD[d.drawing]||[]).slice().sort((a,b)=>String(a.element_id).localeCompare(String(b.element_id)));
    const vis=all.filter(r=>r.matched);
    const open=EXPANDED.has(d.drawing);
    htm+=`<tr class="dhead" data-d="${esc(d.drawing)}"><td><span class="arrow">${open?'▼':'▶'}</span> ${esc(d.project||'—')}</td>`
      +`<td><a class="dxf" href="/pdf/${esc(d.drawing)}.pdf">📄 ${esc(d.drawing)}</a>${d.via_history?'<span class="badge">via history</span>':''}`
      +` <button class="linkbtn revbtn" data-d="${esc(d.drawing)}">revisions</button></td>`
      +`<td colspan="7">${vis.length} of ${all.length} shown${(delByD[d.drawing]||[]).length?` (+${delByD[d.drawing].length} deleted)`:''}${(d.images||[]).length?` 🖼️${d.images.length}`:''}</td></tr>`;
    htm+=`<tr class="revrow" data-d="${esc(d.drawing)}" style="display:none"><td colspan="9"></td></tr>`;
    if(open){vis.forEach(r=>{htm+=`<tr class="hit"><td></td>`
      +`<td></td><td><code>${esc(r.element_id)}</code></td><td>${esc(r.material||'')}</td><td>${esc(r.value??'')}</td>`
      +`<td>${esc(r.text_raw||'')}</td><td>${esc(r.status||'')}</td>`
      +`<td><code>${esc((r.commit_sha||'').slice(0,7))}</code></td><td>${esc(r.commit_date||'')}</td></tr>`;});
    (delByD[d.drawing]||[]).forEach(r=>{htm+=`<tr class="del"><td></td>`
      +`<td></td><td><code>${esc(r.element_id)}</code></td><td>${esc(r.material||'')}</td><td>${esc(r.value??'')}</td>`
      +`<td>${esc(r.text_raw||'')}</td><td>deleted</td>`
      +`<td><code>${esc((r.commit_sha||'').slice(0,7))}</code></td><td>${esc(r.commit_date||'')}</td></tr>`;});}
    if(open&&(d.images||[]).length){htm+=`<tr><td></td><td colspan="8" class="thumbs">🖼️ reference (view-only): `
      +d.images.map(u=>`<a href="${esc(u)}"><img src="${esc(u)}" loading="lazy"></a>`).join('')+`</td></tr>`;}
  });
  $('body').innerHTML=htm||'<tr><td colspan="9">No drawings contain all stacked filters.</td></tr>';
  const refs=res.ref_images||[];
  $('refsec').innerHTML=refs.length
    ?`<div class="card"><h3 style="margin-top:0">Reference images with no drawing (${refs.length})</h3>`
     +`<div class="thumbs">`+refs.map(u=>`<a href="${esc(u.url)}" title="${esc(u.drawing)}"><img src="${esc(u.url)}" loading="lazy"></a>`).join('')+`</div>`
     +`<div class="hint">View-only — upload the matching .dxf to make them searchable.</div></div>`
    :'';
  $('body').querySelectorAll('tr.dhead').forEach(tr=>tr.onclick=e=>{
    if(e.target.tagName==='A'||e.target.closest('.revbtn'))return;
    const d=tr.dataset.d;
    EXPANDED.has(d)?EXPANDED.delete(d):EXPANDED.add(d);
    search();
  });
  $('body').querySelectorAll('.revbtn').forEach(b=>{b.onclick=async e=>{
    e.stopPropagation();
    const d=b.dataset.d;
    const row=$('body').querySelector(`tr.revrow[data-d="${CSS.escape(d)}"]`);
    if(row.style.display!=='none'){row.style.display='none';return;}
    row.style.display='';
    row.firstElementChild.innerHTML='loading revisions…';
    const revs=await (await fetch('/api/drawing_history?drawing='+encodeURIComponent(d))).json();
    row.firstElementChild.innerHTML=revs.length
      ?revs.map(r=>{const sh=(r.commit_sha||'').slice(0,12);
        const pdf=`/api/blob?sha=${sh}&path=`+encodeURIComponent('pdf/'+d+'.pdf');
        const dxf=`/api/blob?sha=${sh}&path=`+encodeURIComponent('drawings/'+d+'.dxf');
        return `<div>rev ${r.revision} <code>${esc((r.commit_sha||'').slice(0,7))}</code> ${esc((r.commit_date||'').slice(0,10))} — ${r.changed} changed ${esc(JSON.stringify(r.counts))} by ${esc(r.author||'')} — ${esc(r.commit_message||'')} <a href="${pdf}">📄 old PDF</a> <a href="${dxf}" style="font-size:11px">dxf</a></div>`;}).join('')
      :'no revisions indexed';
  }});
}
let EXPANDED=new Set();
$('expall').onclick=async()=>{
  const p=new URLSearchParams({ever:$('ever').checked?'1':''});
  CHIPS.forEach(c=>p.append('chip',c.k+':'+c.v));
  const res=await (await fetch('/api/search?'+p)).json();
  (res.drawings||[]).forEach(d=>EXPANDED.add(d.drawing));
  search();
};
$('colall').onclick=()=>{EXPANDED.clear();search();};
function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
$('ever').addEventListener('change',search);
$('ubtn').addEventListener('click',async()=>{
  const f=$('ufile').files[0];if(!f){$('umsg').textContent='Pick a file first.';return;}
  const fd=new FormData();
  fd.append('project',$('unew').value.trim()||$('uproj').value);
  fd.append('file',f,f.name);
  $('umsg').textContent='Uploading…';
  const r=await fetch('/api/upload',{method:'POST',body:fd});
  const t=await r.text();
  let summary=t;
  try{const j=JSON.parse(t);
    summary=(r.ok?'OK — '+j.iteration_note+' | ':'FAILED ')
      +j.elements+' element(s), pdf: '+(j.pdf?'yes':'FAILED')
      +(j.committed===true?' — in search index':(' — NOT INDEXED: '+j.committed))
      +((j.warnings||[]).length?' | ⚠️ '+j.warnings.join(' | '):'');
  }catch(e){summary=(r.ok?'OK ':'FAILED '+r.status+' ')+t;}
  $('umsg').textContent=summary;
  await meta();renderChips();await search();
});
meta().then(search);
</script></body></html>
"""


def run(repo=".", db="index.sqlite", drawings_dir="drawings", state_dir="state",
        pdf_dir="pdf", images_dir="images", config_dir="config", port=8000):
    # Absolute paths: the server must run from the drawings repo root, and the
    # printed repo line makes a wrong-folder start obvious immediately.
    repo_p = Path(repo).resolve()

    def _abs(p):
        pp = Path(p)
        return str((repo_p / pp).resolve() if not pp.is_absolute() else pp)
    ctx = {"repo": str(repo_p), "db": _abs(db), "drawings_dir": _abs(drawings_dir),
           "state_dir": _abs(state_dir), "pdf_dir": _abs(pdf_dir),
           "images_dir": _abs(images_dir), "config_dir": str(config_dir)}
    print(f"gitail serve: http://localhost:{port}")
    print(f"  repo: {ctx['repo']}")
    if not (repo_p / ".git").exists():
        print("  WARNING: not a git repo — uploads will save but NOT be indexed. "
              "Restart from the drawings repo root.")
    # build the index on startup so search works immediately
    try:
        from .index import build_index
        if (repo_p / ".git").exists():
            print("  index:", build_index(repo_p, Path(ctx["db"])))
    except Exception as ex:
        print(f"  index build failed: {ex}")
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.gitail_ctx = ctx
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
