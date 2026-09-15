"""serve: live search reads the DB; uploads land directly (no manual commands)."""
import io
import json
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

import ezdxf

from arcdiff.serve import Handler, _ingest_dxf
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
    srv.arcdiff_ctx = ctx
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


def _get(base, path):
    with urllib.request.urlopen(base + path) as r:
        return r.status, r.read()


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
        # upload a DXF into project StageC — no manual commands
        code, body = _post_multipart(base, "StageC", "D-9.dxf", _dxf_bytes("6mm GLASS"))
        assert code == 200, body[:300]
        resp = json.loads(body)
        assert resp["drawing"] == "StageC/D-9"
        assert (repo / "drawings" / "StageC" / "D-9.dxf").exists()
        assert (repo / "state" / "StageC" / "D-9.jsonl").exists()
        assert (repo / "pdf" / "StageC" / "D-9.pdf").exists()
        # live DB search finds it immediately
        with urllib.request.urlopen(base + "/api/search?material=glass&ever=1") as r:
            rows = json.loads(r.read())
        assert any(x["drawing"] == "StageC/D-9" for x in rows)
        code, _ = _get(base, "/pdf/StageC/D-9.pdf")
        assert code == 200
    finally:
        srv.shutdown()
