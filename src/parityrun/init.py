"""`parityrun init`: set parityrun up in an existing app's folder, with nothing to write by hand.

It tells the stack from the files (PHP and its frameworks, node, Python, Rust), starts the app once in a throwaway
copy to see that it runs and what its home page links to, and writes parityrun/ beside the code: impls.json (how to
start the app), parityrun_project.py (settings, with notes on what it couldn't decide) and scenarios/smoke.yaml
(the home page and the pages it links to, with any form token captured). The app's own files are never changed.
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
import re
import shutil
import tempfile
import urllib.parse
from html.parser import HTMLParser

import httpx
import yaml

from . import routes, runner
from .project import Project, RunInfo
from .stubs import Stub

FOLDER = "parityrun"
DOCROOTS = ("public", "web", "htdocs", "www", "public_html")
TOKEN = re.compile(r"csrf|xsrf|token|nonce|authenticity", re.I)
RISKY = re.compile(r"log-?out|sign-?out|delete|remove|destroy|reset|uninstall|unsubscribe", re.I)
ASSET = re.compile(r"\.(css|js|mjs|map|png|jpe?g|gif|svg|webp|ico|woff2?|ttf|pdf|zip|gz|mp4|mp3)$", re.I)
MAX_LINKS = 10


@dataclasses.dataclass
class Stack:
    """How to start an app and what init found out about it."""
    name: str
    """The implementation's name in impls.json: php, node, python, rust."""
    label: str
    cmd: list[str]
    env: dict[str, str] = dataclasses.field(default_factory=dict)
    """Environment values; {port} is the port parityrun gives the app."""
    databases: list[str] = dataclasses.field(default_factory=list)
    ignore_files: list[str] = dataclasses.field(default_factory=list)
    ready_timeout: float = 90
    notes: list[str] = dataclasses.field(default_factory=list)
    tool: str = ""
    """The program that must be on PATH."""


def _dotenv(app: pathlib.Path) -> dict[str, str]:
    path = app / ".env"
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key and not key.startswith("#"):
            out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def _sqlite_files(app: pathlib.Path) -> list[str]:
    """SQLite files kept in the app's folder (not in vendor/ or node_modules/)."""
    found = []
    for path in sorted(app.rglob("*")):
        rel = path.relative_to(app).as_posix()
        if path.is_file() and path.suffix in (".sqlite", ".sqlite3", ".db") and not re.match(
                r"(vendor|node_modules|\.git|target|parityrun)/", rel):
            found.append(rel)
    return found


def _databases(app: pathlib.Path, stack: Stack) -> None:
    env = _dotenv(app)
    connection = env.get("DB_CONNECTION", "").lower()
    if connection in ("mysql", "mariadb", "pgsql", "postgres", "postgresql"):
        kind = "MySQL" if connection in ("mysql", "mariadb") else "PostgreSQL"
        stack.notes.append(f"The app uses {kind} (.env DB_CONNECTION). Every run must start from the same data and "
                           "the tables must be recorded: fill in setup= and snapshot= below (README: Databases).")
        return
    if connection == "sqlite" and env.get("DB_DATABASE"):
        db = env["DB_DATABASE"]
        if pathlib.Path(db).is_absolute():
            stack.notes.append(f"The SQLite file {db} is outside the app's folder, so runs can't use a fresh copy "
                               "of it: point DB_DATABASE at a path inside the app.")
            return
        stack.databases.append(pathlib.PurePath(db).as_posix())
        return
    found = _sqlite_files(app)
    if found:
        stack.databases.append(found[0])
        if len(found) > 1:
            stack.notes.append(f"More SQLite files than one: recording {found[0]}; the others "
                               f"({', '.join(found[1:])}) are compared as files.")


def _php(app: pathlib.Path) -> Stack | None:
    if not ((app / "composer.json").exists() or any(app.glob("*.php"))
            or any((app / d / "index.php").exists() for d in DOCROOTS)):
        return None
    root = next((d for d in DOCROOTS if (app / d / "index.php").exists()), ".")
    # no router script: PHP's built-in server serves real files and sends every other path to index.php
    stack = Stack("php", "PHP", ["php", "-S", "127.0.0.1:{port}", "-t", root], tool="php")
    if (app / "artisan").exists():
        stack.label = "PHP (Laravel)"
        stack.ignore_files = ["storage/framework/*", "storage/logs/*", "bootstrap/cache/*"]
    elif (app / "bin" / "console").exists() and ((app / "symfony.lock").exists()
                                                 or (app / "config" / "bundles.php").exists()):
        stack.label = "PHP (Symfony)"
        stack.ignore_files = ["var/cache/*", "var/log/*"]
    elif (app / "wp-config.php").exists() or (app / "wp-load.php").exists():
        stack.label = "PHP (WordPress)"
        stack.notes.append("WordPress keeps its data in MySQL: fill in setup= and snapshot= below (README: Databases).")
    if not (app / "vendor").exists() and (app / "composer.json").exists():
        stack.notes.append("vendor/ is missing: run `composer install` in the app's folder first.")
    _databases(app, stack)
    return stack


def _node(app: pathlib.Path) -> Stack | None:
    path = app / "package.json"
    if not path.exists():
        return None
    package = json.loads(path.read_text(encoding="utf-8"))
    start = (package.get("scripts") or {}).get("start", "")
    if re.fullmatch(r"node [\w./-]+(\.[cm]?[jt]s)?", start):
        cmd = ["node", start.split()[1]]
    elif package.get("main"):
        cmd = ["node", package["main"]]
    elif start:
        cmd = ["npm", "start"]
    else:
        cmd = ["node", next((f for f in ("server.js", "app.js", "index.js", "main.js") if (app / f).exists()),
                            "index.js")]
    stack = Stack("node", "Node.js", cmd, env={"PORT": "{port}"}, tool="node")
    stack.notes.append("parityrun gives the app its port as PORT: the app must listen on process.env.PORT.")
    if not (app / "node_modules").exists() and (package.get("dependencies") or {}):
        stack.notes.append("node_modules/ is missing: run `npm install` in the app's folder first.")
    _databases(app, stack)
    return stack


def _python(app: pathlib.Path) -> Stack | None:
    if (app / "manage.py").exists():
        stack = Stack("python", "Python (Django)", ["{python}", "manage.py", "runserver", "127.0.0.1:{port}",
                                                    "--noreload"], tool="")
    else:
        entry = next((f for f in ("app.py", "main.py", "wsgi.py") if (app / f).exists()), None)
        if not entry and not (app / "pyproject.toml").exists() and not (app / "requirements.txt").exists():
            return None
        entry = entry or "app.py"
        text = (app / entry).read_text(encoding="utf-8", errors="replace") if (app / entry).exists() else ""
        if "Flask(" in text:
            stack = Stack("python", "Python (Flask)", ["{python}", "-m", "flask", "--app", entry[:-3], "run",
                                                       "--port", "{port}"])
        elif "FastAPI(" in text:
            stack = Stack("python", "Python (FastAPI)", ["{python}", "-m", "uvicorn", f"{entry[:-3]}:app",
                                                         "--port", "{port}"])
        else:
            stack = Stack("python", "Python", ["{python}", entry], env={"PORT": "{port}"})
            stack.notes.append("parityrun gives the app its port as PORT: the app must listen on it.")
    stack.notes.append("The app runs with the Python parityrun is installed in: its packages must be installed there.")
    _databases(app, stack)
    return stack


def _rust(app: pathlib.Path) -> Stack | None:
    if not (app / "Cargo.toml").exists():
        return None
    stack = Stack("rust", "Rust", ["cargo", "run", "--release", "--quiet"], env={"PORT": "{port}"},
                  ready_timeout=900, tool="cargo", ignore_files=["target/*"])
    stack.notes.append("parityrun gives the app its port as PORT: the app must listen on it. The first run builds it.")
    _databases(app, stack)
    return stack


def detect(app: pathlib.Path) -> Stack:
    """The stack of the app in `app`; SystemExit when there is nothing parityrun knows how to start."""
    for find in (_php, _node, _python, _rust):
        stack = find(app)
        if stack:
            return stack
    raise SystemExit(f"Couldn't tell how to start the app in {app}: no PHP files, package.json, manage.py, app.py or "
                     "Cargo.toml. Write parityrun/impls.json yourself (README: A project).")


class _Page(HTMLParser):
    """The links and the hidden form tokens of a page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self.tokens: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = {k: v or "" for k, v in attrs}
        if tag == "a" and a.get("href"):
            self.links.append(a["href"])
        if tag == "input" and a.get("type", "").lower() == "hidden" and TOKEN.search(a.get("name", "")) \
                and a.get("value") and a["name"] not in self.tokens:
            self.tokens.append(a["name"])


def page_parts(html: str, url: str) -> tuple[list[str], list[str]]:
    """The site's own pages a page links to (at most MAX_LINKS, nothing that signs out or deletes, no files), and
    the names of its hidden token fields."""
    page = _Page()
    page.feed(html)
    here = urllib.parse.urlsplit(url)
    links = []
    for href in page.links:
        to = urllib.parse.urlsplit(urllib.parse.urljoin(url, href))
        path = (to.path or "/") + (f"?{to.query}" if to.query else "")
        if to.scheme not in ("http", "https") or to.netloc != here.netloc or path == (here.path or "/") \
                or path in links or RISKY.search(path) or ASSET.search(to.path):
            continue
        links.append(path)
    return links[:MAX_LINKS], page.tokens


def token_regex(name: str) -> str:
    """A regex whose group is the value of the hidden input `name`, whatever the attribute order."""
    n = re.escape(name)
    return (rf"""<input[^>]*?\bname=["']{n}["'][^>]*?\bvalue=["']([^"']*)["']"""
            rf"""|<input[^>]*?\bvalue=["']([^"']*)["'][^>]*?\bname=["']{n}["']""")


def _env(stack: Stack, run: RunInfo) -> dict:
    return {k: v.replace("{port}", str(run.port)) for k, v in stack.env.items()}


def probe(app: pathlib.Path, stack: Stack) -> dict:
    """Start the app once in a throwaway copy and read its home page: {status, ready, links, tokens} or {error}."""
    work = pathlib.Path(tempfile.mkdtemp(prefix="parityrun-init-"))
    try:
        (work / "impls.json").write_text(json.dumps({stack.name: {"cmd": stack.cmd}}), encoding="utf-8")
        project = Project(root=work, repo=app, reference=stack.name, env=lambda run: _env(stack, run), isolate=True,
                          ready_timeout=stack.ready_timeout, isolate_skip=[".git", ".hg", ".svn", FOLDER])
        impl = runner.Implementation(project, stack.name)
        (work / "data").mkdir()
        runner.copy_app(impl, work / "app")
        stub = Stub()
        try:
            proc, base = impl.start(work / "data", stub.port, work / "service.log", cwd=work / "app",
                                    accept=lambda status: status < 500)
        except RuntimeError:
            log = (work / "service.log").read_text(encoding="utf-8", errors="replace")[-2000:]
            stub.close()
            return {"error": f"the app didn't start ({' '.join(stack.cmd)}); its output:\n{log}"}
        try:
            r = httpx.get(f"{base}/", follow_redirects=True, timeout=60)
            links, tokens = page_parts(r.text, str(r.url)) if "html" in r.headers.get("content-type", "") else ([], [])
            final = r.url.path or "/"
            return {"status": r.status_code, "ready": final if r.status_code == 200 else None, "links": links,
                    "tokens": tokens, "redirected": bool(r.history)}
        except httpx.HTTPError as e:
            return {"error": f"the app started but its home page failed: {e}"}
        finally:
            proc.kill()
            proc.wait()
            stub.close()
    finally:
        shutil.rmtree(work, ignore_errors=True)


def smoke(found: dict) -> dict:
    """The first scenario: the home page (tokens captured) and the pages it links to."""
    first = {"call": "GET /", "follow": True}
    if found.get("tokens"):
        first["capture"] = {name: token_regex(name) for name in found["tokens"]}
    return {"description": "Made by parityrun init: the home page and the pages it links to. Add what people do on "
                           "the site (forms, signing in) as steps here or as new scenarios.",
            "steps": [first] + [{"call": f"GET {link}", "follow": True} for link in found.get("links", [])]}


def project_file(stack: Stack, ready: str) -> str:
    q = lambda value: json.dumps(value, ensure_ascii=False)  # noqa: E731 - a JSON string or list is Python too
    values = {"PORT": "{port}", **stack.env}  # every implementation, the ports added later too, gets its port
    env = ", ".join(f"{q(k)}: " + ("str(run.port)" if v == "{port}" else
                                   f"{q(v)}.replace(\"{{port}}\", str(run.port))" if "{port}" in v else q(v))
                    for k, v in values.items())
    notes = "".join(f"# {'-' if i == 0 else ' '} {line}\n" for note in stack.notes
                    for i, line in enumerate(_wrap(note)))
    settings = [f"    reference={q(stack.name)},", '    quick=["smoke"],', "    env=env,", f"    ready={q(ready)},",
                f"    ready_timeout={stack.ready_timeout:g},", "    html=True,", "    redirects=True,", "    isolate=True,"]
    if stack.databases:
        settings.append(f"    databases={q(stack.databases)},")
    if stack.ignore_files:
        settings.append(f"    ignore_files={q(stack.ignore_files)},")
    if stack.name == "php":
        settings.append("    coverage=True,")
    return (f'"""parityrun settings for this app ({stack.label}), written by `parityrun init`.\n\n'
            "  parityrun record                 record what the app does now (golden/)\n"
            "  parityrun check --impl NAME      check another version against it (add NAME to impls.json)\n"
            '"""\n\nimport pathlib\n\nfrom parityrun import Project, RunInfo\n\n\n'
            "def env(run: RunInfo) -> dict:\n"
            '    """The environment the app starts with, on top of yours."""\n'
            f"    return {{{env}}}\n\n\n"
            "PROJECT = Project(\n    root=pathlib.Path(__file__).parent,\n" + "\n".join(settings) + "\n)\n"
            + (f"\n# Notes from parityrun init:\n{notes}" if notes else ""))


def _wrap(text: str, width: int = 110) -> list[str]:
    lines, line = [], ""
    for word in text.split():
        if line and len(line) + 1 + len(word) > width:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    return lines + [line] if line else lines


def init(app: pathlib.Path, force: bool = False, check_start: bool = True) -> int:
    """Write parityrun/ in the app's folder; 0 when the app started and the smoke scenario is ready to record."""
    app = app.resolve()
    folder = app / FOLDER
    if folder.exists() and not force:
        print(f"{folder} exists already; use --force to write it again.")
        return 1
    stack = detect(app)
    print(f"Found: {stack.label} app in {app}")
    found: dict = {}
    if stack.tool and not shutil.which(stack.tool):
        found = {"error": f"{stack.tool} isn't installed (or not on PATH): install it and run init again."}
    elif check_start:
        print(f"Starting it once to look at it: {' '.join(stack.cmd)}")
        found = probe(app, stack)
    if found.get("status") and found["status"] != 200:
        stack.notes.append(f"The home page answers {found['status']}: set ready= to a path that answers 200.")
    (folder / "scenarios").mkdir(parents=True, exist_ok=True)
    (folder / "impls.json").write_text(json.dumps({stack.name: {"cmd": stack.cmd}}, indent=2) + "\n",
                                       encoding="utf-8")
    (folder / "parityrun_project.py").write_text(project_file(stack, found.get("ready") or "/"), encoding="utf-8")
    (folder / "scenarios" / "smoke.yaml").write_text(
        yaml.safe_dump(smoke(found), sort_keys=False, allow_unicode=True, width=1000), encoding="utf-8")
    (folder / ".gitignore").write_text("out/\n", encoding="utf-8")
    found_routes, route_notes = routes.scan(app, use_framework=check_start)
    stack.notes += route_notes
    (folder / "golden").mkdir(exist_ok=True)
    (folder / "golden" / "endpoints.json").write_text(json.dumps([r.key for r in found_routes], indent=1) + "\n",
                                                      encoding="utf-8")
    print(f"Wrote {folder.relative_to(app.parent)}/: impls.json, parityrun_project.py, scenarios/smoke.yaml, "
          f"golden/endpoints.json ({len(found_routes)} routes found in the code)")
    for note in stack.notes:
        print(f"  note: {note}")
    if "error" in found:
        print(f"\nNot ready yet: {found['error']}")
        return 1
    if check_start:
        pages = len(found.get("links", []))
        tokens = f", form tokens captured: {', '.join(found['tokens'])}" if found.get("tokens") else ""
        print(f"The app runs: home page {found['status']}, {pages} linked page{'s' * (pages != 1)} in the smoke "
              f"scenario{tokens}.")
    print("\nNext:\n  parityrun capture       use the app in your browser: every route you visit is ticked off and\n"
          "                          what you do becomes a scenario\n"
          "  parityrun record        record what the app does now\n"
          "  git add parityrun       keep the settings and recordings with the code")
    return 0
