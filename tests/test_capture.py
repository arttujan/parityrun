"""`parityrun capture`: the scenario a browsing session becomes, and the proxy in front of a real app."""

import json
import textwrap

import httpx
import yaml

from parityrun import Project, cli
from parityrun.capture import Exchange, Session, build, context_regex, dynamic, is_page

PAGE = '<form action="/save"><input type="hidden" name="_token" value="Tk9x2LmQp8Zr4Vb1"><input name="q"></form>'


def test_random_values_are_told_from_ordinary_ones():
    assert dynamic("ab12cd34") and dynamic("Tk9x2LmQp8Zr4Vb1") and dynamic("123e4567-e89b-12d3-a456-426614174000")
    assert not dynamic("12345678") and not dynamic("hello-world") and not dynamic("a1") and not dynamic("x y 12345")


def test_only_pages_and_forms_become_steps():
    assert is_page(Exchange("GET", "/about", resp_type="text/html"))
    assert is_page(Exchange("POST", "/upload.css"))  # a post is never an asset
    assert not is_page(Exchange("GET", "/app.css?v=3")) and not is_page(Exchange("GET", "/favicon.ico"))
    assert not is_page(Exchange("GET", "/img", resp_type="image/png"))
    assert not is_page(Exchange("GET", "/__parityrun")) and not is_page(Exchange("GET", "/.well-known/x"))
    assert not is_page(Exchange("HEAD", "/"))


def test_a_value_is_found_again_by_its_field_attribute_key_or_text():
    assert context_regex(PAGE, "Tk9x2LmQp8Zr4Vb1", "text/html").startswith("<input[^>]*?\\bname=")
    assert context_regex('<a class="x" href="/items/ab12cd34">', "ab12cd34", "text/html") == \
        'href="/items/([^"\'<>\\s&?#/]+)'
    assert context_regex('{"ok": true, "id": "ab12cd34"}', "ab12cd34") == '"id":\\ "([^"\'<>\\s&?#/]+)'
    assert context_regex("<p>Order 9f8e7d6c5b ready: x1y2z3w4q5</p>", "x1y2z3w4q5") == \
        'Order\\ [^"\'<>\\s]+\\ ready:\\ ([^"\'<>\\s&?#/]+)'  # the other random part is any text


def test_a_session_becomes_a_scenario_that_replays(tmp_path):
    exchanges = [
        Exchange("GET", "/", status=200, resp_type="text/html", resp_text=PAGE),
        Exchange("GET", "/app.css", status=200, resp_type="text/css", resp_text="body{}"),
        Exchange("POST", "/save", "application/x-www-form-urlencoded",
                 b"_token=Tk9x2LmQp8Zr4Vb1&q=milk&tag=a&tag=b&remember=on", status=303, location="/items/ab12cd34"),
        Exchange("GET", "/items/ab12cd34", status=200, resp_type="text/html",
                 resp_text='<a href="/items/ab12cd34/photos/77">photos</a>'),
        Exchange("GET", "/items/ab12cd34/photos/77?sig=s1g2n3a4t5u6re", status=200, resp_type="text/html"),
        Exchange("PUT", "/api/items/ab12cd34", "application/json", b'{"id": "ab12cd34", "n": 2}',
                 {"accept": "application/json"}, 200),
    ]
    keys = ["GET /", "POST /save", "GET /items/{item}", "GET /items/{item}/photos/{photo}", "PUT /api/items/{id}"]
    scenario = build(exchanges, keys, tmp_path, "visit")
    assert scenario["steps"] == [
        {"call": "GET /", "capture": {"_token": scenario["steps"][0]["capture"]["_token"]}},
        {"call": "POST /save", "form": {"_token": "{{_token}}", "q": "milk", "tag": ["a", "b"], "remember": "on"},
         "capture": {"item": {"header": "location", "regex": "/items/([^\"'<>\\s&?#/]+)"}}},
        {"call": "GET /items/{{item}}", "capture": {"photo": 'href="/items/[^"\'<>\\s]+/photos/([^"\'<>\\s&?#/]+)'}},
        {"call": "GET /items/{{item}}/photos/{{photo}}?sig=s1g2n3a4t5u6re"},  # never seen before: typed, kept as is
        {"call": "PUT /api/items/{{item}}", "body": {"id": "{{item}}", "n": 2},
         "headers": {"accept": "application/json"}},
    ]
    assert scenario["description"] == "Captured with parityrun capture: 5 requests, 5 routes."


def test_uploaded_files_become_fixtures(tmp_path):
    body = (b"--XyZ\r\nContent-Disposition: form-data; name=\"title\"\r\n\r\nCat\r\n"
            b"--XyZ\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"cat.png\"\r\n"
            b"Content-Type: image/png\r\n\r\n\x89PNG data\r\n"
            b"--XyZ\r\nContent-Disposition: form-data; name=\"none\"; filename=\"\"\r\n\r\n\r\n--XyZ--\r\n")
    x = Exchange("POST", "/up", "multipart/form-data", body, {"content-type": "multipart/form-data; boundary=XyZ"})
    step = build([x], [], tmp_path, "visit")["steps"][0]
    assert step == {"call": "POST /up", "form": {"title": "Cat"}, "upload": {"photo": "captured/visit/cat.png"}}
    assert (tmp_path / "captured" / "visit" / "cat.png").read_bytes() == b"\x89PNG data"


APP = textwrap.dedent("""\
    import secrets, sys
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    PORT = int(sys.argv[1])

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def _send(self, status, body, kind="text/html", headers=()):
            data = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", kind)
            for k, v in headers:
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/":
                token = secrets.token_hex(16)
                return self._send(200, '<html><body><form method="post" action="/save"><input type="hidden" '
                                       f'name="token" value="{token}"><input name="text"></form></body></html>',
                                  headers=[("Set-Cookie", "s=1; Path=/")])
            if self.path.startswith("/items/"):
                return self._send(200, f"<p>item {self.path[7:]} cookie={self.headers.get('Cookie')}</p>")
            if self.path == "/style.css":
                return self._send(200, "p{}", "text/css")
            return self._send(404, "<p>no</p>")

        def do_POST(self):
            form = self.rfile.read(int(self.headers["Content-Length"])).decode()
            if self.path == "/save" and "token=" in form:
                where = f"http://127.0.0.1:{PORT}/items/{secrets.token_hex(4)}"  # absolute, like many frameworks
                return self._send(303, "", headers=[("Location", where)])
            return self._send(403, "<p>no token</p>")

    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    """)


def test_the_proxy_passes_the_app_through_ticks_routes_off_and_saves_a_replayable_scenario(tmp_path, capsys):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "app.py").write_text(APP, encoding="utf-8")
    root = tmp_path / "proj"
    (root / "golden").mkdir(parents=True)
    (root / "impls.json").write_text(json.dumps({"py": {"cwd": "app", "cmd": ["{python}", "app.py", "{port}"]}}),
                                     encoding="utf-8")
    (root / "golden" / "endpoints.json").write_text(
        '["GET /", "POST /save", "GET /items/{id}", "GET /about"]', encoding="utf-8")
    project = Project(root=root, reference="py")
    session = Session(project, port=0)
    session.start()
    try:
        browser = httpx.Client(base_url=session.url, follow_redirects=True)
        home = browser.get("/")
        assert "parityrun: 1/4 routes visited" in home.text and home.text.endswith("</body></html>")
        assert browser.get("/style.css").text == "p{}"
        token = home.text.split('name="token" value="')[1].split('"')[0]
        item = browser.post("/save", data={"token": token, "text": "hi"})
        assert item.url.host == "127.0.0.1" and item.url.port == session.port  # the redirect stays on the proxy
        assert "cookie=s=1" in item.text  # the browser's cookie reached the app
        assert "3 of 4 routes visited; 1 to go" in browser.get("/__parityrun").text
        assert not session.finished.is_set()
        browser.post("/__parityrun/finish")
        assert session.finished.is_set()
    finally:
        session.stop()
    said = capsys.readouterr().out
    assert "GET / -> 200   GET /  (new)   [1/4 routes]" in said and "style.css" not in said
    path = session.save("browse")
    steps = yaml.safe_load(path.read_text(encoding="utf-8"))["steps"]
    assert [s["call"] for s in steps] == ["GET /", "POST /save", "GET /items/{{id}}"]
    assert steps[1]["form"] == {"token": "{{token}}", "text": "hi"}
    assert cli.record(project, ["browse"], force=False) == 0  # replays on a fresh app, twice the same
    recording = json.loads((root / "golden" / "browse" / "recording.json").read_text(encoding="utf-8"))
    assert recording["steps"][2]["text"] == "<p>item <id> cookie=s=1</p>"
