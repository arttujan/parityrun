"""`parityrun port`: port the reference app to another language with the user's own AI tool, turn by turn, until
`check` passes.

It writes the port's folder (a starting point that already runs), its entry in impls.json and PORTING.md (the task:
how the port is started, what it is compared on, the rules). Then it loops: one agent turn, build, check; the failing
checks of one scenario at a time become the next turn's task. It stops at 100 % parity, after --turns turns, or when
every red scenario has gone --patience turns without getting better (those are reported, not hidden). The agent may
not change the parityrun folder (put back after every turn) or the original app (the loop stops). Every turn is
logged in out/port-log.jsonl; its prompt and the agent's output are kept in out/port/.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime
import io
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sysconfig
import tempfile
import time

import httpx

from . import runner
from .project import Project, RunInfo
from .stubs import Stub

PROMPT_CHECKS = 30
"""The most failing checks one turn's prompt lists (the rest are in out/report-<name>.json)."""
SAMPLE_PORT = 8123
"""The port PORTING.md's example environment shows."""
LABELS = {"javascript": "JavaScript", "typescript": "TypeScript", "rust": "Rust"}


@dataclasses.dataclass
class Target:
    """The port: its language, its name in impls.json, its folder and how parityrun builds and starts it."""
    language: str
    name: str
    folder: pathlib.Path
    spec: dict
    """Its impls.json entry."""
    files: dict[str, str]
    """The starting point written into a new folder (never over a file that exists)."""
    tool: str
    """The program that must be on PATH."""
    allowed: list[str]
    """What Claude Code may run on its own: the language's tools."""
    notes: str
    """The language's part of PORTING.md."""


def _relative(folder: pathlib.Path, base: pathlib.Path) -> str:
    try:
        return pathlib.Path(os.path.relpath(folder, base)).as_posix()
    except ValueError:  # another drive
        return folder.as_posix()


def _package(name: str) -> str:
    """A Cargo package name (the program's name too) from a folder name."""
    package = re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-")
    return package if package[:1].isalpha() else f"app-{package}".rstrip("-")


SERVER_JS = """\
// The port of {app}: written by `parityrun port` as a starting point that runs. The task is in PORTING.md.
"use strict";

const http = require("node:http");

http.createServer((req, res) => {{
  res.writeHead(501, {{ "Content-Type": "text/plain; charset=utf-8" }});
  res.end("Not ported yet\\n");
}}).listen(Number(process.env.PORT), "127.0.0.1");
"""

SERVER_TS = """\
// The port of {app}: written by `parityrun port` as a starting point that runs. The task is in PORTING.md.
import http from "node:http";

http.createServer((req: http.IncomingMessage, res: http.ServerResponse) => {{
  res.writeHead(501, {{ "Content-Type": "text/plain; charset=utf-8" }});
  res.end("Not ported yet\\n");
}}).listen(Number(process.env.PORT), "127.0.0.1");
"""

MAIN_RS = """\
// The port of {app}: written by `parityrun port` as a starting point that runs. The task is in PORTING.md.
use std::io::{{Read, Write}};
use std::net::TcpListener;

fn main() {{
    let port = std::env::var("PORT").expect("parityrun gives the port in PORT");
    let listener = TcpListener::bind(format!("127.0.0.1:{{port}}")).expect("port in use");
    for stream in listener.incoming() {{
        let mut stream = stream.expect("a connection");
        let mut request = [0u8; 8192];
        let _ = stream.read(&mut request);
        let body = "Not ported yet\\n";
        let _ = write!(stream, "HTTP/1.1 501 Not Implemented\\r\\nContent-Type: text/plain; charset=utf-8\\r\\n\\
                               Content-Length: {{}}\\r\\nConnection: close\\r\\n\\r\\n{{body}}", body.len());
    }}
}}
"""

NODE_NOTES = """\
- Use node's own modules where they do (`node:http`, `node:fs`, `node:crypto`, `node:path`). npm packages are
  fine too: `npm install <package>` in this folder; parityrun uses node_modules/ as it is."""

NOTES = {
    "javascript": """\
parityrun starts it with `node server.js` (CommonJS: package.json has no `type`).
""" + NODE_NOTES,
    "typescript": """\
parityrun starts it with `node server.ts`: node 23.6 or newer runs TypeScript as it is by stripping the types, with
no build step. So:
- use only syntax that can be stripped: no `enum`, `namespace`, parameter properties or `import x = require()`;
- import your own files with their .ts extension (`import { db } from "./db.ts"`); package.json has `type: module`.
""" + NODE_NOTES,
    "rust": """\
parityrun builds it with `cargo build --release` in this folder and starts `target/release/{package}`. Crates are
fine: add them to Cargo.toml. Keep the package name `{package}`: it is the program's name.""",
}


def plan(project: Project, language: str, into: str | None = None, name: str | None = None) -> Target:
    """Where the port goes and how it runs; nothing is written yet."""
    reference = runner.Implementation(project, project.reference).folder.resolve()
    short, suffix = {"javascript": ("js", "js"), "typescript": ("ts", "ts"), "rust": ("rust", "rs")}[language]
    folder = pathlib.Path(into).resolve() if into else reference.parent / f"{reference.name}-{suffix}"
    cwd = _relative(folder, project.repo)
    app = reference.name
    if language == "rust":
        package = _package(folder.name)
        program = f"{cwd}/target/release/{package}" if pathlib.PurePosixPath(cwd).is_absolute() or ":" in cwd \
            else f"{{repo}}/{cwd}/target/release/{package}"
        spec = {"cwd": cwd, "cmd": [program], "env": {"PORT": "{port}"},
                "build": ["cargo", "build", "--release", "--quiet"], "skip": ["target"]}
        files = {"Cargo.toml": f'[package]\nname = "{package}"\nversion = "0.1.0"\nedition = "2021"\npublish = false\n'
                               "\n[dependencies]\n\n"
                               "# Its own workspace: a Cargo.toml in a folder above doesn't take it in.\n[workspace]\n",
                 "src/main.rs": MAIN_RS.format(app=app), ".gitignore": "target/\n"}
        return Target(language, name or short, folder, spec, files, "cargo", ["Bash(cargo *)"],
                      NOTES["rust"].format(package=package))
    entry = "server.ts" if language == "typescript" else "server.js"
    package = {"name": _package(folder.name), "private": True, "description": f"The port of {app}: see PORTING.md",
               **({"type": "module"} if language == "typescript" else {}), "scripts": {"start": f"node {entry}"}}
    files = {"package.json": json.dumps(package, indent=2) + "\n",
             entry: (SERVER_TS if language == "typescript" else SERVER_JS).format(app=app),
             ".gitignore": "node_modules/\n"}
    spec = {"cwd": cwd, "cmd": ["node", entry], "env": {"PORT": "{port}"}}
    return Target(language, name or short, folder, spec, files, "node", ["Bash(node *)", "Bash(npm *)"],
                  NOTES[language])


def set_up(project: Project, target: Target) -> bool:
    """Write the starting point (no file that exists is changed) and the impls.json entry; True when the folder
    holds nothing but the starting point, so the first turn ports the whole app."""
    path = project.root / "impls.json"
    spec = json.loads(path.read_text(encoding="utf-8"))
    if target.name in spec:
        there = (project.repo / spec[target.name].get("cwd", ".")).resolve()
        if there != target.folder:
            raise SystemExit(f"impls.json has {target.name!r} already, for {there}: choose another --name")
    else:
        spec[target.name] = target.spec  # one implementation a line
        path.write_text("{\n" + ",\n".join(f"  {json.dumps(k)}: {json.dumps(v, ensure_ascii=False)}"
                                           for k, v in spec.items()) + "\n}\n", encoding="utf-8")
    for rel, text in target.files.items():
        file = target.folder / rel
        if not file.exists():
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(text, encoding="utf-8", newline="\n")
    ours = set(target.files) | {"PORTING.md", "package-lock.json", "Cargo.lock"}
    for file in target.folder.rglob("*"):
        rel = file.relative_to(target.folder).as_posix()
        if file.is_file() and not re.match(r"(target|node_modules)/", rel) and (
                rel not in ours or (rel in target.files and file.read_text(encoding="utf-8") != target.files[rel])):
            return False
    return True


def _contract_env(project: Project, target: Target) -> dict[str, str]:
    """The environment every run gives the port, with sample values."""
    sample = RunInfo(data_dir=pathlib.Path("<data>"), work=pathlib.Path("<work>"), port=SAMPLE_PORT,
                     stub_url="<stub>", repo=project.repo)
    try:
        env = {k: str(v) for k, v in (project.env(sample) if project.env else {}).items()}
    except Exception:  # noqa: BLE001 - a project's env may need a real run; PORTING.md then points to it
        env = {}
    env.update({k: str(v).replace("{port}", str(SAMPLE_PORT)) for k, v in target.spec.get("env", {}).items()})
    return {"PARITYRUN_STUB": "<stub>", **env}


def porting_md(project: Project, target: Target, project_file: pathlib.Path | None) -> str:
    """The task for the agent: written into the port's folder, read at the start of every turn."""
    reference = runner.Implementation(project, project.reference)
    root, name = project.root.as_posix(), target.name
    env = "\n".join(f"| `{k}` | `{v}` |" for k, v in _contract_env(project, target).items())
    steps = {n: len(runner.load(project, n)["steps"]) for n in runner.all_scenarios(project)}
    scenarios = "\n".join(f"- `{n}`: {k} step{'s' * (k != 1)} in `{root}/scenarios/{n}.yaml`, the original's answers "
                          f"in `{root}/golden/{n}/recording.json`" for n, k in steps.items())
    compared = ["the status code of every answer"]
    if project.redirects:
        compared.append("the `Location` of every redirect")
    compared.append("pages as normalised lines (`html`): indentation, attribute order, entity spelling and comments "
                    "don't count; tags, attributes and text do" if project.html else "pages and text answers exactly")
    compared.append("JSON answers value by value")
    if project.databases or project.snapshot:
        db = f"the SQLite database `{project.databases[0]}`" if project.databases and not project.snapshot \
            else "the database"
        compared.append(f"{db}: the same tables and columns, and the same rows")
    compared.append("every file the app writes" + (
        " (in its data folder, and in its own folder as `app/<path>`: each run starts from a fresh copy of this "
        "folder" + (f", without {', '.join(target.spec.get('skip', []))}/" if target.spec.get("skip") else "") + ")"
        if project.isolate else " in its data folder"))
    compared.append("every request to an LLM or an outside service (they go to the stub at `PARITYRUN_STUB`)")
    check = f"parityrun check --impl {name}"
    where = f" (`PARITYRUN_PROJECT` points it at `{project_file.as_posix()}`)" if project_file else ""
    language = LABELS[target.language]
    return f"""# Port {reference.folder.name} to {language}

Written by `parityrun port`, which rewrites it every time it runs.

You are porting the app in `{reference.folder.as_posix()}` (started with `{' '.join(reference.spec['cmd'])}`) to
{language}, in this folder. The port must behave exactly like the original: parityrun runs the same
recorded scenarios against it and compares what it does with what the original did. The work is done when
`{check}` reports parity 100 %.

## How your version is started

{target.notes}

Every run gets these environment variables (sample values: `{SAMPLE_PORT}` is the run's own port, `<data>` a fresh
empty data folder, `<stub>` parityrun's stand-in for outside services):

| Variable | Sample |
|---|---|
{env}

Listen on 127.0.0.1 at `PORT`. parityrun waits until `GET {project.ready}` answers 200, then runs the scenario.

## What is compared

{chr(10).join(f'- {line}' for line in compared)}

Random or run-specific values are replaced in recordings by placeholders such as `<id:1>`, `<time>`, `<data>`,
`<service>` or a captured form token like `<csrf>`: produce the real kind of value (a new id, the time, a token).

## The scenarios

{scenarios}

After a check, what your version did is in `{root}/out/{name}/<scenario>/recording.json`: set it beside the
original's to see every difference.

## Check your work

    {check}                 every scenario
    {check} <scenario>      one scenario

Each failing check lists its differences as `path: expected != yours`{where}.

## The order of work

Port what every page shares first (routing, layout, data access), then the routes. After the first turn, each turn
names one red scenario: make it green without breaking the green ones.

## Rules

- Never change anything in `{root}` or in `{reference.folder.as_posix()}`: parityrun puts its folder back after every
  turn and stops when the original app changes.
- Match the original's behaviour exactly, quirks and error pages included: the same URLs, status codes, redirects,
  page text, JSON, database rows and files. The original is the specification; don't fix or improve it.
- Keep this folder runnable after every change.
"""


@dataclasses.dataclass
class Result:
    """One check of the port: passed and total checks per scenario, the failing checks, or why nothing ran."""
    scenarios: dict[str, tuple[int, int]]
    failures: dict[str, list[dict]] = dataclasses.field(default_factory=dict)
    problem: str | None = None
    """The port didn't build or start: no scenario ran."""

    @property
    def passed(self) -> int:
        return sum(p for p, _ in self.scenarios.values())

    @property
    def total(self) -> int:
        return sum(t for _, t in self.scenarios.values())

    @property
    def parity(self) -> float:
        return 100 * self.passed / self.total if self.total else 0.0

    @property
    def red(self) -> list[str]:
        return [n for n, (p, t) in self.scenarios.items() if self.problem or p < t]

    def summary(self) -> str:
        red = ", ".join(f"{n} ({p}/{t})" for n, (p, t) in self.scenarios.items() if n in self.red)
        return f"parity {self.parity:.1f} % ({self.passed}/{self.total})" + (f"; red: {red}" if red else "")


def totals(project: Project, names: list[str]) -> dict[str, int]:
    """How many checks each scenario has: what the golden recording compares against itself."""
    out = {}
    for name in names:
        folder = project.golden / name
        golden = json.loads((folder / "recording.json").read_text(encoding="utf-8"))
        out[name] = len(runner.checks(golden, golden, folder, folder, project))
    return out


def _tail(path: pathlib.Path, lines: int = 30) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return "(no output)"


def starts(impl: runner.Implementation) -> str | None:
    """None when the implementation starts and its ready path answers 200; else why not, with its output."""
    project = impl.project
    work = pathlib.Path(tempfile.mkdtemp(prefix=f"{project.work_prefix}start-"))
    try:
        (work / "data").mkdir()
        app = work / "app" if project.isolate else None
        if app:
            runner.copy_app(impl, app)
        stub = Stub(project.stub_handler)
        try:
            proc, base = impl.start(work / "data", stub.port, work / "service.log", cwd=app,
                                    accept=lambda status: True)
        except RuntimeError as exc:
            stub.close()
            return f"It doesn't start: {exc}. Its output:\n{_tail(work / 'service.log')}"
        try:
            answer = httpx.get(f"{base}{project.ready}", headers=project.headers, timeout=30).status_code
        except httpx.HTTPError as exc:
            answer = str(exc)
        finally:
            proc.kill()
            proc.wait()
            stub.close()
        if answer != 200:
            return (f"It starts, but GET {project.ready} answers {answer}, and parityrun waits for a 200 there before "
                    f"every scenario. Its output:\n{_tail(work / 'service.log')}")
        return None
    finally:
        shutil.rmtree(work, ignore_errors=True)


def evaluate(project: Project, name: str, names: list[str], expected: dict[str, int]) -> Result:
    """Build the port, see that it starts, then check every scenario (quietly: the result is the report)."""
    impl = runner.Implementation(project, name)
    broken = impl.build()
    if broken:
        return Result({n: (0, expected[n]) for n in names}, problem=f"It doesn't build:\n{broken}")
    problem = starts(impl)
    if problem:
        return Result({n: (0, expected[n]) for n in names}, problem=problem)
    from . import cli

    with contextlib.redirect_stdout(io.StringIO()):
        cli.check(project, name, names)
    report = json.loads((project.out / f"report-{name}.json").read_text(encoding="utf-8"))
    result = Result({})
    for scenario in names:
        checks = report.get(scenario, [])
        result.scenarios[scenario] = (sum(c["ok"] for c in checks), len(checks) or expected[scenario])
        failing = [c for c in checks if not c["ok"]]
        for c in failing:
            log = re.search(r"see (.+?service\.log)", " ".join(c["detail"]))
            if c["name"] == "run" and log:  # the run's own output explains a crash
                c["detail"] = c["detail"] + ["Its output:"] + _tail(pathlib.Path(log.group(1))).splitlines()
        if failing:
            result.failures[scenario] = failing
    return result


class Focus:
    """The red scenario the next turn works on: one at a time, in order, set aside after `patience` turns without
    getting better. Stuck when every red one is set aside, or the port hasn't built or started for `patience` turns."""

    def __init__(self, patience: int) -> None:
        self.patience = patience
        self.current: str | None = None
        self.best = 0
        self.stale = 0
        self.broken = 0
        self.set_aside: list[str] = []
        self.stuck = False

    def update(self, result: Result) -> None:
        if result.problem:
            self.broken += 1
            self.stuck = self.broken >= self.patience
            return
        self.broken = 0
        red = result.red
        if self.current in red:
            passed = result.scenarios[self.current][0]
            if passed > self.best:
                self.best, self.stale = passed, 0
            else:
                self.stale += 1
            if self.stale < self.patience:
                return
            self.set_aside.append(self.current)
        left = [n for n in red if n not in self.set_aside]
        self.current = left[0] if left else None
        self.best, self.stale = (result.scenarios[self.current][0] if self.current else 0), 0
        self.stuck = bool(red) and not left


class Guard:
    """What the agent may not change: the parityrun folder (kept aside, put back after every turn; out/ and
    __pycache__ are free) and the original app (its files' sizes and times; a change stops the loop)."""

    def __init__(self, project: Project, reference: pathlib.Path, target: pathlib.Path) -> None:
        self.root, self.reference, self.target = project.root, reference, target
        self.free = {project.out.resolve(), target.resolve()}
        self.skip = set(project.isolate_skip) | {"__pycache__"}
        self.kept = {rel: path.read_bytes() for rel, path in self._files(self.root).items()}
        self.stamps = self._stamps()

    def _files(self, folder: pathlib.Path) -> dict[str, pathlib.Path]:
        found = {}
        for dirpath, dirnames, filenames in os.walk(folder):
            here = pathlib.Path(dirpath)
            dirnames[:] = [d for d in dirnames if d not in self.skip and (here / d).resolve() not in self.free
                           and (folder == self.root or (here / d).resolve() != self.root.resolve())]
            for f in filenames:
                found[(here / f).relative_to(folder).as_posix()] = here / f
        return found

    def _stamps(self) -> dict[str, tuple[int, int]]:
        return {rel: (p.stat().st_size, p.stat().st_mtime_ns) for rel, p in self._files(self.reference).items()}

    def restore(self) -> list[str]:
        """Put the parityrun folder back as it was; the paths that had changed."""
        now = self._files(self.root)
        changed = []
        for rel, data in self.kept.items():
            if rel not in now or now[rel].read_bytes() != data:
                (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
                (self.root / rel).write_bytes(data)
                changed.append(rel)
        for rel in sorted(set(now) - set(self.kept)):
            now[rel].unlink()
            changed.append(rel)
        return sorted(changed)

    def original_changed(self) -> list[str]:
        """The original app's files that were added, changed or deleted."""
        now = self._stamps()
        return sorted(rel for rel in set(now) | set(self.stamps) if now.get(rel) != self.stamps.get(rel))


def agent_command(agent: str, target: Target, reads: list[pathlib.Path], prompt_file: pathlib.Path) -> list[str]:
    """The agent's command line. Claude Code works in the port's folder, may read the original and the parityrun
    folder, edit files and run the language's tools and `parityrun check`; any other tool is the user's command line,
    with {prompt_file} and {folder} filled in. Every tool gets the prompt on stdin too."""
    if agent == "claude":
        allowed = ["Read", "Edit", "Write", "Glob", "Grep", "Bash(parityrun check)", "Bash(parityrun check *)",
                   *target.allowed]
        cmd = ["claude", "-p", "--permission-mode", "acceptEdits", "--allowedTools", *allowed]
        for folder in reads:
            cmd += ["--add-dir", str(folder)]
        return runner._command(cmd)
    return runner._command([part.replace("{prompt_file}", str(prompt_file)).replace("{folder}", str(target.folder))
                            for part in shlex.split(agent)])


def run_agent(cmd: list[str], prompt: str, target: Target, env: dict, log: pathlib.Path,
              minutes: float) -> int | str:
    """One agent turn in the port's folder, its output into `log`: the exit code, or "timeout"."""
    with open(log, "w", encoding="utf-8") as out:
        try:
            return subprocess.run(cmd, cwd=target.folder, env=env, input=prompt.encode("utf-8"), stdout=out,
                                  stderr=subprocess.STDOUT, timeout=minutes * 60).returncode
        except subprocess.TimeoutExpired:
            return "timeout"
        except OSError as exc:
            raise SystemExit(f"Couldn't start the agent ({cmd[0]}): {exc}") from exc


def prompt(turn: int, turns: int, project: Project, target: Target, result: Result | None, focus: Focus,
           restored: list[str]) -> str:
    """One turn's task: the whole app first; then how far the port is and one red scenario's failing checks."""
    check = f"parityrun check --impl {target.name}"
    head = f"Turn {turn} of at most {turns}. PORTING.md in this folder has the task and the rules: read it first.\n\n"
    if restored:
        head += ("parityrun put back what the last turn changed in its folder, which you may not change: "
                 f"{', '.join(restored[:20])}.\n\n")
    if result is None:
        return head + ("Port the whole app now: first what every page shares (routing, layout, data access), then "
                       f"every route. Once it runs, run `{check}` and fix what it reports.\n")
    head += f"Where the port is now: {result.summary()}.\n\n"
    if result.problem:
        return head + f"Nothing could be checked. {result.problem}\n\nFix that first, then run `{check}`.\n"
    name, root = focus.current, project.root.as_posix()
    if name is None:
        return head + f"Run `{check}` and fix what it reports.\n"
    green = [n for n in result.scenarios if n not in result.red]
    lines = [f"This turn: make scenario `{name}` pass" + (f", and keep {', '.join(green)} green." if green else "."),
             f"  its steps:              {root}/scenarios/{name}.yaml",
             f"  the original's answers: {root}/golden/{name}/recording.json",
             f"  yours, last check:      {root}/out/{target.name}/{name}/recording.json",
             "", "Its failing checks (each difference is `path: expected != yours`):"]
    failing = result.failures.get(name, [])
    for c in failing[:PROMPT_CHECKS]:
        lines.append(f"  FAIL {c['name']}")
        lines += [f"       {line}" for line in c["detail"][:40 if c["name"] == "run" else 8]]
    if len(failing) > PROMPT_CHECKS:
        lines.append(f"  ... {len(failing) - PROMPT_CHECKS} more in {root}/out/report-{target.name}.json")
    lines += ["", f"Check it with `{check} {name}`, then every scenario with `{check}`."]
    return head + "\n".join(lines) + "\n"


def port(project: Project, language: str, into: str | None = None, name: str | None = None, agent: str = "claude",
         turns: int = 20, patience: int = 3, turn_minutes: float = 60, setup_only: bool = False,
         project_file: pathlib.Path | None = None) -> int:
    """Set the port up, then loop agent turns until parity is 100 %: 0 then, 1 when it stops short, 2 when the agent
    changed the original app."""
    names, turns = runner.all_scenarios(project), max(turns, 1)
    if not names:
        raise SystemExit("No scenarios yet: run `parityrun capture` (or write scenarios/), then `parityrun record`.")
    missing = [n for n in names if not (project.golden / n / "recording.json").exists()]
    if missing:
        raise SystemExit(f"Record first: `parityrun record` ({', '.join(missing)} not recorded yet).")
    target = plan(project, language, into, name)
    fresh = set_up(project, target)
    (target.folder / "PORTING.md").write_text(porting_md(project, target, project_file), encoding="utf-8",
                                              newline="\n")
    print(f"The {LABELS[target.language]} port is in {target.folder} "
          f"({'new' if fresh else 'going on from what is there'}); impls.json calls it {target.name}.")
    if setup_only:
        print(f"Wrote PORTING.md there, the task for your AI tool. Check the port with: parityrun check --impl "
              f"{target.name}")
        return 0
    if not shutil.which(target.tool):
        raise SystemExit(f"{target.tool} isn't installed (or not on PATH): the port can't run without it.")
    if agent == "claude" and not shutil.which("claude"):
        raise SystemExit("Claude Code isn't installed (https://claude.com/claude-code), or use your own AI tool: "
                         '--agent "yourtool ... {prompt_file}"')
    reference = runner.Implementation(project, project.reference).folder.resolve()
    logs = project.out / "port"
    logs.mkdir(parents=True, exist_ok=True)
    scripts = [sysconfig.get_path("scripts")] + ([os.path.dirname(shutil.which("parityrun"))]
                                                  if shutil.which("parityrun") else [])
    env = {**os.environ, "PATH": os.pathsep.join(scripts + [os.environ.get("PATH", "")]),
           **({"PARITYRUN_PROJECT": str(project_file)} if project_file else {})}
    expected = totals(project, names)
    guard = Guard(project, reference, target.folder)
    focus = Focus(patience)
    result = None if fresh else evaluate(project, target.name, names, expected)
    if result:
        print(f"Now: {result.summary()}")
        if not result.red:
            print(f"{target.name}: parity 100 % already.")
            return 0
        focus.update(result)
    started, restored = time.time(), []
    try:
        for turn in range(1, turns + 1):
            task = prompt(turn, turns, project, target, result, focus, restored)
            prompt_file = logs / f"turn-{turn}.prompt.md"
            prompt_file.write_text(task, encoding="utf-8", newline="\n")
            working_on = focus.current if result else None
            print(f"Turn {turn}: " + ("porting the whole app" if result is None else
                                      f"working on {working_on}" if working_on else "fixing what fails")
                  + " ...", flush=True)
            began = time.time()
            exit_code = run_agent(agent_command(agent, target, [reference, project.root], prompt_file), task,
                                  target, env, logs / f"turn-{turn}.log", turn_minutes)
            seconds = time.time() - began
            restored = guard.restore()
            touched = guard.original_changed()
            if touched:
                print(f"Stopped: the agent changed the original app ({', '.join(touched[:10])}). Undo that (e.g. "
                      f"git checkout) before porting on.")
                return 2
            result = evaluate(project, target.name, names, expected)
            focus.update(result)
            with open(project.out / "port-log.jsonl", "a", encoding="utf-8") as log:
                log.write(json.dumps({
                    "time": datetime.datetime.now().isoformat(timespec="seconds"), "impl": target.name, "turn": turn,
                    "focus": working_on, "agent_exit": exit_code, "agent_seconds": round(seconds, 1),
                    "parity": round(result.parity, 2), "passed": result.passed, "total": result.total,
                    "scenarios": {n: list(v) for n, v in result.scenarios.items()}, "problem": result.problem,
                    "restored": restored}, ensure_ascii=False) + "\n")
            agent_said = "" if exit_code == 0 else f", the agent exited with {exit_code}"
            print(f"  turn {turn}: {seconds / 60:.1f} min{agent_said}; " + (
                "it doesn't build or start" if result.problem else result.summary()), flush=True)
            if restored:
                print(f"  put back in {project.root.name}/: {', '.join(restored[:10])}")
            if not result.red:
                print(f"\n{target.name}: parity 100 % after {turn} turn{'s' * (turn != 1)} "
                      f"({(time.time() - started) / 60:.0f} min). The port is in {target.folder}; check it any time "
                      f"with: parityrun check --impl {target.name}")
                return 0
            if focus.stuck:
                break
    finally:
        guard.restore()
    why = (f"{turns} turns are used up" if not focus.stuck else
           f"it hasn't built or started for {patience} turns" if result.problem else
           f"{', '.join(focus.set_aside)} stayed red for {patience} turns each without getting better")
    print(f"\nStopped: {why}. {target.name}: {result.summary()}.\nWhat fails: {project.out}/report-{target.name}.json; "
          f"every turn: {project.out}/port-log.jsonl. Run the same `parityrun port` again to go on from here.")
    return 1
