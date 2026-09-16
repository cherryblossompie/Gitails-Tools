"""serve: live search reads the DB; uploads land directly (no manual commands)."""
import io
import json
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

import ezdxf

from gitail.serve import Handler, _ingest_dxf
from http.server import ThreadingHTTPServer


def _dxf_bytes(text="3mm GLASS"):
    import tempfile
    doc = ezdxf.new("R2018")
    msp = doc.modelspace()
    msp.add_mtext(text, dxfattribs={"layer": "A-DETL-ANNO"}).dxf.insert = (0, 0)
    with tempfile.NamedTemporaryFile(suffix=".dxf", delete=False) as f:
        doc.saveas(f.name)
        return Path(f.name).read_bytes()


def _start(ctx):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.gitail_ctx = ctx
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


def _get(base, path):
    import urllib.error
    try:
        with urllib.request.urlopen(base + path) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as ex:
        return ex.code, ex.read()


def _post_multipart(base, project, filename, data):
    boundary = "BOUNDARY123"
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"project\"\r\n\r\n{project}\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
            f"Content-Type: application/octet-stream\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(base + "/api/upload", data=body,
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as ex:
        return ex.code, ex.read()


def test_serve_search_and_upload(tmp_path):
    import subprocess
    repo = tmp_path / "repo"
    (repo / "drawings").mkdir(parents=True)
    for d in ("state", "pdf"):
        (repo / d).mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-b", "main"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    (repo / "x.txt").write_text("x")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True)
    ctx = {"repo": str(repo), "db": str(repo / "index.sqlite"),
           "drawings_dir": str(repo / "drawings"), "state_dir": str(repo / "state"),
           "pdf_dir": str(repo / "pdf"), "config_dir": str(Path(__file__).parent.parent / "config")}
    srv = _start(ctx)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        code, body = _get(base, "/api/meta")
        assert code == 200
        meta = json.loads(body)
        assert meta["git"] is True and Path(meta["repo"]) == repo
        # upload a DXF into project StageC — no manual commands
        code, body = _post_multipart(base, "StageC", "D-9.dxf", _dxf_bytes("6mm GLASS"))
        assert code == 200, body[:300]
        resp = json.loads(body)
        assert resp["drawing"] == "StageC/D-9"
        assert resp["committed"] is True
        assert resp["is_new_drawing"] is True and resp["revision"] == 1
        assert (repo / "drawings" / "StageC" / "D-9.dxf").exists()
        assert (repo / "state" / "StageC" / "D-9.jsonl").exists()
        assert (repo / "pdf" / "StageC" / "D-9.pdf").exists()
        # live DB search finds it immediately
        with urllib.request.urlopen(base + "/api/search?material=glass&ever=1") as r:
            legacy = json.loads(r.read())
        assert any(x["drawing"] == "StageC/D-9" for x in legacy["rows"])
        # re-upload = iteration 2 of the same drawing
        code, body = _post_multipart(base, "StageC", "D-9.dxf", _dxf_bytes("12mm GLASS"))
        assert code == 200, body[:300]
        resp2 = json.loads(body)
        assert resp2["is_new_drawing"] is False and resp2["revision"] == 2
        assert "iteration 2" in resp2["iteration_note"]
        with urllib.request.urlopen(base + "/api/drawing_history?drawing=" +
                                    urllib.parse.quote("StageC/D-9")) as r:
            revs = json.loads(r.read())
        assert [x["revision"] for x in revs] == [1, 2]
        assert revs[1]["changed"] >= 1
        # stacked chip mode: drawing must contain EACH chip
        chip_q = urllib.parse.urlencode([("chip", "material:glass"), ("ever", "1")])
        with urllib.request.urlopen(base + "/api/search?" + chip_q) as r:
            stacked = json.loads(r.read())
        assert [d["drawing"] for d in stacked["drawings"]] == ["StageC/D-9"]
        assert all(x["matched"] for x in stacked["rows"])
        code, _ = _get(base, "/pdf/StageC/D-9.pdf")
        assert code == 200
        # archived blobs: old PDF/DXF per revision, straight from git
        rev1 = revs[0]["commit_sha"]
        code, body = _get(base, "/api/blob?sha=" + rev1[:12] +
                          "&path=" + urllib.parse.quote("pdf/StageC/D-9.pdf"))
        assert code == 200 and body.startswith(b"%PDF")
        code, body = _get(base, "/api/blob?sha=" + rev1[:12] +
                          "&path=" + urllib.parse.quote("drawings/StageC/D-9.dxf"))
        assert code == 200 and b"SECTION" in body
        code, _ = _get(base, "/api/blob?sha=zzzz&path=" + urllib.parse.quote("pdf/StageC/D-9.pdf"))
        assert code == 400
        code, _ = _get(base, "/api/blob?sha=" + rev1[:12] + "&path=" + urllib.parse.quote("../x.txt"))
        assert code in (400, 403)
        code, _ = _get(base, "/api/blob?sha=" + rev1[:12] + "&path=" + urllib.parse.quote("pdf/StageC/nope.pdf"))
        assert code == 404
    finally:
        srv.shutdown()


def test_serve_files_with_spaces_and_traversal(tmp_path):
    repo = tmp_path / "repo"
    (repo / "pdf" / "test").mkdir(parents=True)
    (repo / "pdf" / "test" / "test 01.pdf").write_bytes(b"%PDF-1.4 fake")
    ctx = {"repo": str(repo), "db": str(repo / "index.sqlite"),
           "drawings_dir": str(repo / "drawings"), "state_dir": str(repo / "state"),
           "pdf_dir": str(repo / "pdf"), "config_dir": str(Path(__file__).parent.parent / "config")}
    srv = _start(ctx)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        # browser-encoded space must resolve
        code, body = _get(base, "/pdf/test/test%2001.pdf")
        assert code == 200 and body == b"%PDF-1.4 fake"
        # path traversal must not escape
        code, _ = _get(base, "/pdf/..%2f..%2fsecret.txt")
        assert code in (403, 404)
    finally:
        srv.shutdown()


def test_serve_upload_outside_repo_explains_itself(tmp_path):
    plain = tmp_path / "notarepo"
    (plain / "drawings").mkdir(parents=True)
    ctx = {"repo": str(plain), "db": str(plain / "index.sqlite"),
           "drawings_dir": str(plain / "drawings"), "state_dir": str(plain / "state"),
           "pdf_dir": str(plain / "pdf"), "config_dir": str(Path(__file__).parent.parent / "config")}
    srv = _start(ctx)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        code, body = _get(base, "/api/meta")
        assert json.loads(body)["git"] is False
        code, body = _post_multipart(base, "", "D-1.dxf", _dxf_bytes())
        assert code == 200
        resp = json.loads(body)
        assert resp["committed"] != True and "not a git repo" in str(resp["committed"])
    finally:
        srv.shutdown()


def test_serve_ingest_counts_deletions(tmp_path):
    import subprocess
    repo = tmp_path / "repo"
    (repo / "drawings").mkdir(parents=True)
    for d in ("state", "pdf"):
        (repo / d).mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-b", "main"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    (repo / "x.txt").write_text("x")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True)
    ctx = {"repo": str(repo), "db": str(repo / "index.sqlite"),
           "drawings_dir": str(repo / "drawings"), "state_dir": str(repo / "state"),
           "pdf_dir": str(repo / "pdf"), "config_dir": str(Path(__file__).parent.parent / "config")}
    srv = _start(ctx)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        code, body = _post_multipart(base, "", "D-1.dxf", _dxf_bytes("3mm GLASS"))
        assert code == 200
        assert json.loads(body)["statuses"].get("deleted") is None
        # re-upload emptied: the old element must be reported, not hidden
        doc = ezdxf.new("R2018")
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".dxf", delete=False) as f:
            doc.saveas(f.name)
            empty = Path(f.name).read_bytes()
        code, body = _post_multipart(base, "", "D-1.dxf", empty)
        assert code == 200, body[:300]
        resp = json.loads(body)
        assert resp["elements"] == 0
        assert resp["statuses"].get("deleted") == 1  # MTEXT tombstoned
        assert "deleted" in resp["iteration_note"]
        with urllib.request.urlopen(base + "/api/search") as r:
            res = json.loads(r.read())
        assert [d["drawing"] for d in res["drawings"]] == ["D-1"]
        assert res["rows"] == [] and len(res["deleted"]) == 1
    finally:
        srv.shutdown()


def _png_bytes():
    # minimal valid 1x1 PNG (no imaging libs needed)
    import base64
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def test_serve_images_view_only(tmp_path):
    import subprocess
    repo = tmp_path / "repo"
    (repo / "drawings").mkdir(parents=True)
    for d in ("state", "pdf", "images"):
        (repo / d).mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-b", "main"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    (repo / "x.txt").write_text("x")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True)
    ctx = {"repo": str(repo), "db": str(repo / "index.sqlite"),
           "drawings_dir": str(repo / "drawings"), "state_dir": str(repo / "state"),
           "pdf_dir": str(repo / "pdf"), "images_dir": str(repo / "images"),
           "config_dir": str(Path(__file__).parent.parent / "config")}
    srv = _start(ctx)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        # orphan image first: no drawing yet
        code, body = _post_multipart(base, "StageC", "site.png", _png_bytes())
        assert code == 200, body[:200]
        resp = json.loads(body)
        assert resp["committed"] is True and "view-only" in resp["note"]
        assert (repo / "images" / "StageC" / "site.png").exists()
        code, body = _get(base, "/images/StageC/site.png")
        assert code == 200 and body == _png_bytes()
        with urllib.request.urlopen(base + "/api/search") as r:
            res = json.loads(r.read())
        assert res["drawings"] == []  # images never parse into elements
        assert [u["drawing"] for u in res["ref_images"]] == ["StageC/site"]
        # now upload the matching DXF: image attaches to the drawing
        code, body = _post_multipart(base, "StageC", "site.dxf", _dxf_bytes("3mm GLASS"))
        assert code == 200, body[:200]
        with urllib.request.urlopen(base + "/api/search") as r:
            res = json.loads(r.read())
        assert [d["drawing"] for d in res["drawings"]] == ["StageC/site"]
        assert res["drawings"][0]["images"] == ["/images/StageC/site.png"]
        assert res["ref_images"] == []
    finally:
        srv.shutdown()
