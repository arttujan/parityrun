"""Run a scenario against an implementation and compare the result with the golden recording.

An implementation is a command that starts the service (impls.json). The harness gives it a fresh data folder, points
its outside services at the stub and talks to it only over HTTP (and its command line), so a rewrite in any language
is judged the same way. A run gives a recording: every response, every LLM and outside-service request, the database
and the files.
"""

from __future__ import annotations

import contextlib
import dataclasses
import html
import json
import os
import pathlib
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, Protocol, cast

import httpx
import yaml

from . import coverage, htmlnorm, snapshot
from .normalize import Normalizer
from .project import Project, RunInfo
from .stubs import Stub

if TYPE_CHECKING:
    from collections.abc import Callable, Collection

    from .jsontypes import JSON, Data

TEMPLATE = re.compile(r"\{\{\s*([\w.\[\]-]+)\s*\}\}")
SHIMS = pathlib.Path(__file__).resolve().parent / "shims"
"""What {shims} means in impls.json: parityrun's own shims, e.g. {shims}/library.php serves a PHP library."""
IMAGE_SSIM = 0.98  # images must look the same; antialiasing may differ between drawing libraries
MIN_CAPTURED = 8  # a captured value this long or longer is random (a token): recorded as <name>
DIFFERENCES = 8  # the differences a failed check lists


def free_port() -> int:
    """Return a local port nothing listens on."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


def _text(raw: bytes) -> str:
    return raw.decode("utf-8", "replace").replace("\r\n", "\n")


def _ok(status: int) -> bool:
    return status == HTTPStatus.OK


class Implementation:
    """One implementation of a project, as impls.json describes it."""

    def __init__(self, project: Project, name: str) -> None:
        """Read the implementation `name` from the project's impls.json."""
        super().__init__()
        spec: Data = json.loads((project.root / "impls.json").read_text(encoding="utf-8"))
        if name not in spec:
            msg = f"No implementation {name!r} in impls.json ({', '.join(spec)})"
            raise SystemExit(msg)
        self.project, self.name = project, name
        self.spec: Data = spec[name]
        self.cwd = self.folder
        self.port = 0
        self.env: dict[str, str] = {}
        self._info: RunInfo | None = None

    @property
    def folder(self) -> pathlib.Path:
        """Where the implementation runs (impls.json `cwd`, relative to the project's repo)."""
        return self.project.repo / self.spec.get("cwd", ".")

    @property
    def info(self) -> RunInfo:
        """What the last start gave the service."""
        if self._info is None:
            msg = f"{self.name} hasn't been started"
            raise RuntimeError(msg)
        return self._info

    def _format(self, parts: list[str], port: int | None = None) -> list[str]:
        """Return impls.json's command parts with {python}, {repo}, {shims} (and {port}) filled in."""
        repo = self.project.repo
        if port is None:
            return [part.format(python=sys.executable, repo=repo, shims=SHIMS.as_posix()) for part in parts]
        return [part.format(python=sys.executable, port=port, repo=repo, shims=SHIMS.as_posix()) for part in parts]

    def _environment(self, info: RunInfo, measure: dict[str, str], extra: Data | None) -> dict[str, str]:
        """Return the service's environment: the harness's, the project's, impls.json's, then the scenario's."""
        project, repo, stub = self.project, info.repo, info.stub_url
        spec_env: Data = self.spec.get("env", {})
        own = {
            k: str(v).format(python=sys.executable, port=info.port, repo=repo, shims=SHIMS.as_posix())  # its own
            for k, v in spec_env.items()
        }
        return {
            **os.environ,
            "PARITYRUN_STUB": stub,
            **measure,
            **(project.env(info) if project.env else {}),
            **own,
            **{
                k: str(v)
                .replace("{stub}", stub)
                .replace("{python}", sys.executable)
                .replace("{repo}", str(repo))
                .replace("{data}", info.data_dir.as_posix())
                for k, v in (extra or {}).items()
            },
        }

    def start(
        self,
        data_dir: pathlib.Path,
        stub_port: int,
        *,
        extra: Data | None = None,
        cwd: pathlib.Path | None = None,
        accept: Callable[[int], bool] | None = None,
    ) -> tuple[subprocess.Popen[bytes], str]:
        """Start the service on a free port with the project's environment (in `cwd`: a copy of its folder).

        Wait until its ready path answers 200 (or any status `accept` takes). Its output goes to service.log beside
        the data folder.
        """
        project, repo = self.project, self.project.repo
        log = data_dir.parent / "service.log"
        port = free_port()
        cmd = command(self._format(self.spec["cmd"], port))
        self.cwd = cwd or self.folder
        measure: dict[str, str] = {}
        if project.coverage and coverage.is_php(cmd):  # each request's lines, into <work>/coverage
            cmd = coverage.php_command(cmd, self.cwd, data_dir.parent / "coverage-prepend.php")
            measure = {"PARITYRUN_COVERAGE": str(data_dir.parent / "coverage")}
        stub = f"http://127.0.0.1:{stub_port}"
        self.port = port
        info = RunInfo(data_dir=data_dir, work=data_dir.parent, port=port, stub_url=stub, repo=repo)
        self._info = info
        if project.setup:  # e.g. an outside database emptied, so every run starts the same
            project.setup(info)
        self.env = self._environment(info, measure, extra)
        out = open(log, "w", encoding="utf-8")  # noqa: SIM115, PTH123 (the service writes to it until it stops)
        proc = subprocess.Popen(cmd, cwd=self.cwd, env=self.env, stdout=out, stderr=subprocess.STDOUT)  # noqa: S603 (impls.json's command)
        base = f"http://127.0.0.1:{port}"
        self._wait(proc, base, log, accept or _ok)
        return proc, base

    def _wait(self, proc: subprocess.Popen[bytes], base: str, log: pathlib.Path, accept: Callable[[int], bool]) -> None:
        """Wait until the service's ready path answers a status `accept` takes; kill it when it doesn't in time."""
        project = self.project
        deadline = time.time() + project.ready_timeout
        while time.time() < deadline:
            if proc.poll() is not None:
                msg = f"{self.name} exited at start; see {log}"
                raise RuntimeError(msg)
            try:
                if accept(httpx.get(f"{base}{project.ready}", headers=project.headers, timeout=30).status_code):
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.3)
        proc.kill()
        msg = f"{self.name} didn't answer in {project.ready_timeout:g} s; see {log}"
        raise RuntimeError(msg)

    def build(self) -> str | None:
        """Run impls.json `build` (e.g. cargo build --release) in the implementation's folder.

        Return its output when it fails, None when it built (or has nothing to build).
        """
        if "build" not in self.spec:
            return None
        cmd = command(self._format(self.spec["build"]))
        try:
            proc = subprocess.run(cmd, cwd=self.folder, capture_output=True, timeout=1800, check=False)  # noqa: S603 (impls.json's command)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"{' '.join(cmd)}: {exc}"
        if proc.returncode == 0:
            return None
        return _text(proc.stdout + proc.stderr)[-6000:]

    def cli(self, args: list[str]) -> Data:
        """Run the command line once next to the running service (same data, same environment): exit code, output."""
        if "cli" not in self.spec:
            msg = f"{self.name} has no cli in impls.json"
            raise RuntimeError(msg)
        cmd = command(self._format(self.spec["cli"])) + args
        env = {**self.env, "COLUMNS": "100"}
        proc = subprocess.run(cmd, cwd=self.cwd, env=env, capture_output=True, timeout=900, check=False)  # noqa: S603 (impls.json's command)
        return {"exit": proc.returncode, "stdout": _text(proc.stdout), "stderr": _text(proc.stderr)}


def command(cmd: list[str]) -> list[str]:
    """Return the command with a bare program name found on PATH, so Windows starts `npm` (npm.cmd) and the like."""
    if cmd and "/" not in cmd[0] and "\\" not in cmd[0]:
        return [shutil.which(cmd[0]) or cmd[0], *cmd[1:]]
    return cmd


def copy_app(impl: Implementation, target: pathlib.Path) -> dict[str, tuple[int, int]]:
    """Copy the implementation's folder to `target` for one run (Project.isolate).

    .git and the like (and impls.json `skip`, e.g. Rust's target/) and the parityrun project folder stay behind.
    Return each file's (size, mtime), to tell later what the run changed.
    """
    spec_skip: list[str] = impl.spec.get("skip", [])
    skip, root = set(impl.project.isolate_skip) | set(spec_skip), impl.project.root.resolve()

    def ignore(folder: str, names: list[str]) -> list[str]:
        return [n for n in names if n in skip or pathlib.Path(folder, n).resolve() == root]

    _ = shutil.copytree(impl.folder, target, ignore=ignore, symlinks=True)
    return {p.relative_to(target).as_posix(): _stamp(p) for p in target.rglob("*") if p.is_file()}


def _stamp(path: pathlib.Path) -> tuple[int, int]:
    st = path.stat()
    return st.st_size, st.st_mtime_ns


def app_changes(
    impl: Implementation, copy: pathlib.Path, baseline: dict[str, tuple[int, int]]
) -> tuple[set[str], set[str]]:
    """Return the files a run created or changed in its copy, and the ones it deleted.

    A file written again with the same bytes hasn't changed.
    """
    now = {p.relative_to(copy).as_posix(): p for p in copy.rglob("*") if p.is_file()}
    changed: set[str] = set()
    for rel, path in now.items():
        if rel not in baseline or (
            _stamp(path) != baseline[rel] and path.read_bytes() != (impl.folder / rel).read_bytes()
        ):
            changed.add(rel)
    return changed, set(baseline) - set(now)


def _resolve(expr: str, ctx: Data) -> JSON:
    value: Any = ctx
    for part in re.findall(r"[\w-]+", expr):
        value = cast("list[Any]", value)[int(part)] if isinstance(value, list) else value[part]
    return cast("JSON", value)


def render(value: JSON, ctx: Data) -> JSON:
    """Fill in {{step.path}}: a value from an earlier step's JSON response (the whole string keeps its type)."""
    if isinstance(value, str):
        whole = TEMPLATE.fullmatch(value.strip())
        if whole:
            return _resolve(whole.group(1), ctx)
        return TEMPLATE.sub(lambda m: str(_resolve(m.group(1), ctx)), value)
    if isinstance(value, list):
        return [render(v, ctx) for v in value]
    if isinstance(value, dict):
        if "$merge" in value:  # an earlier response sent back with some fields changed
            base = cast("dict[str, JSON]", render(value["$merge"], ctx))
            return {**base, **{k: render(v, ctx) for k, v in value.items() if k != "$merge"}}
        return {k: render(v, ctx) for k, v in value.items()}
    return value


def _path_value(data: JSON, path: str) -> JSON:
    """Return "worker.busy", "tasks[0].status", "[1].id" in a JSON response; None when it isn't there."""
    value: Any = data
    try:
        for part in re.findall(r"[\w-]+", path):
            value = cast("list[Any]", value)[int(part)] if isinstance(value, list) else value[part]
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    return cast("JSON", value)


def _met(data: JSON, until: Data) -> bool:
    value = _path_value(data, until["path"]) if until.get("path") else data
    if "equals" in until:
        return value == until["equals"]
    if "in" in until:
        return value in until["in"]
    if "not" in until:
        return value != until["not"]
    if "length" in until:
        return isinstance(value, list) and len(value) == until["length"]
    msg = f"until needs equals, in, not or length: {until}"
    raise ValueError(msg)


def _is_number(value: JSON) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def measured(value: JSON, keys: Collection[str], companions: Collection[str] = ()) -> JSON:
    """Blank measured numbers (and a number computed from them, in the same object) as "<measured>"."""
    if isinstance(value, dict):
        timed = any(k in keys and isinstance(v, (int, float)) for k, v in value.items())
        return {
            k: "<measured>"
            if (k in keys or (timed and k in companions)) and _is_number(v)
            else measured(v, keys, companions)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [measured(v, keys, companions) for v in value]
    return value


def _blank(data: JSON, parts: list[str]) -> None:
    """Replace the value at a dotted path ("tools.*.running", "gpus.*.driver") with "<ignored>"; * is any key."""
    if not parts:
        return
    if isinstance(data, list):
        _blank_items(data, parts[0], parts[1:])
    elif isinstance(data, dict):
        _blank_keys(data, parts[0], parts[1:])


def _blank_items(data: list[JSON], head: str, rest: list[str]) -> None:
    if head == "*":
        indexes: Collection[int] = range(len(data))
    elif head.isdigit():
        indexes = [int(head)]
    else:
        return  # a key on a list: nothing there
    for i in (i for i in indexes if i < len(data)):
        if rest:
            _blank(data[i], rest)
        else:
            data[i] = "<ignored>"


def _blank_keys(data: dict[str, JSON], head: str, rest: list[str]) -> None:
    for key in (k for k in (list(data) if head == "*" else [head]) if k in data):
        if rest:
            _blank(data[key], rest)
        else:
            data[key] = "<ignored>"


def _form_value(value: JSON) -> JSON:
    """Return a form field as a browser sends it: a ticked checkbox (YAML's on/yes/true) is "on"."""
    if isinstance(value, list):
        return [_form_value(v) for v in value]
    if value is True:
        return "on"
    return "" if value is None else str(value)


def _request(client: httpx.Client, step: Data, ctx: Data, fixtures: pathlib.Path) -> httpx.Response:
    method, template = step["call"].split(" ", 1)
    path = str(render(template, ctx))
    follow = bool(step.get("follow"))  # a browser following redirects to the page they end on
    if "raw" in step:  # a file sent as the body, e.g. an upload
        return client.request(
            method,
            path,
            content=(fixtures / step["raw"]).read_bytes(),
            headers={"Content-Type": "application/octet-stream"},
            follow_redirects=follow,
        )
    headers: dict[str, str] = step.get("headers") or {}  # e.g. Range: a player seeking in a video
    if "form" in step or "upload" in step:  # a web form: urlencoded, or multipart with files from fixtures
        fields = cast("Data", render(step.get("form"), ctx) or {})
        form = {k: _form_value(v) for k, v in fields.items() if v is not False}
        uploads: dict[str, str] = step.get("upload") or {}
        files = {field: (pathlib.Path(name).name, (fixtures / name).read_bytes()) for field, name in uploads.items()}
        return client.request(method, path, data=form, files=files or None, headers=headers, follow_redirects=follow)
    body = render(step.get("body"), ctx)
    return (
        client.request(method, path, json=body, headers=headers, follow_redirects=follow)
        if body is not None
        else client.request(method, path, headers=headers, follow_redirects=follow)
    )


def call_name(step: Data) -> str:
    """Return a library step as its recording names it: "fn slug", "fn Cart::fromJson", "fn cart.add", "new Cart"."""
    return f"new {step['new']}" if "new" in step else f"fn {step['fn']}"


def _library_call(client: httpx.Client, step: Data, ctx: Data, where: str) -> Data:
    """Send one `fn` or `new` step to the library's shim: {"value"} or {"error"}, with "output" if it printed."""
    call = {
        key: render(step[key], ctx) if key == "args" else step[key]
        for key in ("fn", "new", "args", "as")
        if key in step
    }
    r = client.post("/call", json=call)
    if r.status_code != HTTPStatus.OK or not r.headers.get("content-type", "").startswith("application/json"):
        msg = f"{where} ({call_name(step)}): the shim answered {r.status_code}: {r.text[:2000]}"
        raise RuntimeError(msg)
    result: Data = r.json()
    return result


def _capture(r: httpx.Response, captures: Data, kind: str, where: str) -> dict[str, str]:
    """Return values for later steps' templates: a regex's first group (or whole match) in the body, or a header."""
    found: dict[str, str] = {}
    for name, given in captures.items():
        rule = cast("Data", given) if isinstance(given, dict) else {"regex": given}
        text = r.headers.get(rule["header"], "") if "header" in rule else r.text
        match = re.search(rule["regex"], text)
        if not match:
            msg = f"{where}: capture {name} found nothing for {rule['regex']!r} in {text[:500]!r}"
            raise RuntimeError(msg)
        groups: tuple[str | None, ...] = match.groups()
        value = next((g for g in groups if g is not None), match.group(0))  # alternatives: the one that matched
        found[name] = html.unescape(value) if kind == "text/html" and "header" not in rule else value
    return found


def _listen(url: str, headers: dict[str, str], count: int, found: list[JSON], ready: threading.Event) -> None:
    """Collect the first `count` server-sent events (data lines) from the project's events path."""
    try:
        with httpx.stream("GET", url, headers=headers, timeout=20) as r:
            ready.set()
            for line in r.iter_lines():
                if line.startswith("data: "):
                    found.append(json.loads(line[6:]))
                    if len(found) >= count:
                        return
    except httpx.HTTPError:
        ready.set()


def _comma_split(value: object) -> list[str]:
    """Return the keys a YAML flow mapping made from an unquoted comma ({title: a, b} -> {"title": "a", "b": None})."""
    if isinstance(value, dict):
        mapping = cast("dict[object, object]", value)
        return [k for k, v in mapping.items() if v is None and isinstance(k, str) and " " in k] + [
            k for v in mapping.values() for k in _comma_split(v)
        ]
    if isinstance(value, list):
        return [k for v in cast("list[object]", value) for k in _comma_split(v)]
    return []


def load(project: Project, name: str) -> Data:
    """Return the scenario `name` of the project."""
    scenario: Data = yaml.safe_load((project.scenarios / f"{name}.yaml").read_text(encoding="utf-8"))
    split = _comma_split(scenario["steps"])
    if split:
        msg = f"{name}: quote the text with commas in it; YAML split it at {split}"
        raise ValueError(msg)
    return scenario


def all_scenarios(project: Project) -> list[str]:
    """Return the names of the project's scenarios."""
    return sorted(p.stem for p in project.scenarios.glob("*.yaml"))


def _entry_calls(entry: Data, stub: Stub) -> None:
    requests = stub.take()
    if requests:
        entry["llm"] = requests
    calls = stub.take_platform()
    if calls:
        entry["platform"] = calls


def _write_files(scenario: Data, fixtures: pathlib.Path, data_dir: pathlib.Path, stub: Stub) -> None:
    """Put the scenario's `files` in the data folder: state a service can't be given over HTTP."""
    files: dict[str, str] = scenario.get("files") or {}
    for rel, fixture in files.items():
        target = data_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if (fixtures / fixture).suffix in (".png", ".jpg"):  # a picture, as it is
            _ = target.write_bytes((fixtures / fixture).read_bytes())
            continue
        text = (fixtures / fixture).read_text(encoding="utf-8").replace("{stub}", stub.url)
        _ = target.write_text(text.replace("{data}", data_dir.as_posix()), encoding="utf-8")


def _scenario_env(scenario: Data, work: pathlib.Path) -> Data:
    """Return the scenario's `env`, with {fake_claude} made a real executable."""
    env: Data = dict(scenario.get("env") or {})
    if any("{fake_claude}" in str(v) for v in env.values()):
        exe = _fake_claude(work / "bin")
        env = {k: str(v).replace("{fake_claude}", exe) for k, v in env.items()}
    return env


@dataclasses.dataclass
class _Run:
    """One scenario's run: the service, its stub and what the steps have given so far."""

    impl: Implementation
    name: str
    scenario: Data
    out: pathlib.Path
    """Where pictures and frames go."""
    work: pathlib.Path
    stub: Stub
    app: pathlib.Path | None
    """The run's copy of the implementation's folder (Project.isolate)."""
    baseline: dict[str, tuple[int, int]]
    base: str = ""
    ctx: Data = dataclasses.field(default_factory=dict[str, Any])
    captured: dict[str, str] = dataclasses.field(default_factory=dict[str, str])

    @property
    def project(self) -> Project:
        return self.impl.project

    @property
    def data_dir(self) -> pathlib.Path:
        return self.work / "data"

    def step(self, client: httpx.Client, k: int, step: Data) -> Data:
        """Run one step and return its entry in the recording."""
        project, stub, fixtures = self.project, self.stub, self.project.fixtures
        replies: list[JSON] = step.get("llm") or []
        stub.queue(
            [
                json.loads((fixtures / str(r["$file"])).read_text(encoding="utf-8"))
                if isinstance(r, dict) and "$file" in r
                else r
                for r in replies
            ]
        )
        for key in project.stub_queues:
            stub.feed(key, cast("list[JSON]", render(step.get(key) or [], self.ctx)))
        events: list[JSON] = []
        listener = self._listen(k, step, events) if step.get("sse") else None
        entry: Data
        if "cli" in step:  # a command-line run instead of an HTTP call
            entry = {
                "call": f"cli {step['cli']}",
                "cli": self.impl.cli(shlex.split(str(render(step["cli"], self.ctx)))),
            }
        elif "fn" in step or "new" in step:  # a call into a library, through its shim
            result = _library_call(client, step, self.ctx, f"{self.name} step {k}")
            entry = {"call": call_name(step), "result": result}
            self.ctx[step.get("id", f"step{k}")] = result
        else:
            r = self._call(client, k, step)
            if listener:
                listener.join(15)
            entry = self._response(k, step, r, events)
        _entry_calls(entry, stub)
        return entry

    def _listen(self, k: int, step: Data, events: list[JSON]) -> threading.Thread:
        """Listen to the events while the step's call runs."""
        if not self.project.events:
            msg = f"{self.name} step {k}: sse needs the project's events path"
            raise ValueError(msg)
        ready = threading.Event()
        args = (self.base + self.project.events, self.project.headers, int(step["sse"]), events, ready)
        listener = threading.Thread(target=_listen, args=args, daemon=True)
        listener.start()
        _ = ready.wait(10)
        time.sleep(0.3)
        return listener

    def _call(self, client: httpx.Client, k: int, step: Data) -> httpx.Response:
        """Send the step's request; with `until`, again until a background task is done (the last answer counts)."""
        r = _request(client, step, self.ctx, self.project.fixtures)
        if "until" not in step:
            return r
        deadline = time.time() + float(step.get("timeout", 300))
        while not (r.headers.get("content-type", "").startswith("application/json") and _met(r.json(), step["until"])):
            if time.time() > deadline:
                # the whole answer: an error and a log tail are what explain a stuck step
                msg = (
                    f"{self.name} step {k} ({step['call']}): {step['until']} not met in time; "
                    f"last answer {r.text[:4000]}"
                )
                raise RuntimeError(msg)
            time.sleep(float(step.get("every", 1)))
            r = _request(client, step, self.ctx, self.project.fixtures)
        return r

    def _response(self, k: int, step: Data, r: httpx.Response, events: list[JSON]) -> Data:
        """Return an HTTP step's entry: status, redirects, events, the body and its disposition."""
        kind = r.headers.get("content-type", "").split(";")[0]
        if step.get("capture"):  # e.g. a CSRF token in a form: random, so recorded as <name>
            found = _capture(r, step["capture"], kind, f"{self.name} step {k} ({step['call']})")
            self.ctx.update(found)
            self.captured.update({value: f"<{key}>" for key, value in found.items() if len(value) >= MIN_CAPTURED})
        entry: Data = {"call": step["call"], "status": r.status_code}
        redirect = HTTPStatus.MULTIPLE_CHOICES <= r.status_code < HTTPStatus.BAD_REQUEST
        if self.project.redirects and redirect and "location" in r.headers:
            entry["location"] = r.headers["location"]
        if step.get("follow"):  # the redirects on the way to the page the step ends on
            entry["redirects"] = [[h.status_code, h.headers.get("location", "")] for h in r.history]
        if events:
            entry["events"] = events
        entry.update(self._body(k, step, r, kind))
        if r.headers.get("content-disposition"):
            entry["disposition"] = r.headers["content-disposition"]
        return entry

    def _body(self, k: int, step: Data, r: httpx.Response, kind: str) -> Data:
        """Return how a response's body is recorded."""
        if kind == "application/json":
            body: JSON = r.json()
            self.ctx[step.get("id", f"step{k}")] = body
            return {"json": body}
        if kind.startswith("image/"):
            key = f"response-{k}.png"  # kept as a small PNG (snapshot.shrink)
            _ = snapshot.shrink(r.content, self.out / key)
            return {"image": key}
        if r.status_code == HTTPStatus.PARTIAL_CONTENT:  # which bytes of how many (the file's size is the encoder's)
            content_range = r.headers.get("content-range", "").rsplit("/", 1)[0]
            return {"range": {"content_range": content_range, "bytes": len(r.content), "type": kind}}
        if kind.startswith("video/"):  # kept as its duration, size and frames (response-k#frame<n>.png)
            clip = self.work / f"response-{k}.mp4"
            _ = clip.write_bytes(r.content)
            video, frames = snapshot.video(clip, self.out / f"response-{k}")
            return {"video": video, "frames": [f.name for f in frames]}
        if self.project.html and (kind == "text/html" or not r.content):  # as a browser reads it; an empty
            return {"html": htmlnorm.lines(r.text)}  # answer (a redirect) is an empty page whatever its type
        return {"text": r.text}

    def _database(self) -> Data:
        """Return the database the run left: the project's own snapshot, or its first SQLite file."""
        project = self.project
        if project.snapshot:  # an outside database (MySQL, PostgreSQL, ...) read by the project
            return project.snapshot(self.impl.info)
        if not project.databases:
            return {}
        # in the data folder, or (isolated) where the app keeps it in its own folder
        folders = [folder for folder in (self.data_dir, self.app) if folder]
        db_file = next((f / project.databases[0] for f in folders if (f / project.databases[0]).exists()), None)
        return snapshot.database(db_file) if db_file else {}

    def _files(self) -> tuple[dict[str, Data], dict[str, pathlib.Path]]:
        """Return the files the run left (data folder, the app's copy, extra roots) and the pictures among them."""
        project, work = self.project, self.work
        endings = ("", "-wal", "-shm", "-journal")
        skip = {f"{pathlib.PurePosixPath(n).name}{s}" for n in project.databases for s in endings}
        rules = snapshot.FileRules(skip, project.random_files, project.interleaved_files, project.ignore_files)
        files, images = snapshot.files(self.data_dir, rules=rules)
        if self.app:  # what the run created, changed or deleted in its copy of the app, as app/...
            changed, deleted = app_changes(self.impl, self.app, self.baseline)
            more_files, more_images = snapshot.files(self.app, work / "frames", rules, only=changed)
            files.update({f"app/{k}": v for k, v in more_files.items()})
            images.update({f"app/{k}": v for k, v in more_images.items()})
            files.update(
                {
                    f"app/{rel}": {"deleted": True}
                    for rel in sorted(deleted)
                    if not snapshot.ignored(rel, project.ignore_files)
                }
            )
        for prefix, folder in project.extra_roots.items():  # folders the service writes outside its data folder
            if (work / folder).exists():
                more_files, more_images = snapshot.files(work / folder, work / "frames", rules)
                files.update({f"{prefix}/{k}": v for k, v in more_files.items()})
                images.update({f"{prefix}/{k}": v for k, v in more_images.items()})
        return files, images

    def recording(self, steps: list[Data]) -> Data:
        """Return the normalised recording of the finished run; its pictures go to `out`."""
        project, work, out = self.project, self.work, self.out
        measured_lines = coverage.collect(work / "coverage", self.impl.cwd) if project.coverage else None
        if measured_lines:  # beside the recording, not in it: record keeps it as golden/<name>/coverage.json
            out.mkdir(parents=True, exist_ok=True)
            _ = (out / "coverage.json").write_text(json.dumps(measured_lines, indent=1) + "\n", encoding="utf-8")
        db = self._database()
        files, images = self._files()
        port = self.base.rsplit(":", 1)[1]
        labels = project.labels
        data_dir = self.data_dir
        norm = Normalizer(
            {
                str(data_dir): labels["data"],
                data_dir.as_posix(): labels["data"],
                str(work): labels["work"],
                work.as_posix(): labels["work"],
                f"127.0.0.1:{self.stub.port}": labels["stub"],
                f"127.0.0.1:{port}": labels["service"],
                f"localhost:{port}": labels["service"],
            }
        )
        raw: JSON = {"steps": cast("list[JSON]", steps), "db": db, "files": cast("dict[str, JSON]", files)}
        norm.collect(raw)
        norm.add({value: label for value, label in self.captured.items() if value not in norm.ids})  # ids keep theirs
        recording = cast("Data", measured(norm.apply(raw), project.measured_keys, project.measured_companions))
        if project.post_process:
            project.post_process(recording)
        self._after(recording)
        for rel, path in images.items():  # keep the images under their normalised names
            target = out / "files" / safe(norm.text(rel))
            target.parent.mkdir(parents=True, exist_ok=True)
            _ = snapshot.shrink(path, target)
        return recording

    def _after(self, recording: Data) -> None:
        """Apply the scenario's `ignore` and `unordered` to the recorded steps."""
        ignore: list[str] = self.scenario.get("ignore") or []
        for entry, step in zip(recording["steps"], self.scenario["steps"], strict=False):
            step_ignore: list[str] = step.get("ignore") or []
            for pattern in ignore + step_ignore:  # machine state, not code
                for part in ("json", "result"):
                    if part in entry:
                        _blank(entry[part], pattern.split("."))
            if step.get("unordered") and isinstance(entry.get("json"), list):  # e.g. a log parallel work shares
                entry["json"].sort(key=_row_key)


def run(impl: Implementation, name: str, out: pathlib.Path, *, strict: bool = True) -> Data:
    """Run one scenario on a fresh service and return the normalised recording; pictures and frames go to `out`."""
    project = impl.project
    scenario = load(project, name)
    work = pathlib.Path(tempfile.mkdtemp(prefix=f"{project.work_prefix}{name}-"))
    data_dir = work / "data"
    data_dir.mkdir()
    stub = Stub(project.stub_handler)
    _write_files(scenario, project.fixtures, data_dir, stub)
    env = _scenario_env(scenario, work)
    app = work / "app" if project.isolate else None  # a copy of the implementation's folder: it may write anywhere
    baseline = copy_app(impl, app) if app else {}
    session = _Run(impl, name, scenario, out, work, stub, app, baseline)
    proc, session.base = impl.start(data_dir, stub.port, extra=env, cwd=app)
    session.ctx["stub"] = stub.url
    try:
        client = httpx.Client(base_url=session.base, headers=project.headers, timeout=180)
        steps = [session.step(client, k, step) for k, step in enumerate(scenario["steps"])]
        if stub.unused() and strict:  # recording: an authoring error; checking: the missing prompts fail anyway
            msg = f"{name}: {stub.unused()} queued LLM replies were never asked for; fix the scenario"
            raise RuntimeError(msg)
        if project.shutdown:
            method, path = project.shutdown.split(" ", 1)
            with contextlib.suppress(httpx.HTTPError):
                _ = client.request(method, path)
            with contextlib.suppress(subprocess.TimeoutExpired):
                _ = proc.wait(timeout=20)
    finally:
        if proc.poll() is None:
            proc.kill()
            _ = proc.wait()
        stub.close()
    recording = session.recording(steps)
    shutil.rmtree(work, ignore_errors=True)
    return recording


class _ScriptMaker(Protocol):
    """What _fake_claude uses of distlib's ScriptMaker (pip's copy has no type annotations)."""

    executable: str
    variants: set[str]

    def make(self, specification: str) -> list[str]:
        """Make the executables for an entry point; return their paths."""
        ...


def _fake_claude(folder: pathlib.Path) -> str:
    """Make a real executable for fake_claude (pip's launcher): every argument arrives intact, newlines included."""
    from pip._vendor.distlib.scripts import ScriptMaker  # noqa: PLC0415 (pip is needed only here)

    folder.mkdir(parents=True, exist_ok=True)
    maker = cast("_ScriptMaker", ScriptMaker(None, str(folder)))
    maker.executable = sys.executable
    maker.variants = {""}
    made: list[str] = maker.make("fake_claude = parityrun.fake_claude:main")
    return made[0]


def safe(rel: str) -> str:
    """Return a normalised path as a file name Windows accepts ("<id:3>" -> "(id-3)")."""
    return rel.replace("<", "(").replace(">", ")").replace(":", "-")


def _flatten(value: JSON, prefix: str = "") -> dict[str, JSON]:
    if isinstance(value, dict):
        out: dict[str, JSON] = {}
        for k, v in value.items():
            out.update(_flatten(v, f"{prefix}.{k}" if prefix else k))
        return out
    if isinstance(value, list):
        out = {}
        for i, v in enumerate(value):
            out.update(_flatten(v, f"{prefix}[{i}]"))
        return out or {prefix: []}
    return {prefix: value}


def differences(a: JSON, b: JSON, limit: int = DIFFERENCES) -> list[str]:
    """Return where two values differ, path by path (at most `limit` of them, then how many more)."""
    fa, fb = _flatten(a), _flatten(b)
    diffs = [
        f"{k}: {fa.get(k, '<missing>')!r} != {fb.get(k, '<missing>')!r}"
        for k in sorted(set(fa) | set(fb))
        if fa.get(k, "<missing>") != fb.get(k, "<missing>")
    ]
    return diffs[:limit] + ([f"... {len(diffs) - limit} more"] if len(diffs) > limit else [])


TRACEBACK = re.compile(r"\nTraceback \(most recent call last\):\n.*\Z", re.DOTALL)


def _no_tracebacks(value: JSON) -> JSON:
    """Cut a failed job's error at the implementation's own stack trace: compare everything before it."""
    if isinstance(value, dict):
        return {k: _no_tracebacks(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_no_tracebacks(v) for v in value]
    if isinstance(value, str) and "\nTraceback (most recent call last):\n" in value:
        return TRACEBACK.sub("\n<traceback>", value)
    return value


PARTS = (
    "status",
    "location",
    "redirects",
    "json",
    "text",
    "html",
    "disposition",
    "events",
    "video",
    "range",
    "cli",
    "result",
)
"""The parts of a step's entry compared as they are."""


@dataclasses.dataclass(frozen=True)
class _Folders:
    """Where the golden run's and this run's pictures are, and how alike they must be."""

    golden: pathlib.Path
    got: pathlib.Path
    threshold: float

    def picture(self, name: str, golden: str, got: str | None) -> Data:
        """Return a picture check: the two look alike (structural similarity)."""
        score = snapshot.ssim(self.golden / golden, self.got / got) if got is not None else 0.0
        return {"name": name, "ok": score >= self.threshold, "detail": [f"SSIM {score:.4f} (needs {self.threshold})"]}


def _same(name: str, want: JSON, have: JSON) -> Data:
    return {"name": name, "ok": want == have, "detail": differences(want, have)}


def _step_checks(k: int, want: Data, have: Data, folders: _Folders) -> list[Data]:
    """Return the checks of one step: its frames, every recorded part, its picture, prompts and outside calls."""
    out: list[Data] = []
    call = f"step {k} {want['call']}"
    have_frames: list[str] = have.get("frames", [])
    want_frames: list[str] = want.get("frames", [])
    out += [folders.picture(f"{call} {name}", name, name if name in have_frames else None) for name in want_frames]
    out += [_same(f"{call} {part}", want.get(part), have.get(part)) for part in PARTS if part in want or part in have]
    if "image" in want:
        out.append(folders.picture(f"{call} image", want["image"], have.get("image")))
    if "llm" in want or "llm" in have:
        out.append(_same(f"{call} prompts", want.get("llm"), have.get("llm")))
    if "platform" in want or "platform" in have:
        out.append(_same(f"{call} platform calls", want.get("platform"), have.get("platform")))
    return out


def _row_key(row: JSON) -> str:
    return json.dumps(row, sort_keys=True)  # row order isn't part of the contract


def _db_checks(golden: Data, got: Data) -> list[Data]:
    """Return the checks of the database: each table's schema, and its rows in any order."""
    out = [
        _same(f"schema {table}", golden["schema"].get(table), got.get("schema", {}).get(table))
        for table in sorted(set(golden.get("schema", {})) | set(got.get("schema", {})))
    ]
    for table in sorted(set(golden.get("rows", {})) | set(got.get("rows", {}))):
        want = sorted(golden["rows"].get(table, []), key=_row_key)
        have = sorted(got.get("rows", {}).get(table, []), key=_row_key)
        out.append(_same(f"rows {table}", want, have))
    return out


def _resorted(entry: Data | None) -> Data | None:
    """Return an interleaved JSON Lines file's lines sorted again (now that measured numbers are blanked)."""
    if entry and "jsonl" in entry:
        return {"jsonl": sorted(entry["jsonl"], key=_row_key)}
    return entry


def _file_checks(golden: Data, got: Data, folders: _Folders, interleaved: Collection[str]) -> list[Data]:
    """Return the checks of the files: each one as recorded, pictures by how alike they look."""
    out: list[Data] = []
    for rel in sorted(set(golden) | set(got)):
        want, have = golden.get(rel), got.get(rel)
        if rel.rsplit("/", 1)[-1] in interleaved:
            want, have = _resorted(want), _resorted(have)
        check = _same(f"file {rel}", want, have)
        if check["ok"] and want and "image" in want:
            check = folders.picture(f"file {rel}", f"files/{safe(rel)}", f"files/{safe(rel)}")
        out.append(check)
    return out


def checks(
    golden: Data, got: Data, golden_dir: pathlib.Path, got_dir: pathlib.Path, project: Project | None = None
) -> list[Data]:
    """Return every comparison as {name, ok, detail}: each step's response and prompts, each table, each file."""
    folders = _Folders(golden_dir, got_dir, project.image_ssim if project else IMAGE_SSIM)
    interleaved = project.interleaved_files if project else set[str]()
    golden, got = cast("Data", _no_tracebacks(golden)), cast("Data", _no_tracebacks(got))
    out: list[Data] = []
    for k, want in enumerate(golden["steps"]):
        out += _step_checks(k, want, got["steps"][k] if k < len(got["steps"]) else {}, folders)
    out += _db_checks(golden["db"], got["db"])
    out += _file_checks(golden["files"], got["files"], folders, interleaved)
    return out
