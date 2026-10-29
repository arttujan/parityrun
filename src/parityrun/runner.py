"""Run a scenario against an implementation and compare the result with the golden recording.

An implementation is a command that starts the service (impls.json). The harness gives it a fresh data folder, points
its outside services at the stub and talks to it only over HTTP (and its command line), so a rewrite in any language
is judged the same way. A run gives a recording: every response, every LLM and outside-service request, the database
and the files.
"""

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

import httpx
import yaml

from . import coverage, htmlnorm, snapshot
from .normalize import Normalizer
from .project import Project, RunInfo
from .stubs import Stub

TEMPLATE = re.compile(r"\{\{\s*([\w.\[\]-]+)\s*\}\}")
IMAGE_SSIM = 0.98  # images must look the same; antialiasing may differ between drawing libraries


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Implementation:
    def __init__(self, project: Project, name: str) -> None:
        spec = json.loads((project.root / "impls.json").read_text(encoding="utf-8"))
        if name not in spec:
            raise SystemExit(f"No implementation {name!r} in impls.json ({', '.join(spec)})")
        self.project, self.name, self.spec = project, name, spec[name]
        self.cwd = self.folder

    @property
    def folder(self) -> pathlib.Path:
        """Where the implementation runs (impls.json `cwd`, relative to the project's repo)."""
        return self.project.repo / self.spec.get("cwd", ".")

    def start(self, data_dir: pathlib.Path, stub_port: int, log: pathlib.Path, extra: dict | None = None,
              cwd: pathlib.Path | None = None, accept=None) -> tuple[subprocess.Popen, str]:
        """Start the service on a free port with the project's environment (in `cwd`: an isolated copy of its folder);
        wait until its ready path answers 200 (or any status `accept` takes)."""
        project, repo = self.project, self.project.repo
        port = free_port()
        cmd = _command([part.format(python=sys.executable, port=port, repo=repo) for part in self.spec["cmd"]])
        self.cwd = cwd or self.folder
        measure = {}
        if project.coverage and coverage.is_php(cmd):  # each request's lines, into <work>/coverage
            cmd = coverage.php_command(cmd, self.cwd, data_dir.parent / "coverage-prepend.php")
            measure = {"PARITYRUN_COVERAGE": str(data_dir.parent / "coverage")}
        stub = f"http://127.0.0.1:{stub_port}"
        self.port = port
        info = RunInfo(data_dir=data_dir, work=data_dir.parent, port=port, stub_url=stub, repo=repo)
        self.info = info
        if project.setup:  # e.g. an outside database emptied, so every run starts the same
            project.setup(info)
        env = {**os.environ, "PARITYRUN_STUB": stub, **measure, **(project.env(info) if project.env else {}),
               **{k: str(v).replace("{stub}", stub).replace("{python}", sys.executable).replace("{repo}", str(repo))
                  .replace("{data}", data_dir.as_posix()) for k, v in (extra or {}).items()}}
        self.env = env
        out = open(log, "w", encoding="utf-8")  # noqa: SIM115 - closed with the process
        proc = subprocess.Popen(cmd, cwd=self.cwd, env=env, stdout=out, stderr=subprocess.STDOUT)
        base = f"http://127.0.0.1:{port}"
        deadline = time.time() + project.ready_timeout
        accept = accept or (lambda status: status == 200)
        while time.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(f"{self.name} exited at start; see {log}")
            try:
                if accept(httpx.get(f"{base}{project.ready}", headers=project.headers, timeout=30).status_code):
                    return proc, base
            except httpx.HTTPError:
                pass
            time.sleep(0.3)
        proc.kill()
        raise RuntimeError(f"{self.name} didn't answer in {project.ready_timeout:g} s; see {log}")

    def cli(self, args: list[str]) -> dict:
        """One command-line run next to the running service (same data, same environment): exit code and output."""
        if "cli" not in self.spec:
            raise RuntimeError(f"{self.name} has no cli in impls.json")
        cmd = _command([part.format(python=sys.executable, repo=self.project.repo) for part in self.spec["cli"]]) + args
        proc = subprocess.run(cmd, cwd=self.cwd, env={**self.env, "COLUMNS": "100"}, capture_output=True, timeout=900)
        text = lambda b: b.decode("utf-8", "replace").replace("\r\n", "\n")  # noqa: E731
        return {"exit": proc.returncode, "stdout": text(proc.stdout), "stderr": text(proc.stderr)}


def _command(cmd: list[str]) -> list[str]:
    """A bare program name found on PATH, so Windows starts `npm` (npm.cmd) and the like too."""
    if cmd and "/" not in cmd[0] and "\\" not in cmd[0]:
        return [shutil.which(cmd[0]) or cmd[0], *cmd[1:]]
    return cmd


def copy_app(impl: Implementation, target: pathlib.Path) -> dict[str, tuple[int, int]]:
    """The implementation's folder copied to `target` for one run (Project.isolate), without .git and the like or the
    parityrun project folder; returns each file's (size, mtime) to tell later what the run changed."""
    skip, root = set(impl.project.isolate_skip), impl.project.root.resolve()
    ignore = lambda folder, names: [n for n in names  # noqa: E731
                                    if n in skip or pathlib.Path(folder, n).resolve() == root]
    shutil.copytree(impl.folder, target, ignore=ignore, symlinks=True)
    return {p.relative_to(target).as_posix(): _stamp(p) for p in target.rglob("*") if p.is_file()}


def _stamp(path: pathlib.Path) -> tuple[int, int]:
    st = path.stat()
    return st.st_size, st.st_mtime_ns


def app_changes(impl: Implementation, copy: pathlib.Path, baseline: dict) -> tuple[set[str], set[str]]:
    """The files a run created or changed in its copy, and the ones it deleted (a rewrite to the same bytes isn't)."""
    now = {p.relative_to(copy).as_posix(): p for p in copy.rglob("*") if p.is_file()}
    changed = set()
    for rel, path in now.items():
        if rel not in baseline:
            changed.add(rel)
        elif _stamp(path) != baseline[rel] and path.read_bytes() != (impl.folder / rel).read_bytes():
            changed.add(rel)
    return changed, set(baseline) - set(now)


def _resolve(expr: str, ctx: dict):
    value = ctx
    for part in re.findall(r"[\w-]+", expr):
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


def render(value, ctx: dict):
    """{{step.path}} -> a value from an earlier step's JSON response (the whole string keeps its type)."""
    if isinstance(value, str):
        whole = TEMPLATE.fullmatch(value.strip())
        if whole:
            return _resolve(whole.group(1), ctx)
        return TEMPLATE.sub(lambda m: str(_resolve(m.group(1), ctx)), value)
    if isinstance(value, list):
        return [render(v, ctx) for v in value]
    if isinstance(value, dict):
        if "$merge" in value:  # an earlier response sent back with some fields changed
            base = render(value["$merge"], ctx)
            return {**base, **{k: render(v, ctx) for k, v in value.items() if k != "$merge"}}
        return {k: render(v, ctx) for k, v in value.items()}
    return value


def _path_value(data, path: str):
    """"worker.busy", "tasks[0].status", "[1].id" in a JSON response; None when it isn't there."""
    try:
        for part in re.findall(r"[\w-]+", path):
            data = data[int(part)] if isinstance(data, list) else data[part]
        return data
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def _met(data, until: dict) -> bool:
    value = _path_value(data, until["path"]) if until.get("path") else data
    if "equals" in until:
        return value == until["equals"]
    if "in" in until:
        return value in until["in"]
    if "not" in until:
        return value != until["not"]
    if "length" in until:
        return isinstance(value, list) and len(value) == until["length"]
    raise ValueError(f"until needs equals, in, not or length: {until}")


def measured(value, keys: set[str], companions: set[str] = frozenset()):
    """Blank measured numbers (and a number computed from them, in the same object) as "<measured>"."""
    if isinstance(value, dict):
        timed = any(k in keys and isinstance(v, (int, float)) for k, v in value.items())
        return {k: "<measured>" if (k in keys or (timed and k in companions)) and isinstance(v, (int, float))
                and not isinstance(v, bool) else measured(v, keys, companions) for k, v in value.items()}
    if isinstance(value, list):
        return [measured(v, keys, companions) for v in value]
    return value


def _blank(data, parts: list[str]) -> None:
    """Replace the value at a dotted path ("tools.*.running", "gpus.*.driver") with "<ignored>"; * is any key."""
    if not parts or not isinstance(data, (dict, list)):
        return
    head, rest = parts[0], parts[1:]
    keys = (range(len(data)) if isinstance(data, list) else list(data)) if head == "*" else \
        ([int(head)] if isinstance(data, list) and head.isdigit() else [head])
    for key in keys:
        try:
            if rest:
                _blank(data[key], rest)
            elif isinstance(data, list) or key in data:
                data[key] = "<ignored>"
        except (KeyError, IndexError, TypeError):
            pass


def _form_value(value):
    """A form field as a browser sends it: a ticked checkbox (YAML's on/yes/true) is "on", an empty one isn't sent."""
    if isinstance(value, list):
        return [_form_value(v) for v in value]
    if value is True:
        return "on"
    return "" if value is None else str(value)


def _request(client: httpx.Client, step: dict, ctx: dict, fixtures: pathlib.Path) -> httpx.Response:
    method, path = step["call"].split(" ", 1)
    path = render(path, ctx)
    follow = bool(step.get("follow"))  # a browser following redirects to the page they end on
    if "raw" in step:  # a file sent as the body, e.g. an upload
        return client.request(method, path, content=(fixtures / step["raw"]).read_bytes(),
                              headers={"Content-Type": "application/octet-stream"}, follow_redirects=follow)
    headers = step.get("headers") or {}  # e.g. Range: a player seeking in a video
    if "form" in step or "upload" in step:  # a web form: urlencoded, or multipart with files from fixtures
        form = {k: _form_value(v) for k, v in (render(step.get("form"), ctx) or {}).items() if v is not False}
        files = {field: (pathlib.Path(name).name, (fixtures / name).read_bytes())
                 for field, name in (step.get("upload") or {}).items()}
        return client.request(method, path, data=form, files=files or None, headers=headers,
                              follow_redirects=follow)
    body = render(step.get("body"), ctx)
    return client.request(method, path, json=body, headers=headers, follow_redirects=follow) if body is not None \
        else client.request(method, path, headers=headers, follow_redirects=follow)


def _capture(r: httpx.Response, captures: dict, kind: str, where: str) -> dict:
    """Values for later steps' templates: a regex's first group (or whole match) in the body, or in a header."""
    found = {}
    for name, rule in captures.items():
        rule = rule if isinstance(rule, dict) else {"regex": rule}
        text = r.headers.get(rule["header"], "") if "header" in rule else r.text
        match = re.search(rule["regex"], text)
        if not match:
            raise RuntimeError(f"{where}: capture {name} found nothing for {rule['regex']!r} in {text[:500]!r}")
        value = next((g for g in match.groups() if g is not None), match.group(0))  # alternatives: the one that matched
        found[name] = html.unescape(value) if kind == "text/html" and "header" not in rule else value
    return found


def _listen(url: str, headers: dict, count: int, found: list, ready: threading.Event) -> None:
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


def _comma_split(value) -> list[str]:
    """Keys a YAML flow mapping made from an unquoted comma ({title: a, b} -> {"title": "a", "b": None})."""
    if isinstance(value, dict):
        return [k for k, v in value.items() if v is None and isinstance(k, str) and " " in k] + \
            [k for v in value.values() for k in _comma_split(v)]
    if isinstance(value, list):
        return [k for v in value for k in _comma_split(v)]
    return []


def load(project: Project, name: str) -> dict:
    scenario = yaml.safe_load((project.scenarios / f"{name}.yaml").read_text(encoding="utf-8"))
    split = _comma_split(scenario["steps"])
    if split:
        raise ValueError(f"{name}: quote the text with commas in it; YAML split it at {split}")
    return scenario


def all_scenarios(project: Project) -> list[str]:
    return sorted(p.stem for p in project.scenarios.glob("*.yaml"))


def _entry_calls(entry: dict, stub: Stub) -> None:
    requests = stub.take()
    if requests:
        entry["llm"] = requests
    calls = stub.take_platform()
    if calls:
        entry["platform"] = calls


def run(impl: Implementation, name: str, out: pathlib.Path, strict: bool = True) -> dict:
    """One scenario on a fresh service. Returns the normalised recording; pictures and frames go to `out`."""
    project = impl.project
    fixtures = project.fixtures
    scenario = load(project, name)
    work = pathlib.Path(tempfile.mkdtemp(prefix=f"{project.work_prefix}{name}-"))
    data_dir = work / "data"
    data_dir.mkdir()
    stub = Stub(project.stub_handler)
    for rel, fixture in (scenario.get("files") or {}).items():  # state a service can't be given over HTTP
        target = data_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if (fixtures / fixture).suffix in (".png", ".jpg"):  # a picture, as it is
            target.write_bytes((fixtures / fixture).read_bytes())
            continue
        text = (fixtures / fixture).read_text(encoding="utf-8").replace("{stub}", stub.url)
        target.write_text(text.replace("{data}", data_dir.as_posix()), encoding="utf-8")
    env = dict(scenario.get("env") or {})
    if any("{fake_claude}" in str(v) for v in env.values()):
        exe = _fake_claude(work / "bin")
        env = {k: str(v).replace("{fake_claude}", exe) for k, v in env.items()}
    app = work / "app" if project.isolate else None  # a copy of the implementation's folder: it may write anywhere
    baseline = copy_app(impl, app) if app else {}
    proc, base = impl.start(data_dir, stub.port, work / "service.log", env, cwd=app)
    steps, ctx, captured = [], {"stub": stub.url}, {}
    try:
        client = httpx.Client(base_url=base, headers=project.headers, timeout=180)
        for k, step in enumerate(scenario["steps"]):
            stub.queue([json.loads((fixtures / r["$file"]).read_text(encoding="utf-8"))
                        if isinstance(r, dict) and "$file" in r else r for r in step.get("llm") or []])
            for key in project.stub_queues:
                stub.feed(key, render(step.get(key) or [], ctx))
            events: list = []
            if step.get("sse"):  # listen to the events while the step's call runs
                if not project.events:
                    raise ValueError(f"{name} step {k}: sse needs the project's events path")
                ready = threading.Event()
                listener = threading.Thread(target=_listen, args=(base + project.events, project.headers,
                                                                  int(step["sse"]), events, ready), daemon=True)
                listener.start()
                ready.wait(10)
                time.sleep(0.3)
            if "cli" in step:  # a command-line run instead of an HTTP call
                entry = {"call": f"cli {step['cli']}", "cli": impl.cli(shlex.split(render(step["cli"], ctx)))}
                _entry_calls(entry, stub)
                steps.append(entry)
                continue
            r = _request(client, step, ctx, fixtures)
            if "until" in step:  # poll until a background task is done; the last answer is what's compared
                deadline = time.time() + float(step.get("timeout", 300))
                while not (r.headers.get("content-type", "").startswith("application/json")
                           and _met(r.json(), step["until"])):
                    if time.time() > deadline:
                        # the whole answer: an error and a log tail are what explain a stuck step
                        raise RuntimeError(f"{name} step {k} ({step['call']}): {step['until']} not met in time; "
                                           f"last answer {r.text[:4000]}")
                    time.sleep(float(step.get("every", 1)))
                    r = _request(client, step, ctx, fixtures)
            if step.get("sse"):
                listener.join(15)
            kind = r.headers.get("content-type", "").split(";")[0]
            if step.get("capture"):  # e.g. a CSRF token in a form: random, so recorded as <name>
                found = _capture(r, step["capture"], kind, f"{name} step {k} ({step['call']})")
                ctx.update(found)
                captured.update({value: f"<{key}>" for key, value in found.items() if len(value) >= 8})
            entry = {"call": step["call"], "status": r.status_code}
            if project.redirects and 300 <= r.status_code < 400 and "location" in r.headers:
                entry["location"] = r.headers["location"]
            if step.get("follow"):  # the redirects on the way to the page the step ends on
                entry["redirects"] = [[h.status_code, h.headers.get("location", "")] for h in r.history]
            if events:
                entry["events"] = events
            if kind == "application/json":
                entry["json"] = r.json()
                ctx[step.get("id", f"step{k}")] = entry["json"]
            elif kind.startswith("image/"):
                key = f"response-{k}.png"  # kept as a small PNG (snapshot.shrink)
                snapshot.shrink(r.content, out / key)
                entry["image"] = key
            elif r.status_code == 206:  # a byte range: which bytes of how many (the file's size is the encoder's)
                entry["range"] = {"content_range": r.headers.get("content-range", "").rsplit("/", 1)[0],
                                  "bytes": len(r.content), "type": kind}
            elif kind.startswith("video/"):  # kept as its duration, size and frames (response-k#frame<n>.png)
                clip = work / f"response-{k}.mp4"
                clip.write_bytes(r.content)
                entry["video"], frames = snapshot.video(clip, out / f"response-{k}")
                entry["frames"] = [f.name for f in frames]
            elif project.html and (kind == "text/html" or not r.content):  # as a browser reads it; an empty
                entry["html"] = htmlnorm.lines(r.text)  # answer (a redirect) is an empty page whatever its type
            else:
                entry["text"] = r.text
            if r.headers.get("content-disposition"):
                entry["disposition"] = r.headers["content-disposition"]
            _entry_calls(entry, stub)
            steps.append(entry)
        if stub.unused() and strict:  # recording: an authoring error; checking: the missing prompts fail anyway
            raise RuntimeError(f"{name}: {stub.unused()} queued LLM replies were never asked for; fix the scenario")
        if project.shutdown:
            method, path = project.shutdown.split(" ", 1)
            try:
                client.request(method, path)
            except httpx.HTTPError:
                pass
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                pass
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        stub.close()
    measured_lines = coverage.collect(work / "coverage", impl.cwd) if project.coverage else None
    if measured_lines:  # beside the recording, not in it: record keeps it as golden/<name>/coverage.json
        out.mkdir(parents=True, exist_ok=True)
        (out / "coverage.json").write_text(json.dumps(measured_lines, indent=1) + "\n", encoding="utf-8")
    if project.snapshot:  # an outside database (MySQL, PostgreSQL, ...) read by the project
        db = project.snapshot(impl.info)
    else:  # in the data folder, or (isolated) where the app keeps it in its own folder
        db_file = next((folder / project.databases[0] for folder in (data_dir, app) if folder
                        and (folder / project.databases[0]).exists()), None) if project.databases else None
        db = snapshot.database(db_file) if db_file else {}
    skip = {f"{pathlib.PurePosixPath(n).name}{s}" for n in project.databases for s in ("", "-wal", "-shm", "-journal")}
    kinds = {"skip": skip, "random": project.random_files, "interleaved": project.interleaved_files,
             "ignore": project.ignore_files}
    files, images = snapshot.files(data_dir, **kinds)
    if app:  # what the run created, changed or deleted in its copy of the app, as app/...
        changed, deleted = app_changes(impl, app, baseline)
        more_files, more_images = snapshot.files(app, work / "frames", only=changed, **kinds)
        files.update({f"app/{k}": v for k, v in more_files.items()})
        images.update({f"app/{k}": v for k, v in more_images.items()})
        files.update({f"app/{rel}": {"deleted": True} for rel in sorted(deleted)
                      if not snapshot.ignored(rel, project.ignore_files)})
    for prefix, folder in project.extra_roots.items():  # folders the service writes outside its data folder
        if (work / folder).exists():
            more_files, more_images = snapshot.files(work / folder, work / "frames", **kinds)
            files.update({f"{prefix}/{k}": v for k, v in more_files.items()})
            images.update({f"{prefix}/{k}": v for k, v in more_images.items()})
    port = base.rsplit(":", 1)[1]
    labels = project.labels
    norm = Normalizer({str(data_dir): labels["data"], data_dir.as_posix(): labels["data"], str(work): labels["work"],
                       work.as_posix(): labels["work"], f"127.0.0.1:{stub.port}": labels["stub"],
                       f"127.0.0.1:{port}": labels["service"], f"localhost:{port}": labels["service"]})
    raw = {"steps": steps, "db": db, "files": files}
    norm.collect(raw)
    norm.add({value: label for value, label in captured.items() if value not in norm.ids})  # ids keep their numbers
    recording = measured(norm.apply(raw), project.measured_keys, project.measured_companions)
    if project.post_process:
        project.post_process(recording)
    for entry, step in zip(recording["steps"], scenario["steps"]):
        for pattern in (scenario.get("ignore") or []) + (step.get("ignore") or []):  # machine state, not code
            if "json" in entry:
                _blank(entry["json"], pattern.split("."))
        if step.get("unordered") and isinstance(entry.get("json"), list):  # e.g. a log parallel work shares
            entry["json"].sort(key=lambda x: json.dumps(x, sort_keys=True))
    for rel, path in images.items():  # keep the images under their normalised names
        target = out / "files" / safe(norm.text(rel))
        target.parent.mkdir(parents=True, exist_ok=True)
        snapshot.shrink(path, target)
    shutil.rmtree(work, ignore_errors=True)
    return recording


def _fake_claude(folder: pathlib.Path) -> str:
    """A real executable for fake_claude (pip's launcher): every argument arrives intact, newlines included."""
    from pip._vendor.distlib.scripts import ScriptMaker

    folder.mkdir(parents=True, exist_ok=True)
    maker = ScriptMaker(None, str(folder))
    maker.executable = sys.executable
    maker.variants = {""}
    return maker.make("fake_claude = parityrun.fake_claude:main")[0]


def safe(rel: str) -> str:
    """A normalised path as a file name Windows accepts ("<id:3>" -> "(id-3)")."""
    return rel.replace("<", "(").replace(">", ")").replace(":", "-")


def _flatten(value, prefix: str = "") -> dict:
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            out.update(_flatten(v, f"{prefix}.{k}" if prefix else k))
        return out
    if isinstance(value, list):
        out = {}
        for i, v in enumerate(value):
            out.update(_flatten(v, f"{prefix}[{i}]"))
        return out or {prefix: []}
    return {prefix: value}


def differences(a, b, limit: int = 8) -> list[str]:
    fa, fb = _flatten(a), _flatten(b)
    diffs = [f"{k}: {fa.get(k, '<missing>')!r} != {fb.get(k, '<missing>')!r}"
             for k in sorted(set(fa) | set(fb)) if fa.get(k, "<missing>") != fb.get(k, "<missing>")]
    return diffs[:limit] + ([f"... {len(diffs) - limit} more"] if len(diffs) > limit else [])


TRACEBACK = re.compile(r"\nTraceback \(most recent call last\):\n.*\Z", re.S)


def _no_tracebacks(value):
    """A failed job's error ends with the implementation's own stack trace: compare everything before it."""
    if isinstance(value, dict):
        return {k: _no_tracebacks(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_no_tracebacks(v) for v in value]
    if isinstance(value, str) and "\nTraceback (most recent call last):\n" in value:
        return TRACEBACK.sub("\n<traceback>", value)
    return value


def checks(golden: dict, got: dict, golden_dir: pathlib.Path, got_dir: pathlib.Path,
           project: Project | None = None) -> list[dict]:
    """Every comparison as {name, ok, detail}: each step's response and prompts, each table, each file."""
    threshold = project.image_ssim if project else IMAGE_SSIM
    interleaved = project.interleaved_files if project else set()
    golden, got = _no_tracebacks(golden), _no_tracebacks(got)
    out = []
    for k, want in enumerate(golden["steps"]):
        have = got["steps"][k] if k < len(got["steps"]) else {}
        for name in want.get("frames", []):
            score = snapshot.ssim(golden_dir / name, got_dir / name) if name in have.get("frames", []) else 0.0
            out.append({"name": f"step {k} {want['call']} {name}", "ok": score >= threshold,
                        "detail": [f"SSIM {score:.4f} (needs {threshold})"]})
        for part in ("status", "location", "redirects", "json", "text", "html", "disposition", "events", "video",
                     "range", "cli"):
            if part in want or part in have:
                out.append({"name": f"step {k} {want['call']} {part}", "ok": want.get(part) == have.get(part),
                            "detail": differences(want.get(part), have.get(part))})
        if "image" in want:
            score = snapshot.ssim(golden_dir / want["image"], got_dir / have["image"]) if "image" in have else 0.0
            out.append({"name": f"step {k} {want['call']} image", "ok": score >= threshold,
                        "detail": [f"SSIM {score:.4f} (needs {threshold})"]})
        if "llm" in want or "llm" in have:
            out.append({"name": f"step {k} {want['call']} prompts", "ok": want.get("llm") == have.get("llm"),
                        "detail": differences(want.get("llm"), have.get("llm"))})
        if "platform" in want or "platform" in have:
            out.append({"name": f"step {k} {want['call']} platform calls",
                        "ok": want.get("platform") == have.get("platform"),
                        "detail": differences(want.get("platform"), have.get("platform"))})
    for table in sorted(set(golden["db"].get("schema", {})) | set(got["db"].get("schema", {}))):
        want, have = golden["db"]["schema"].get(table), got["db"].get("schema", {}).get(table)
        out.append({"name": f"schema {table}", "ok": want == have, "detail": differences(want, have)})
    for table in sorted(set(golden["db"].get("rows", {})) | set(got["db"].get("rows", {}))):
        key = lambda r: json.dumps(r, sort_keys=True)  # noqa: E731 - row order isn't part of the contract
        want = sorted(golden["db"]["rows"].get(table, []), key=key)
        have = sorted(got["db"].get("rows", {}).get(table, []), key=key)
        out.append({"name": f"rows {table}", "ok": want == have, "detail": differences(want, have)})
    for rel in sorted(set(golden["files"]) | set(got["files"])):
        want, have = golden["files"].get(rel), got["files"].get(rel)
        if rel.rsplit("/", 1)[-1] in interleaved:  # sorted again now that measured numbers are blanked
            want, have = (
                {"jsonl": sorted(x["jsonl"], key=lambda r: json.dumps(r, sort_keys=True))} if x and "jsonl" in x else x
                for x in (want, have))
        ok = want == have
        detail = differences(want, have)
        if ok and want and "image" in want:
            score = snapshot.ssim(golden_dir / "files" / safe(rel), got_dir / "files" / safe(rel))
            ok, detail = score >= threshold, [f"SSIM {score:.4f} (needs {threshold})"]
        out.append({"name": f"file {rel}", "ok": ok, "detail": detail})
    return out
