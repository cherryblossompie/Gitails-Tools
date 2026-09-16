"""The page scripts must parse: crude but effective bracket-balance check with a
JS-aware scanner (strings, comments, template ${} nesting, regex literals).

Regression test for the broken thumbnails line that killed the whole page
(`Uncaught SyntaxError: missing ) after argument list`, dead Upload button).
"""
from pathlib import Path


def scan(text):
    out = []
    n, L = 0, len(text)
    prev_sig = ""

    while n < L:
        c = text[n]
        nxt = text[n + 1] if n + 1 < L else ""
        if c == "/" and nxt == "/":
            while n < L and text[n] != "\n":
                n += 1
        elif c == "/" and nxt == "*":
            n += 2
            while n < L and not (text[n] == "*" and n + 1 < L and text[n + 1] == "/"):
                n += 1
            n += 2
        elif c == "/" and prev_sig in ("", "=", "(", ",", ":", "[", "!", "&", "|", "?", "{", ";", "}", "return"):
            n += 1
            inclass = False
            while n < L:
                if text[n] == "\\":
                    n += 2
                elif text[n] == "[":
                    inclass = True
                    n += 1
                elif text[n] == "]":
                    inclass = False
                    n += 1
                elif text[n] == "/" and not inclass:
                    n += 1
                    break
                else:
                    n += 1
            prev_sig = "x"
        elif c in ('"', "'"):
            q = c
            n += 1
            while n < L and text[n] != q:
                n += 2 if text[n] == "\\" else 1
            n += 1
            prev_sig = "x"
        elif c == "`":
            n += 1
            depth = 0
            while n < L:
                if text[n] == "\\":
                    n += 2
                elif text[n] == "$" and n + 1 < L and text[n + 1] == "{":
                    depth += 1
                    out.append("{")
                    n += 2
                elif text[n] == "}" and depth > 0:
                    depth -= 1
                    out.append("}")
                    n += 1
                elif text[n] == "`" and depth == 0:
                    n += 1
                    break
                else:
                    if depth > 0:
                        if text[n] in ('"', "'"):
                            q = text[n]
                            n += 1
                            while n < L and text[n] != q:
                                n += 2 if text[n] == "\\" else 1
                            n += 1
                        else:
                            if text[n] in "(){}[]":
                                out.append(text[n])
                            n += 1
                    else:
                        n += 1
            prev_sig = "x"
        else:
            if not c.isspace():
                if c in "(){}[]":
                    out.append(c)
                prev_sig = c
            n += 1
    return out


def assert_balanced(name, script):
    stack = []
    pairs = {")": "(", "]": "[", "}": "{"}
    for ch in scan(script):
        if ch in "([{":
            stack.append(ch)
        elif stack and stack[-1] == pairs[ch]:
            stack.pop()
        else:
            raise AssertionError(f"{name}: unbalanced {ch!r} (stack top {stack[-1] if stack else None})")
    assert not stack, f"{name}: unclosed {stack}"


def _scripts_from(html):
    import re
    return re.findall(r"<script>(.*?)</script>", html, flags=re.DOTALL)


def test_serve_page_scripts_parse():
    from gitail.serve import PAGE
    scripts = _scripts_from(PAGE)
    assert scripts, "no inline script found"
    for i, s in enumerate(scripts):
        assert_balanced(f"serve PAGE script {i}", s)


def test_report_page_scripts_parse(tmp_path):
    import sqlite3
    from gitail.report import write_html
    db = tmp_path / "i.sqlite"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE element_state (element_id TEXT, commit_sha TEXT, commit_date TEXT,"
                " author TEXT, commit_message TEXT, drawing TEXT, project TEXT, type TEXT, layer TEXT,"
                " material TEXT, value REAL, unit TEXT, text_raw TEXT, x REAL, y REAL, status TEXT,"
                " match_tier TEXT, match_confidence REAL, PRIMARY KEY (element_id, commit_sha))")
    con.execute("INSERT INTO element_state VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("e_1", "a" * 40, "2026-01-01", "a", "m", "D-1", "", "MTEXT",
                 "A", "concrete", 3.0, "mm", "3mm CONC", 0, 0, "new", "new", 1.0))
    con.commit()
    con.close()
    (tmp_path / "pdf").mkdir()
    html = write_html(db, tmp_path / "r.html", pdf_dir=tmp_path / "pdf").read_text(encoding="utf-8")
    scripts = _scripts_from(html)
    assert scripts, "no inline script found"
    for i, s in enumerate(scripts):
        assert_balanced(f"report script {i}", s)
