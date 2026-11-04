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
import sys
import tempfile
import threading
import urllib.parse
import webbrowser
from html.parser import HTMLParser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, cast

import httpx
import yaml

from . import routes, runner
from .init import ASSET, TOKEN, token_regex
from .jsontypes import Data
from .stubs import Stub

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override

if TYPE_CHECKING:
    import subprocess
    from email.message import MIMEPart

    from .jsontypes import JSON
    from .project import Project

HOP = {
    "host",
    "connection",
    "keep-alive",
    "proxy-connection",
    "transfer-encoding",
    "upgrade",
    "accept-encoding",
    "content-length",
    "te",
    "trailer",
    "content-encoding",
    "date",
    "server",
}
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)
NOT_PAGES = ("text/css", "application/javascript", "text/javascript", "image/", "font/", "application/font")
DYNAMIC_LENGTH = 8  # a token or a random id is this long at least


@dataclasses.dataclass
class Exchange:
    """One request through the proxy and what the app answered."""

    method: str
    path: str
    req_type: str = ""
    req_body: bytes = b""
    req_headers: dict[str, str] = dataclasses.field(default_factory=dict[str, str])
    status: int = 0
    location: str = ""
    resp_type: str = ""
    resp_text: str = ""


def dynamic(value: str) -> bool:
    """Tell whether a value is different on every run of the app: a token or a random id (letters and digits, 8+)."""
    return bool(UUID.fullmatch(value)) or (
        len(value) >= DYNAMIC_LENGTH
        and re.fullmatch(r"[\w\-.~+/=]+", value) is not None
        and re.search(r"\d", value) is not None
        and re.search(r"[A-Za-z]", value) is not None
    )


def is_page(x: Exchange) -> bool:
    """Tell whether the scenario keeps a request: not the browser fetching a stylesheet, a picture or its own files."""
    if x.method not in routes.METHODS or x.path.startswith(("/__parityrun", "/.well-known/")):
        return False
    path = x.path.split("?")[0]
    return not (
        x.method == "GET" and (ASSET.search(path) or path == "/favicon.ico" or x.resp_type.startswith(NOT_PAGES))
    )


class _Inputs(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.inputs: list[tuple[str, str]] = []

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v or "" for k, v in attrs}
        if tag == "input" and a.get("name"):
            self.inputs.append((a["name"], a.get("value", "")))


def context_regex(text: str, value: str, kind: str = "") -> str:
    """Return a regex whose group finds `value` again in a fresh run's `text`.

    By the form field holding it, or by what stands right before it (an attribute, a JSON key, the text since the
    last tag), other random parts wildcarded.
    """
    if "html" in kind:
        page = _Inputs()
        page.feed(text)
        name = next((n for n, v in page.inputs if v == value), None)
        if name:
            return token_regex(name)
    at = text.find(value)
    before = text[max(0, at - 80) : at]
    attribute = re.search(r"[\w:-]+\s*=\s*[\"'][^\"'<>]*$", before)
    key = re.search(r"\"[\w-]+\"\s*:\s*\"?$", before)
    if attribute:
        before = attribute.group(0)
    elif key:
        before = key.group(0)
    else:
        cut = max(before.rfind("<"), before.rfind(">"), before.rfind("\n"))
        before = before[cut + 1 :] if cut >= 0 else before
    prefix, last = "", 0
    for m in re.finditer(r"[\w\-.~+=]{8,}", before):  # other random values before it: any text there
        if dynamic(m.group(0)):
            prefix += re.escape(before[last : m.start()]) + "[^\"'<>\\s]+"
            last = m.end()
    prefix += re.escape(before[last:])
    stop = "\"'<>\\s&?#" + ("" if "/" in value else "/")
    return f"{prefix}([^{stop}]+)"


def _urlencoded(body: bytes) -> dict[str, JSON]:
    """Return a urlencoded form's fields; a field sent more than once is a list."""
    fields: dict[str, JSON] = {}
    for k, v in urllib.parse.parse_qsl(body.decode("utf-8", "replace"), keep_blank_values=True):
        if k not in fields:
            fields[k] = v
            continue
        known = fields[k]
        fields[k] = [*known, v] if isinstance(known, list) else [known, v]
    return fields


def _save_upload(target: pathlib.Path, data: bytes) -> pathlib.Path:
    """Save an uploaded file under a name no earlier upload has; return where."""
    k = 2
    while target.exists():
        target = target.with_name(f"{target.stem}-{k}{target.suffix}")
        k += 1
    target.parent.mkdir(parents=True, exist_ok=True)
    _ = target.write_bytes(data)
    return target


def _payload(part: MIMEPart) -> bytes:
    payload = part.get_payload(decode=True)
    return payload if isinstance(payload, bytes) else b""


def _form(x: Exchange, fixtures: pathlib.Path, folder: str) -> tuple[dict[str, JSON], dict[str, str]]:
    """Return a form body as its fields and uploaded files (saved under fixtures/<folder>/)."""
    if x.req_type == "application/x-www-form-urlencoded":
        return _urlencoded(x.req_body), {}
    head = f"Content-Type: {x.req_headers.get('content-type', '')}\r\n\r\n".encode()
    message = email.parser.BytesParser(policy=email.policy.default).parsebytes(head + x.req_body)
    fields: dict[str, JSON] = {}
    uploads: dict[str, str] = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not isinstance(name, str) or not name:
            continue
        filename = part.get_filename()
        if filename is not None:
            if filename:  # not a file input left empty
                target = _save_upload(fixtures / folder / pathlib.Path(filename).name, _payload(part))
                uploads[name] = target.relative_to(fixtures).as_posix()
        elif part.get_content_type().startswith("text/"):
            fields[name] = str(part.get_content())
        else:
            fields[name] = _payload(part).decode("utf-8", "replace")
    return fields, uploads


@dataclasses.dataclass
class _Builder:
    """Makes the scenario of a browsing session (see `build`)."""

    kept: list[Exchange]
    keys: list[str]
    fixtures: pathlib.Path
    name: str
    steps: list[Data] = dataclasses.field(default_factory=list[Data])
    names: dict[str, str] = dataclasses.field(default_factory=dict[str, str])
    """value -> its capture name"""

    def captured(self, value: str, hint: str, upto: int) -> str | None:
        """Return the template for a value an earlier answer gave (captured there), or None if none did."""
        if value in self.names:
            return "{{" + self.names[value] + "}}"
        for j in range(upto - 1, -1, -1):
            x = self.kept[j]
            rule: JSON
            if value in x.location:
                rule = {"header": "location", "regex": context_regex(x.location, value)}
            elif value in x.resp_text:
                rule = context_regex(x.resp_text, value, x.resp_type)
            else:
                continue
            label, k = re.sub(r"\W+", "_", hint) or "value", 2  # the field's own name: _token stays _token
            base = label
            while label in self.names.values():
                label, k = f"{base}_{k}", k + 1
            self.steps[j].setdefault("capture", {})[label] = rule
            self.names[value] = label
            return "{{" + label + "}}"
        return None

    def call(self, i: int, x: Exchange) -> str:
        """Return the step's call, its random path segments and query values captured from earlier answers."""
        path, _, query = x.path.partition("?")
        route = routes.match(self.keys, x.method, path)
        shape = route.split(" ", 1)[1].split("/") if route else []
        segments = path.split("/")
        for k, seg in enumerate(segments):
            param = shape[k] if k < len(shape) and shape[k].startswith("{") else None
            if seg and (param or dynamic(seg)):
                if param and param != "{param}":
                    hint = param.strip("{}")
                elif k > 1 and segments[k - 1]:
                    hint = routes.singular(segments[k - 1])
                else:
                    hint = "id"
                segments[k] = self.captured(urllib.parse.unquote(seg), hint, i) or seg
        pairs: list[str] = []
        for k, v in urllib.parse.parse_qsl(query, keep_blank_values=True):
            t = self.captured(v, k, i) if dynamic(v) or TOKEN.search(k) else None
            pairs.append(f"{urllib.parse.quote(k)}={t or urllib.parse.quote(v)}")
        return f"{x.method} {'/'.join(segments) or '/'}" + (f"?{'&'.join(pairs)}" if pairs else "")

    def body(self, i: int, x: Exchange, step: Data) -> None:
        """Add the request's body to its step: a form, JSON or a file."""
        if x.req_type in ("application/x-www-form-urlencoded", "multipart/form-data"):
            fields, uploads = _form(x, self.fixtures, f"captured/{self.name}")
            for k, v in fields.items():
                if isinstance(v, str) and v and (dynamic(v) or TOKEN.search(k)):
                    fields[k] = self.captured(v, k, i) or v
            step["form"] = fields
            if uploads:
                step["upload"] = uploads
        elif x.req_type == "application/json" and x.req_body:
            body: JSON = json.loads(x.req_body)
            if isinstance(body, dict):
                body = {
                    k: (self.captured(v, k, i) or v) if isinstance(v, str) and dynamic(v) else v
                    for k, v in body.items()
                }
            step["body"] = body
        elif x.req_body:
            target = self.fixtures / "captured" / self.name / f"body-{i}.bin"
            target.parent.mkdir(parents=True, exist_ok=True)
            _ = target.write_bytes(x.req_body)
            step["raw"] = target.relative_to(self.fixtures).as_posix()

    def build(self) -> Data:
        """Return the scenario."""
        for i, x in enumerate(self.kept):
            step: Data = {"call": self.call(i, x)}
            self.body(i, x, step)
            if x.req_headers.get("x-requested-with") or x.req_headers.get("accept", "").startswith("application/json"):
                step["headers"] = {k: v for k, v in x.req_headers.items() if k in ("x-requested-with", "accept")}
            self.steps.append(step)
        visited = sorted({r for x in self.kept if (r := routes.match(self.keys, x.method, x.path))})
        return {
            "description": f"Captured with parityrun capture: {len(self.steps)} requests, {len(visited)} routes.",
            "steps": self.steps,
        }


def build(exchanges: list[Exchange], keys: list[str], fixtures: pathlib.Path, name: str) -> Data:
    """Return the scenario a browsing session makes (see the module docstring)."""
    return _Builder([x for x in exchanges if is_page(x)], keys, fixtures, name).build()


def _blocking_jar() -> http.cookiejar.CookieJar:
    """Return a cookie jar that keeps nothing: the browser's cookies are passed through as they are."""
    return http.cookiejar.CookieJar(policy=http.cookiejar.DefaultCookiePolicy(allowed_domains=[]))


class _ProxyServer(ThreadingHTTPServer):
    def __init__(self, port: int, session: Session) -> None:
        super().__init__(("127.0.0.1", port), _ProxyHandler)
        self.session = session


class _ProxyHandler(BaseHTTPRequestHandler):
    def _any(self) -> None:
        cast("_ProxyServer", self.server).session.handle(self)

    @override
    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        """Pass the request to the app."""
        self._any()

    def do_POST(self) -> None:
        """Pass the request to the app."""
        self._any()

    def do_PUT(self) -> None:
        """Pass the request to the app."""
        self._any()

    def do_PATCH(self) -> None:
        """Pass the request to the app."""
        self._any()

    def do_DELETE(self) -> None:
        """Pass the request to the app."""
        self._any()

    def do_HEAD(self) -> None:
        """Pass the request to the app."""
        self._any()

    def do_OPTIONS(self) -> None:
        """Pass the request to the app."""
        self._any()


@dataclasses.dataclass
class _Live:
    """The running app, its stub and the proxy in front of it."""

    work: pathlib.Path
    stub: Stub
    proc: subprocess.Popen[bytes]
    base: str
    client: httpx.Client
    server: _ProxyServer


def _send(
    h: BaseHTTPRequestHandler, status: int, kind: str | None, content: bytes, headers: list[tuple[str, str]]
) -> None:
    h.send_response(status)
    for k, v in headers:
        h.send_header(k, v)
    if kind:
        h.send_header("Content-Type", kind)
    h.send_header("Content-Length", str(len(content)))
    h.end_headers()
    if h.command != "HEAD":
        _ = h.wfile.write(content)


def _banner(content: bytes, count: str) -> bytes:
    """Return a page with the count of visited routes in a corner."""
    tag = (
        f'<div style="position:fixed;right:10px;bottom:10px;z-index:2147483647;background:#1f2937;color:#fff;'
        f'font:13px/1.4 system-ui,sans-serif;padding:6px 10px;border-radius:6px;opacity:.9">'
        f'<a href="/__parityrun" style="color:#fff">parityrun: {count} routes visited</a></div>'
    ).encode()
    at = content.lower().rfind(b"</body>")
    return content[:at] + tag + content[at:] if at >= 0 else content + tag


class Session:
    """The app and the proxy in front of it, for one capture."""

    def __init__(self, project: Project, impl_name: str | None = None, port: int = 8000) -> None:
        """Get the routes to visit; `start` starts the app and the proxy."""
        super().__init__()
        self.project = project
        self.impl = runner.Implementation(project, impl_name or project.reference)
        self.port = port
        self.exchanges: list[Exchange] = []
        self.lock = threading.Lock()
        self.finished = threading.Event()
        self.keys, self.notes = endpoints(project, self.impl)
        self.before = covered_routes(project, self.keys)
        self.now: set[str] = set()
        self._live: _Live | None = None

    @property
    def live(self) -> _Live:
        """The running app and proxy."""
        if self._live is None:
            msg = "the capture session hasn't started"
            raise RuntimeError(msg)
        return self._live

    def start(self) -> None:
        """Start the app (fresh data, isolated copy) and the proxy in front of it."""
        project = self.project
        work = pathlib.Path(tempfile.mkdtemp(prefix=f"{project.work_prefix}capture-"))
        (work / "data").mkdir()
        app = work / "app" if project.isolate else None
        if app:
            _ = runner.copy_app(self.impl, app)
        stub = Stub(project.stub_handler)
        proc, base = self.impl.start(work / "data", stub.port, cwd=app)
        client = httpx.Client(base_url=base, follow_redirects=False, timeout=180, cookies=_blocking_jar())
        server = _ProxyServer(self.port, self)
        self.port = server.server_port
        self._live = _Live(work, stub, proc, base, client, server)
        threading.Thread(target=server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        """The proxy's address."""
        return f"http://127.0.0.1:{self.port}"

    @property
    def done(self) -> set[str]:
        """The routes visited, in scenarios already or now."""
        return self.before | self.now

    def handle(self, h: BaseHTTPRequestHandler) -> None:
        """Pass a request to the app, note it and answer with the app's answer (a page with the count added)."""
        body = h.rfile.read(int(h.headers.get("Content-Length") or 0))
        if h.path.startswith("/__parityrun"):
            return self._own_page(h)
        live = self.live
        headers = {k: v for k, v in h.headers.items() if k.lower() not in HOP}
        headers["Accept-Encoding"] = "identity"
        try:
            r = live.client.request(h.command, h.path, headers=headers, content=body)
        except httpx.HTTPError as e:
            message = f"parityrun: the app didn't answer: {e}".encode()
            return _send(h, HTTPStatus.BAD_GATEWAY, "text/plain; charset=utf-8", message, [])
        kind = r.headers.get("content-type", "").split(";")[0].strip()
        x = Exchange(
            h.command,
            h.path,
            h.headers.get("Content-Type", "").split(";")[0].strip(),
            body,
            {k.lower(): v for k, v in h.headers.items() if k.lower() in ("content-type", "accept", "x-requested-with")},
            r.status_code,
            r.headers.get("location", "").replace(live.base, ""),
            kind,
            r.text if kind.startswith("text/") or "json" in kind or "xml" in kind else "",
        )
        count = self._note(x)
        content = r.content
        if kind == "text/html" and h.command != "HEAD":
            content = _banner(content, count)
        extra = [
            (k, v.replace(live.base, self.url) if k.lower() == "location" else v)
            for k, v in r.headers.multi_items()
            if k.lower() not in HOP
        ]
        _send(h, r.status_code, None, content, extra)
        return None

    def _note(self, x: Exchange) -> str:
        """Keep an exchange, tick off its route and print it; return the count of routes visited."""
        route = routes.match(self.keys, x.method, x.path)
        with self.lock:
            self.exchanges.append(x)
            new = route is not None and route not in self.done
            if route:
                self.now.add(route)
            count = f"{len(self.done & set(self.keys))}/{len(self.keys)}"
        if is_page(x):
            print(
                f"  {x.method} {x.path} -> {x.status}   {route or 'not a known route'}"
                f"{'  (new)' if new else ''}   [{count} routes]",
                flush=True,
            )
        return count

    def _own_page(self, h: BaseHTTPRequestHandler) -> None:
        if h.command == "POST" and h.path.startswith("/__parityrun/finish"):
            self.finished.set()
            page = "<h1>Saved</h1><p>parityrun is writing the scenario; see the terminal. You can close this tab.</p>"
        else:
            rows: list[str] = []
            for key in self.keys:
                method, path = key.split(" ", 1)
                state = "visited now" if key in self.now else "in a scenario" if key in self.before else "not yet"
                link = (
                    f'<a href="{html.escape(path)}">{html.escape(path)}</a>'
                    if method == "GET" and "{" not in path
                    else html.escape(path)
                )
                rows.append(
                    f"<tr class='{state.split(maxsplit=1)[0]}'><td>{method}</td><td>{link}</td><td>{state}</td></tr>"
                )
            left = len(set(self.keys) - self.done)
            page = (
                f"<h1>parityrun capture</h1><p>{len(self.keys) - left} of {len(self.keys)} routes visited; "
                f"{left} to go. Use the site as people do: open the pages, send the forms.</p>"
                "<form method='post' action='/__parityrun/finish'><button>Finish and save the scenario</button>"
                f"</form><table>{''.join(rows)}</table>"
            )
        style = (
            "<style>body{font:15px/1.5 system-ui,sans-serif;max-width:52rem;margin:2rem auto;padding:0 1rem}"
            "td{padding:2px 12px 2px 0}.not td:last-child{color:#b91c1c}.visited td:last-child,"
            ".in td:last-child{color:#15803d}</style>"
        )
        document = f"<!doctype html><title>parityrun capture</title>{style}{page}".encode()
        _send(h, HTTPStatus.OK, "text/html; charset=utf-8", document, [])

    def stop(self) -> None:
        """Stop the proxy and the app, and remove the run's folder."""
        live = self.live
        live.server.shutdown()
        live.server.server_close()
        live.client.close()
        if live.proc.poll() is None:
            live.proc.kill()
            _ = live.proc.wait()
        live.stub.close()
        shutil.rmtree(live.work, ignore_errors=True)

    def save(self, name: str) -> pathlib.Path:
        """Write scenarios/<name>.yaml from what was done."""
        scenario = build(self.exchanges, self.keys, self.project.fixtures, name)
        path = self.project.scenarios / f"{name}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        text = yaml.safe_dump(scenario, sort_keys=False, allow_unicode=True, width=1000)
        _ = path.write_text(text, encoding="utf-8")
        return path


def endpoints(project: Project, impl: runner.Implementation) -> tuple[list[str], list[str]]:
    """Return the routes to visit: golden/endpoints.json, or read from the code now (and saved there)."""
    path = project.golden / "endpoints.json"
    if path.exists():
        keys: list[str] = json.loads(path.read_text(encoding="utf-8"))
        return keys, []
    found, notes = routes.scan(impl.folder)
    _ = write_endpoints(project, found)
    return [r.key for r in found], notes


def write_endpoints(project: Project, found: list[routes.Route]) -> pathlib.Path:
    """Save the routes as golden/endpoints.json."""
    path = project.golden / "endpoints.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_text(json.dumps([r.key for r in found], indent=1) + "\n", encoding="utf-8")
    return path


def covered_routes(project: Project, keys: list[str]) -> set[str]:
    """Return the routes the project's scenarios already call."""
    done: set[str] = set()
    for name in runner.all_scenarios(project):
        steps: list[Data] = runner.load(project, name)["steps"]
        for step in steps:
            if "call" in step:
                method, path = step["call"].split(" ", 1)
                route = routes.match(keys, method, runner.TEMPLATE.sub("X", path))
                if route:
                    done.add(route)
    return done


def free_name(project: Project, name: str | None) -> str:
    """Return `name`, or the first visit, visit-2, ... no scenario has."""
    if name:
        return name
    k, name = 2, "visit"
    while (project.scenarios / f"{name}.yaml").exists():
        name, k = f"visit-{k}", k + 1
    return name


def capture(
    project: Project,
    name: str | None = None,
    impl_name: str | None = None,
    port: int = 8000,
    *,
    open_browser: bool = True,
) -> str | None:
    """Run the session until Finish (or Ctrl+C) and save the scenario; return its name (None: nothing visited).

    `parityrun capture` then records it twice to prove it replays.
    """
    name = free_name(project, name)
    session = Session(project, impl_name, port)
    for note in session.notes:
        print(f"note: {note}")
    print(
        f"{len(session.keys)} routes to visit ({len(session.before & set(session.keys))} in scenarios already). "
        f"Starting the app ..."
    )
    session.start()
    print(
        f"\nOpen {session.url} and use the app as people do. The list of routes: {session.url}/__parityrun\n"
        f"When you are done, press Finish on that page (or Ctrl+C here).\n"
    )
    if open_browser:
        _ = webbrowser.open(session.url)
    try:
        while not session.finished.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass
    session.stop()
    if not any(is_page(x) for x in session.exchanges):
        print("Nothing was visited; no scenario written.")
        return None
    path = session.save(name)
    left = sorted(set(session.keys) - session.done)
    print(
        f"\nWrote {path.relative_to(project.root.parent)}: {sum(is_page(x) for x in session.exchanges)} requests. "
        f"Routes visited: {len(session.done & set(session.keys))}/{len(session.keys)}."
    )
    for key in left:
        print(f"  not yet: {key}")
    return name
