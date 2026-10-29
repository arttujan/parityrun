"""`parityrun capture`: use the app in a browser; every page you open and form you send becomes a scenario.

The app starts as it does for a run (fresh data, isolated copy), behind a small proxy on http://127.0.0.1:8000. The
proxy passes everything through, ticks off the app's routes (from golden/endpoints.json, or read from the code)
as they are visited, shows the count in a corner of every page and the whole list at /__parityrun, and on Finish
writes scenarios/<name>.yaml: one step per request, form tokens and new ids turned into captures so the scenario
replays on a fresh app, and uploads saved as fixtures. Then the scenario is recorded twice to prove it replays.
"""

from __future__ import annotations

import dataclasses
import email.parser
import email.policy
import html
import http.cookiejar
import json
import pathlib
import re
import shutil
import tempfile
import threading
import urllib.parse
import webbrowser
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import yaml

from . import routes, runner
from .init import ASSET, TOKEN, token_regex
from .project import Project
from .stubs import Stub

HOP = {"host", "connection", "keep-alive", "proxy-connection", "transfer-encoding", "upgrade", "accept-encoding",
       "content-length", "te", "trailer", "content-encoding", "date", "server"}
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
NOT_PAGES = ("text/css", "application/javascript", "text/javascript", "image/", "font/", "application/font")


@dataclasses.dataclass
class Exchange:
    """One request through the proxy and what the app answered."""
    method: str
    path: str
    req_type: str = ""
    req_body: bytes = b""
    req_headers: dict = dataclasses.field(default_factory=dict)
    status: int = 0
    location: str = ""
    resp_type: str = ""
    resp_text: str = ""


def dynamic(value: str) -> bool:
    """A value that is different on every run of the app: a token or a random id (letters and digits, 8+ long)."""
    return bool(UUID.fullmatch(value)) or (len(value) >= 8 and re.fullmatch(r"[\w\-.~+/=]+", value) is not None
                                           and re.search(r"\d", value) is not None
                                           and re.search(r"[A-Za-z]", value) is not None)


def is_page(x: Exchange) -> bool:
    """A request the scenario keeps: not the browser fetching a stylesheet, a picture or its own files."""
    if x.method not in routes.METHODS or x.path.startswith(("/__parityrun", "/.well-known/")):
        return False
    path = x.path.split("?")[0]
    return not (x.method == "GET" and (ASSET.search(path) or path == "/favicon.ico"
                                       or x.resp_type.startswith(NOT_PAGES)))


class _Inputs(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.inputs: list[tuple[str, str]] = []

    def handle_starttag(self, tag, attrs):
        a = {k: v or "" for k, v in attrs}
        if tag == "input" and a.get("name"):
            self.inputs.append((a["name"], a.get("value", "")))


def context_regex(text: str, value: str, kind: str = "") -> str:
    """A regex whose group finds `value` again in a fresh run's `text`: by the form field holding it, or by what
    stands right before it (an attribute, a JSON key, the text since the last tag), other random parts wildcarded."""
    if "html" in kind:
        page = _Inputs()
        page.feed(text)
        name = next((n for n, v in page.inputs if v == value), None)
        if name:
            return token_regex(name)
    at = text.find(value)
    before = text[max(0, at - 80):at]
    attribute = re.search(r"[\w:-]+\s*=\s*[\"'][^\"'<>]*$", before)
    key = re.search(r"\"[\w-]+\"\s*:\s*\"?$", before)
    if attribute:
        before = attribute.group(0)
    elif key:
        before = key.group(0)
    else:
        cut = max(before.rfind("<"), before.rfind(">"), before.rfind("\n"))
        before = before[cut + 1:] if cut >= 0 else before
    prefix, last = "", 0
    for m in re.finditer(r"[\w\-.~+=]{8,}", before):  # other random values before it: any text there
        if dynamic(m.group(0)):
            prefix += re.escape(before[last:m.start()]) + "[^\"'<>\\s]+"
            last = m.end()
    prefix += re.escape(before[last:])
    stop = "\"'<>\\s&?#" + ("" if "/" in value else "/")
    return f"{prefix}([^{stop}]+)"


def _form(x: Exchange, fixtures: pathlib.Path, folder: str) -> tuple[dict, dict]:
    """A form body as its fields and uploaded files (saved under fixtures/<folder>/)."""
    if x.req_type == "application/x-www-form-urlencoded":
        fields: dict = {}
        for k, v in urllib.parse.parse_qsl(x.req_body.decode("utf-8", "replace"), keep_blank_values=True):
            fields[k] = [*fields[k], v] if isinstance(fields.get(k), list) else [fields[k], v] if k in fields else v
        return fields, {}
    message = email.parser.BytesParser(policy=email.policy.default).parsebytes(
        f"Content-Type: {x.req_headers.get('content-type', '')}\r\n\r\n".encode() + x.req_body)
    fields, uploads = {}, {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        if part.get_filename() is not None:
            if not part.get_filename():  # a file input left empty
                continue
            target = fixtures / folder / pathlib.Path(part.get_filename()).name
            k = 2
            while target.exists():
                target = target.with_name(f"{target.stem}-{k}{target.suffix}")
                k += 1
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(part.get_payload(decode=True) or b"")
            uploads[name] = target.relative_to(fixtures).as_posix()
        else:
            fields[name] = part.get_content() if part.get_content_type().startswith("text/") else \
                (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
    return fields, uploads


def build(exchanges: list[Exchange], keys: list[str], fixtures: pathlib.Path, name: str) -> dict:
    """The scenario a browsing session makes (see the module docstring)."""
    kept = [x for x in exchanges if is_page(x)]
    steps: list[dict] = []
    names: dict[str, str] = {}  # value -> its capture name

    def captured(value: str, hint: str, upto: int) -> str | None:
        """The template for a value an earlier answer gave (captured there), or None if none did."""
        if value in names:
            return "{{" + names[value] + "}}"
        for j in range(upto - 1, -1, -1):
            x = kept[j]
            if value in x.location:
                rule = {"header": "location", "regex": context_regex(x.location, value)}
            elif value in x.resp_text:
                rule = context_regex(x.resp_text, value, x.resp_type)
            else:
                continue
            label, k = re.sub(r"\W+", "_", hint) or "value", 2  # the field's own name: _token stays _token
            base = label
            while label in names.values():
                label, k = f"{base}_{k}", k + 1
            steps[j].setdefault("capture", {})[label] = rule
            names[value] = label
            return "{{" + label + "}}"
        return None

    for i, x in enumerate(kept):
        path, _, query = x.path.partition("?")
        route = routes.match(keys, x.method, path)
        shape = route.split(" ", 1)[1].split("/") if route else []
        segments = path.split("/")
        for k, seg in enumerate(segments):
            param = shape[k] if k < len(shape) and shape[k].startswith("{") else None
            if seg and (param or dynamic(seg)):
                hint = param.strip("{}") if param and param != "{param}" else \
                    routes._singular(segments[k - 1]) if k > 1 and segments[k - 1] else "id"
                segments[k] = captured(urllib.parse.unquote(seg), hint, i) or seg
        pairs = []
        for k, v in urllib.parse.parse_qsl(query, keep_blank_values=True):
            t = captured(v, k, i) if dynamic(v) or TOKEN.search(k) else None
            pairs.append(f"{urllib.parse.quote(k)}={t or urllib.parse.quote(v)}")
        step: dict = {"call": f"{x.method} {'/'.join(segments) or '/'}" + (f"?{'&'.join(pairs)}" if pairs else "")}
        if x.req_type in ("application/x-www-form-urlencoded", "multipart/form-data"):
            fields, uploads = _form(x, fixtures, f"captured/{name}")
            for k, v in fields.items():
                if isinstance(v, str) and v and (dynamic(v) or TOKEN.search(k)):
                    fields[k] = captured(v, k, i) or v
            step["form"] = fields
            if uploads:
                step["upload"] = uploads
        elif x.req_type == "application/json" and x.req_body:
            body = json.loads(x.req_body)
            if isinstance(body, dict):
                body = {k: (captured(v, k, i) or v) if isinstance(v, str) and dynamic(v) else v
                        for k, v in body.items()}
            step["body"] = body
        elif x.req_body:
            target = fixtures / "captured" / name / f"body-{i}.bin"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(x.req_body)
            step["raw"] = target.relative_to(fixtures).as_posix()
        if x.req_headers.get("x-requested-with") or x.req_headers.get("accept", "").startswith("application/json"):
            step["headers"] = {k: v for k, v in x.req_headers.items() if k in ("x-requested-with", "accept")}
        steps.append(step)
    visited = sorted({r for x in kept if (r := routes.match(keys, x.method, x.path))})
    return {"description": f"Captured with parityrun capture: {len(steps)} requests, {len(visited)} routes.",
            "steps": steps}


def _blocking_jar() -> http.cookiejar.CookieJar:
    """The proxy keeps no cookies of its own: the browser's are passed through as they are."""
    return http.cookiejar.CookieJar(policy=http.cookiejar.DefaultCookiePolicy(allowed_domains=[]))


class Session:
    """The app and the proxy in front of it, for one capture."""

    def __init__(self, project: Project, impl_name: str | None = None, port: int = 8000) -> None:
        self.project = project
        self.impl = runner.Implementation(project, impl_name or project.reference)
        self.port = port
        self.exchanges: list[Exchange] = []
        self.lock = threading.Lock()
        self.finished = threading.Event()
        self.keys, self.notes = endpoints(project, self.impl)
        self.before = _covered(project, self.keys)
        self.now: set[str] = set()

    def start(self) -> None:
        project = self.project
        self.work = pathlib.Path(tempfile.mkdtemp(prefix=f"{project.work_prefix}capture-"))
        (self.work / "data").mkdir()
        app = self.work / "app" if project.isolate else None
        if app:
            runner.copy_app(self.impl, app)
        self.stub = Stub(project.stub_handler)
        self.proc, self.base = self.impl.start(self.work / "data", self.stub.port, self.work / "service.log", cwd=app)
        self.client = httpx.Client(base_url=self.base, follow_redirects=False, timeout=180, cookies=_blocking_jar())
        session = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def _any(self):
                session.handle(self)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _any  # noqa: N815

        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def done(self) -> set[str]:
        return self.before | self.now

    def handle(self, h: BaseHTTPRequestHandler) -> None:
        body = h.rfile.read(int(h.headers.get("Content-Length") or 0))
        if h.path.startswith("/__parityrun"):
            return self._own_page(h)
        headers = {k: v for k, v in h.headers.items() if k.lower() not in HOP}
        headers["Accept-Encoding"] = "identity"
        try:
            r = self.client.request(h.command, h.path, headers=headers, content=body)
        except httpx.HTTPError as e:
            return self._send(h, 502, "text/plain; charset=utf-8", f"parityrun: the app didn't answer: {e}".encode())
        kind = r.headers.get("content-type", "").split(";")[0].strip()
        x = Exchange(h.command, h.path, h.headers.get("Content-Type", "").split(";")[0].strip(), body,
                     {k.lower(): v for k, v in h.headers.items() if k.lower() in ("content-type", "accept",
                                                                                  "x-requested-with")},
                     r.status_code, r.headers.get("location", "").replace(self.base, ""), kind,
                     r.text if kind.startswith("text/") or "json" in kind or "xml" in kind else "")
        route = routes.match(self.keys, x.method, x.path)
        with self.lock:
            self.exchanges.append(x)
            new = route is not None and route not in self.done
            if route:
                self.now.add(route)
            count = f"{len(self.done & set(self.keys))}/{len(self.keys)}"
        if is_page(x):
            print(f"  {x.method} {x.path} -> {x.status}   {route or 'not a known route'}"
                  f"{'  (new)' if new else ''}   [{count} routes]", flush=True)
        content = r.content
        if kind == "text/html" and h.command != "HEAD":
            content = self._banner(content, count)
        extra = [(k, v.replace(self.base, self.url) if k.lower() == "location" else v)
                 for k, v in r.headers.multi_items() if k.lower() not in HOP]
        self._send(h, r.status_code, None, content, extra)

    def _banner(self, content: bytes, count: str) -> bytes:
        tag = (f'<div style="position:fixed;right:10px;bottom:10px;z-index:2147483647;background:#1f2937;color:#fff;'
               f'font:13px/1.4 system-ui,sans-serif;padding:6px 10px;border-radius:6px;opacity:.9">'
               f'<a href="/__parityrun" style="color:#fff">parityrun: {count} routes visited</a></div>').encode()
        at = content.lower().rfind(b"</body>")
        return content[:at] + tag + content[at:] if at >= 0 else content + tag

    def _send(self, h, status: int, kind: str | None, content: bytes, headers: list | None = None) -> None:
        h.send_response(status)
        for k, v in headers or []:
            h.send_header(k, v)
        if kind:
            h.send_header("Content-Type", kind)
        h.send_header("Content-Length", str(len(content)))
        h.end_headers()
        if h.command != "HEAD":
            h.wfile.write(content)

    def _own_page(self, h) -> None:
        if h.command == "POST" and h.path.startswith("/__parityrun/finish"):
            self.finished.set()
            page = "<h1>Saved</h1><p>parityrun is writing the scenario; see the terminal. You can close this tab.</p>"
        else:
            rows = []
            for key in self.keys:
                method, path = key.split(" ", 1)
                state = "visited now" if key in self.now else "in a scenario" if key in self.before else "not yet"
                link = f'<a href="{html.escape(path)}">{html.escape(path)}</a>' \
                    if method == "GET" and "{" not in path else html.escape(path)
                rows.append(f"<tr class='{state.split()[0]}'><td>{method}</td><td>{link}</td><td>{state}</td></tr>")
            left = len(set(self.keys) - self.done)
            page = (f"<h1>parityrun capture</h1><p>{len(self.keys) - left} of {len(self.keys)} routes visited; "
                    f"{left} to go. Use the site as people do: open the pages, send the forms.</p>"
                    "<form method='post' action='/__parityrun/finish'><button>Finish and save the scenario</button>"
                    f"</form><table>{''.join(rows)}</table>")
        style = ("<style>body{font:15px/1.5 system-ui,sans-serif;max-width:52rem;margin:2rem auto;padding:0 1rem}"
                 "td{padding:2px 12px 2px 0}.not td:last-child{color:#b91c1c}.visited td:last-child,"
                 ".in td:last-child{color:#15803d}</style>")
        self._send(h, 200, "text/html; charset=utf-8",
                   f"<!doctype html><title>parityrun capture</title>{style}{page}".encode())

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.client.close()
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        self.stub.close()
        shutil.rmtree(self.work, ignore_errors=True)

    def save(self, name: str) -> pathlib.Path:
        """Write scenarios/<name>.yaml from what was done."""
        scenario = build(self.exchanges, self.keys, self.project.fixtures, name)
        path = self.project.scenarios / f"{name}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(scenario, sort_keys=False, allow_unicode=True, width=1000), encoding="utf-8")
        return path


def endpoints(project: Project, impl: runner.Implementation) -> tuple[list[str], list[str]]:
    """The routes to visit: golden/endpoints.json, or read from the code now (and saved there)."""
    path = project.golden / "endpoints.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8")), []
    found, notes = routes.scan(impl.folder)
    write_endpoints(project, found)
    return [r.key for r in found], notes


def write_endpoints(project: Project, found: list[routes.Route]) -> pathlib.Path:
    path = project.golden / "endpoints.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([r.key for r in found], indent=1) + "\n", encoding="utf-8")
    return path


def _covered(project: Project, keys: list[str]) -> set[str]:
    """The routes the project's scenarios already call."""
    done = set()
    for name in runner.all_scenarios(project):
        for step in runner.load(project, name)["steps"]:
            if "call" in step:
                method, path = step["call"].split(" ", 1)
                route = routes.match(keys, method, runner.TEMPLATE.sub("X", path))
                if route:
                    done.add(route)
    return done


def free_name(project: Project, name: str | None) -> str:
    if name:
        return name
    k, name = 2, "visit"
    while (project.scenarios / f"{name}.yaml").exists():
        name, k = f"visit-{k}", k + 1
    return name


def capture(project: Project, name: str | None = None, impl_name: str | None = None, port: int = 8000,
            open_browser: bool = True, verify: bool = True) -> int:
    """The command: run the session until Finish (or Ctrl+C), save the scenario, record it to prove it replays."""
    from .cli import record

    name = free_name(project, name)
    session = Session(project, impl_name, port)
    for note in session.notes:
        print(f"note: {note}")
    print(f"{len(session.keys)} routes to visit ({len(session.before & set(session.keys))} in scenarios already). "
          f"Starting the app ...")
    session.start()
    print(f"\nOpen {session.url} and use the app as people do. The list of routes: {session.url}/__parityrun\n"
          f"When you are done, press Finish on that page (or Ctrl+C here).\n")
    if open_browser:
        webbrowser.open(session.url)
    try:
        while not session.finished.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass
    session.stop()
    if not any(is_page(x) for x in session.exchanges):
        print("Nothing was visited; no scenario written.")
        return 1
    path = session.save(name)
    left = sorted(set(session.keys) - session.done)
    print(f"\nWrote {path.relative_to(project.root.parent)}: {sum(is_page(x) for x in session.exchanges)} requests. "
          f"Routes visited: {len(session.done & set(session.keys))}/{len(session.keys)}.")
    for key in left:
        print(f"  not yet: {key}")
    if not verify:
        return 0
    print(f"\nRecording {name} twice to check it replays on a fresh app ...")
    return record(project, [name], force=False)
