"""The routes an app has, read from its code, so parityrun can tell which ones nobody has visited yet.

`scan(app)` asks the framework first where it can list its own routes (Laravel `artisan route:list`, Symfony
`debug:router`), in a throwaway copy of the app, and otherwise reads the code: Laravel and Slim route files,
Symfony attributes, plain PHP files, Express-style routers, Flask, FastAPI and Django, axum and actix, and hand-made
front controllers (`$path === '/x'`, `preg_match('#^/x/(\\d+)$#', ...)`, `path.match(/^\\/x$/)`).
Every route is "METHOD /path/{param}"; `match` tells which route a request was.
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
import re
import shutil
import subprocess
import tempfile

METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
SKIP_DIRS = {"vendor", "node_modules", ".git", "target", "parityrun", "tests", "test", "__tests__", "spec",
             "storage", "cache", "dist", "build", ".venv", "venv", "__pycache__", "migrations"}
SOURCES = {".php", ".js", ".mjs", ".cjs", ".ts", ".mts", ".py", ".rs"}
NOT_PAGES = {"includes", "include", "inc", "lib", "libs", "partials", "templates", "views", "config", "src", "app"}


@dataclasses.dataclass(frozen=True)
class Route:
    method: str
    path: str
    source: str
    """Where it was found: "routes/web.php:12" or the framework's own list."""

    @property
    def key(self) -> str:
        return f"{self.method} {self.path}"


def template(path: str) -> str:
    """A route's path in one notation: {name} for every parameter (:id, <int:id>, {id?}, {id:\\d+}, a regex group)."""
    path = re.sub(r"<(?:\w+:)?(\w+)>", r"{\1}", path)  # Flask, Django
    path = re.sub(r"(?<=/):(\w+)\??", r"{\1}", path)  # Express, axum 0.7
    path = re.sub(r"\{(\w+)[?:][^}]*\}|\{(\w+)\?\}", lambda m: "{" + (m.group(1) or m.group(2)) + "}", path)
    path = re.sub(r"\{\*(\w+)\}", r"{\1}", path)  # axum 0.8 wildcard
    if not path.startswith("/"):
        path = "/" + path
    return path if path == "/" else path.rstrip("/") or "/"


def from_regex(pattern: str) -> str | None:
    """A path regex as a template ("^/entries/([0-9a-f]+)/delete$" -> "/entries/{param}/delete"); None if it
    isn't a plain path with groups."""
    body = pattern.strip().removeprefix("^").removesuffix("$").replace("\\/", "/")
    out, depth, name, k = "", 0, "", 0
    while k < len(body):
        c = body[k]
        if c == "\\" and depth == 0:
            out += body[k + 1:k + 2]
            k += 2
            continue
        if c == "(":
            if depth == 0:
                named = re.match(r"\(\?P?<(\w+)>", body[k:])
                name = named.group(1) if named else "param"
            depth += 1
        elif c == ")" and depth:
            depth -= 1
            if depth == 0:
                out += "{" + name + "}"
                if body[k + 1:k + 2] in ("?", "+", "*"):
                    k += 1
        elif depth == 0:
            out += c
        k += 1
    if not out.startswith("/") or re.search(r"[\[\]*+?|^$()]", out):
        return None
    return template(out)


def _files(app: pathlib.Path):
    for path in sorted(app.rglob("*")):
        rel = path.relative_to(app)
        if path.is_file() and path.suffix in SOURCES and not (set(rel.parts[:-1]) & SKIP_DIRS) \
                and not re.search(r"\.(test|spec|min)\.\w+$", path.name):
            yield path, rel.as_posix()


# --- PHP -------------------------------------------------------------------------------------------------------

LARAVEL = re.compile(r"Route::(get|post|put|patch|delete|any|match|view|redirect|permanentRedirect)\("
                     r"\s*(?:\[([^\]]*)\]\s*,\s*)?['\"]([^'\"]*)['\"]")
LARAVEL_RESOURCE = re.compile(r"Route::(resource|apiResource)\(\s*['\"]([^'\"]+)['\"]")
LARAVEL_PREFIX = re.compile(r"(?:prefix\(\s*['\"]([^'\"]*)['\"]\s*\)|['\"]prefix['\"]\s*=>\s*['\"]([^'\"]*)['\"])")
SLIM = re.compile(r"\$\w+->(get|post|put|patch|delete|any|map)\(\s*(?:\[([^\]]*)\]\s*,\s*)?['\"](/[^'\"]*)['\"]")
SYMFONY = re.compile(r"(?:#\[Route|@Route)\(\s*(?:path\s*[:=]\s*)?['\"]([^'\"]*)['\"](?:[^\n]*?methods\s*[:=]\s*"
                     r"[\[{]([^\]}]*)[\]}])?")
PHP_EQ = re.compile(r"\$\w*(?:path|uri|url|route|page)\w*\s*===?\s*['\"](/[^'\"]*)['\"]", re.I)
PHP_REGEX = re.compile(r"preg_match\(\s*(['\"])([#~!@%|/])(.*?)\2[imsxuU]*\1")
PHP_METHOD = re.compile(r"(?:\$\w*method\w*|\$_SERVER\[['\"]REQUEST_METHOD['\"]\])\s*===?\s*['\"](\w+)['\"]", re.I)


def _methods(text: str | None, default=("GET",)) -> list[str]:
    found = [m.upper() for m in re.findall(r"\w+", text or "") if m.upper() in METHODS]
    return found or list(default)


def _singular(word: str) -> str:
    return word[:-3] + "y" if word.endswith("ies") else word[:-1] if word.endswith("s") else word


def _laravel(text: str, rel: str) -> list[Route]:
    out, prefixes, depth = [], [], 0
    for n, line in enumerate(text.splitlines(), 1):
        prefix = "/".join(p.strip("/") for p, _ in prefixes if p.strip("/"))
        for m in LARAVEL.finditer(line):
            kind, listed, path = m.group(1), m.group(2), m.group(3)
            methods = _methods(listed) if kind == "match" else ["GET", "POST"] if kind == "any" else \
                [kind.upper()] if kind.upper() in METHODS else ["GET"]  # view, redirect
            for method in methods:
                out.append(Route(method, template(f"{prefix}/{path.strip('/')}"), f"{rel}:{n}"))
        for m in LARAVEL_RESOURCE.finditer(line):
            name = m.group(2).strip("/")
            base, param = f"{prefix}/{name}", "{" + _singular(name.split("/")[-1].split(".")[-1]) + "}"
            routes = [("GET", base), ("POST", base), ("GET", f"{base}/{param}"), ("PUT", f"{base}/{param}"),
                      ("PATCH", f"{base}/{param}"), ("DELETE", f"{base}/{param}")]
            if m.group(1) == "resource":
                routes += [("GET", f"{base}/create"), ("GET", f"{base}/{param}/edit")]
            out += [Route(method, template(path), f"{rel}:{n}") for method, path in routes]
        opened = line.count("{") - line.count("}")
        found = LARAVEL_PREFIX.search(line)
        if found and "group" in line:  # a prefix for what the group's function declares
            prefixes.append((found.group(1) or found.group(2) or "", depth))
        depth += opened
        while prefixes and depth <= prefixes[-1][1]:
            prefixes.pop()
    return out


def _symfony(text: str, rel: str) -> list[Route]:
    out, prefix = [], ""
    lines = text.splitlines()
    for n, line in enumerate(lines, 1):
        m = SYMFONY.search(line)
        if not m:
            continue
        following = next((x for x in lines[n:n + 4] if x.strip() and not x.strip().startswith(("#", "@", "*", "/"))),
                         "")
        if re.search(r"\bclass\b", following):  # on the class: a prefix for its methods
            prefix = m.group(1).rstrip("/")
            continue
        out += [Route(method, template(prefix + "/" + m.group(1).lstrip("/")), f"{rel}:{n}")
                for method in _methods(m.group(2))]
    return out


def _front_controller(text: str, rel: str, equal: re.Pattern, regex: re.Pattern, method: re.Pattern,
                      regex_group: int) -> list[Route]:
    """Routes a hand-made router compares the path with: literals and regexes, the method on the line or the next."""
    out = []
    lines = text.splitlines()
    for n, line in enumerate(lines, 1):
        paths = [template(m.group(1)) for m in equal.finditer(line)]
        paths += [p for m in regex.finditer(line) if (p := from_regex(m.group(regex_group)))]
        if not paths:
            continue
        near = method.findall(line) or method.findall(lines[n] if n < len(lines) else "")
        for path in paths:
            out += [Route(m, path, f"{rel}:{n}") for m in _methods(" ".join(near))]
    return out


def _php(text: str, rel: str) -> list[Route]:
    routes = _laravel(text, rel) if "Route::" in text else []
    routes += [Route(method, template(m.group(3)), f"{rel}:{text.count(chr(10), 0, m.start()) + 1}")
               for m in SLIM.finditer(text)
               for method in (_methods(m.group(2)) if m.group(1) == "map" else
                              ["GET", "POST"] if m.group(1) == "any" else [m.group(1).upper()])]
    if "Route(" in text:
        routes += _symfony(text, rel)
    if not routes:
        routes = _front_controller(text, rel, PHP_EQ, PHP_REGEX, PHP_METHOD, 3)
    return routes


def _php_pages(app: pathlib.Path, found: set[str]) -> list[Route]:
    """A plain PHP site: every page file under the document root is a route (POST too when it reads $_POST)."""
    root = next((app / d for d in ("public", "web", "htdocs", "www", "public_html") if (app / d / "index.php").exists()),
                app)
    out = []
    for path, _ in _files(root):
        rel = path.relative_to(root)
        if path.suffix != ".php" or path.name.startswith("_") or set(rel.parts[:-1]) & NOT_PAGES \
                or path.relative_to(app).as_posix() in found:
            continue
        url = "/" + rel.as_posix()
        url = url[:-len("index.php")] if path.name == "index.php" else url
        text = path.read_text(encoding="utf-8", errors="replace")
        source = path.relative_to(app).as_posix()
        out.append(Route("GET", template(url), source))
        if "$_POST" in text or re.search(r"REQUEST_METHOD['\"]\]\s*===?\s*['\"]POST", text):
            out.append(Route("POST", template(url), source))
    return out


# --- JavaScript / TypeScript -------------------------------------------------------------------------------------

EXPRESS = re.compile(r"\b(?:app|router|server|fastify|api|routes|\w*[Rr]outer)\.(get|post|put|patch|delete|all)\("
                     r"\s*['\"`](/[^'\"`]*)['\"`]")
JS_EQ = re.compile(r"\b\w*(?:path|pathname|url|route)\w*\s*===?\s*['\"`](/[^'\"`]*)['\"`]", re.I)
JS_REGEX = re.compile(r"\.(?:match|test|exec)\(\s*/(\^?\\/.*?)/[gimsuy]*\s*\)")
JS_METHOD = re.compile(r"\bmethod\s*===?\s*['\"`](\w+)['\"`]", re.I)


def _js(text: str, rel: str) -> list[Route]:
    routes = [Route(method, template(m.group(2)), f"{rel}:{text.count(chr(10), 0, m.start()) + 1}")
              for m in EXPRESS.finditer(text)
              for method in (["GET", "POST"] if m.group(1) == "all" else [m.group(1).upper()])]
    return routes or _front_controller(text, rel, JS_EQ, JS_REGEX, JS_METHOD, 1)


# --- Python ------------------------------------------------------------------------------------------------------

FLASK = re.compile(r"@\w+\.route\(\s*['\"]([^'\"]+)['\"](?:[^\n]*?methods\s*=\s*[\[(]([^\])]*)[\])])?")
DECORATOR = re.compile(r"@\w+\.(get|post|put|patch|delete)\(\s*['\"]([^'\"]+)['\"]")
DJANGO = re.compile(r"\b(re_path|path)\(\s*r?['\"]([^'\"]*)['\"]")


def _python(text: str, rel: str) -> list[Route]:
    line = lambda m: f"{rel}:{text.count(chr(10), 0, m.start()) + 1}"  # noqa: E731
    routes = [Route(method, template(m.group(1)), line(m)) for m in FLASK.finditer(text) for method in
              _methods(m.group(2))]
    routes += [Route(m.group(1).upper(), template(m.group(2)), line(m)) for m in DECORATOR.finditer(text)]
    if rel.endswith("urls.py"):
        for m in DJANGO.finditer(text):
            path = from_regex("^/" + m.group(2).lstrip("^")) if m.group(1) == "re_path" else template(m.group(2))
            if path and "include(" not in text[m.start():text.find("\n", m.start())]:
                routes.append(Route("GET", path, line(m)))
    return routes


# --- Rust --------------------------------------------------------------------------------------------------------

AXUM = re.compile(r"\.route\(\s*\"([^\"]+)\"")
ACTIX = re.compile(r"#\[(get|post|put|patch|delete)\(\s*\"([^\"]+)\"")
RUST_EQ = re.compile(r"\b\w*path\w*\s*==\s*\"(/[^\"]*)\"")
RUST_METHOD = re.compile(r"\bmethod\w*\s*==\s*\"(\w+)\"")


def _rust(text: str, rel: str) -> list[Route]:
    line = lambda m: f"{rel}:{text.count(chr(10), 0, m.start()) + 1}"  # noqa: E731
    routes = []
    for m in AXUM.finditer(text):  # .route("/x", get(a).post(b)): the methods up to the next route
        rest = text[m.end():]
        ends = [at for at in (rest.find(".route("), rest.find(";")) if at >= 0]  # this route's statement only
        rest = rest[:min(ends)] if ends else rest[:400]
        routes += [Route(method.upper(), template(m.group(1)), line(m))
                   for method in re.findall(r"\b(get|post|put|patch|delete)\(", rest)]
    routes += [Route(m.group(1).upper(), template(m.group(2)), line(m)) for m in ACTIX.finditer(text)]
    return routes or _front_controller(text, rel, RUST_EQ, re.compile(r"(?!x)x"), RUST_METHOD, 0)


# --- Frameworks that list their own routes -----------------------------------------------------------------------

def from_artisan(listing: list[dict]) -> list[Route]:
    """`php artisan route:list --json`: [{"method": "GET|HEAD", "uri": "posts/{post}"}, ...]."""
    return [Route(method, template(item["uri"]), "artisan route:list")
            for item in listing for method in _methods(item.get("method", "").replace("|", " "))]


def from_symfony(listing: dict) -> list[Route]:
    """`php bin/console debug:router --format=json`: {name: {"path": "/x/{id}", "method": "GET|POST" or "ANY"}}."""
    return [Route(method, template(item["path"]), "debug:router") for name, item in listing.items()
            if not name.startswith("_")  # the profiler's and the framework's own
            for method in _methods(item.get("method", "").replace("|", " "))]


def _framework_list(app: pathlib.Path) -> tuple[list[Route], str | None]:
    """The routes as Laravel or Symfony list them, from a throwaway copy; ([], why) when that can't be done."""
    if (app / "artisan").exists():
        cmd, parse = ["php", "artisan", "route:list", "--json"], lambda out: from_artisan(json.loads(out))
    elif (app / "bin" / "console").exists():
        cmd, parse = ["php", "bin/console", "debug:router", "--format=json"], lambda out: from_symfony(json.loads(out))
    else:
        return [], None
    if not shutil.which("php") or not (app / "vendor").exists():
        return [], "php or vendor/ is missing, so the routes were read from the code"
    work = pathlib.Path(tempfile.mkdtemp(prefix="parityrun-routes-"))
    try:
        shutil.copytree(app, work / "app", ignore=shutil.ignore_patterns(".git", "node_modules", "parityrun"),
                        symlinks=True)
        done = subprocess.run([shutil.which(cmd[0]), *cmd[1:]], cwd=work / "app", capture_output=True, text=True,
                              timeout=300)
        if done.returncode != 0:
            return [], f"`{' '.join(cmd)}` failed, so the routes were read from the code"
        return parse(done.stdout), None
    except (OSError, ValueError, subprocess.SubprocessError):
        return [], f"`{' '.join(cmd)}` didn't give a route list, so the routes were read from the code"
    finally:
        shutil.rmtree(work, ignore_errors=True)


READERS = {".php": _php, ".js": _js, ".mjs": _js, ".cjs": _js, ".ts": _js, ".mts": _js, ".py": _python, ".rs": _rust}


def scan(app: pathlib.Path, use_framework: bool = True) -> tuple[list[Route], list[str]]:
    """Every route of the app in `app` (one per method and path, sorted by path) and notes on how they were found."""
    notes = []
    routes, why = _framework_list(app) if use_framework else ([], None)
    if why:
        notes.append(why)
    if not routes:
        found: dict[str, list[Route]] = {}
        for path, rel in _files(app):
            found[rel] = READERS[path.suffix](path.read_text(encoding="utf-8", errors="replace"), rel)
        routes = [r for rs in found.values() for r in rs]
        framework = (app / "artisan").exists() or (app / "bin" / "console").exists()
        if not framework:
            routes += _php_pages(app, {rel for rel, rs in found.items() if rs})
    unique = {}
    for route in routes:
        unique.setdefault(route.key, route)
    return sorted(unique.values(), key=lambda r: (r.path, METHODS.index(r.method) if r.method in METHODS else 9)), notes


def pattern(path: str) -> re.Pattern:
    """A route's path as a regex for request paths: each {param} is one path segment."""
    return re.compile("^" + re.sub(r"\\\{[^/]+?\\\}", "[^/]+", re.escape(path)) + "$")


def match(keys: list[str], method: str, path: str) -> str | None:
    """The route ("GET /entries/{id}") a request was, or None; a literal route wins over one with parameters."""
    path = path.split("?")[0] or "/"
    hits = [key for key in keys if key.split(" ", 1)[0] == method and pattern(key.split(" ", 1)[1]).match(path)]
    return min(hits, key=lambda key: key.count("{"), default=None)
