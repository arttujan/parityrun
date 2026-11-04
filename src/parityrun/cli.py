"""The parityrun command: record golden runs from the reference, check any implementation against them.

It also lists the endpoints the scenarios don't use yet.

  parityrun                                                        set up (init) and record what isn't yet, then show
                                                                   where the project stands and the command to run next
  parityrun [--project FILE] record [scenario ...]                 new golden recordings from the reference (run twice:
                                                                   anything that differs between the runs is reported)
  parityrun [--project FILE] check [--quick | --slow] [--impl NAME] [scenario ...]
                                                                   compare an implementation with the recordings
  parityrun [--project FILE] coverage                              which endpoints (golden/endpoints.json) are used
  parityrun init [PATH] [--force] [--no-start]                     set parityrun up in an existing app's folder
  parityrun [--project FILE] routes                                read the app's routes from its code
  parityrun [--project FILE] capture [NAME] [--port 8000]          use the app in a browser; it becomes a scenario
  parityrun [--project FILE] port --to typescript|javascript|rust [--agent claude|"CMD {prompt_file}"]
                                                                   port the app with an AI tool until check passes
                                                                   (ts, js and rs for short)

FILE (default $PARITYRUN_PROJECT, else parityrun_project.py, else parityrun/parityrun_project.py) defines
PROJECT = parityrun.Project(...). With none of them, every command but init sets parityrun up first (init); the
bare command, check and port record the scenarios that have no recording yet before they go on.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import sys
from typing import TYPE_CHECKING, cast

from . import __version__, api, routes, runner
from . import coverage as code_coverage
from .capture import capture, covered_routes, write_endpoints
from .commands import check, coverage, covered, last_parity, record, templates, unrecorded
from .init import init
from .port import Options, port
from .project import Project, load

if TYPE_CHECKING:
    import io
    from collections.abc import Callable

LANGUAGES = ("javascript", "typescript", "rust")
SHORT = {"js": "javascript", "ts": "typescript", "rs": "rust"}
SHORT_OF = {long: short for short, long in SHORT.items()}
PORTS = {"js": "javascript", "ts": "typescript", "rust": "rust"}  # the names `port` gives, and their languages


def language(text: str) -> str:
    """Return a port's language, also by its short name (ts, js, rs)."""
    name = SHORT.get(text.lower(), text.lower())
    if name not in LANGUAGES:
        msg = f"{text!r}: javascript (js), typescript (ts) or rust (rs)"
        raise argparse.ArgumentTypeError(msg)
    return name


def _next_step(project: Project, names: list[str], to_visit: list[str], ports: list[str]) -> tuple[str, str] | None:
    """Return the command to run next and why, before every port is at parity; None when they all are."""
    missing = unrecorded(project)
    parity = {n: last_parity(project, n) for n in ports}
    if missing:
        return "parityrun record", f"{', '.join(missing)}: the two runs differed (see above); make them alike"
    if project.library and (not names or to_visit):
        return (
            "edit scenarios/*.yaml, then: parityrun record",
            f"call the rest of the library: {len(to_visit)} not called yet (`parityrun routes` lists them)",
        )
    if not names or to_visit:
        return "parityrun capture", f"use the app in your browser: {len(to_visit)} routes not visited yet"
    return _port_step(ports, parity)


def _port_step(ports: list[str], parity: dict[str, tuple[int, int] | None]) -> tuple[str, str] | None:
    """Return the command that brings the ports to parity, or None when they all are."""
    if not ports:
        why = "your AI tool ports it to TypeScript (or js, rs) until it matches"
        if not shutil.which("claude"):
            why += '; it uses Claude Code (https://claude.com/claude-code), or add --agent "yourtool {prompt_file}"'
        return "parityrun port --to ts", why
    behind = [n for n, got in parity.items() if not got or not got[1] or got[0] != got[1]]
    if not behind:
        return None
    n = behind[0]
    if n in PORTS and parity[n]:
        return f"parityrun port --to {SHORT_OF[PORTS[n]]}", "your AI tool goes on from where the port is"
    return f"parityrun check --impl {n}", "see what differs"


def _visited(project: Project) -> tuple[str, list[str]]:
    """Print how many routes (or library parts) the scenarios call; return that line and the ones they don't."""
    if not (project.golden / "endpoints.json").exists():
        return "", []
    endpoints = [f"{m} {p}" for m, p in templates(project)]
    hit = covered(project)
    to_visit = [t for t in endpoints if t not in hit]
    what = "functions, methods and classes called" if project.library else "routes visited"
    if not endpoints:
        return "", to_visit
    visited = f"{len(endpoints) - len(to_visit)}/{len(endpoints)} {what}"
    print(f"  {visited}")
    return visited, to_visit


def status(project: Project) -> int:
    """Show where the project stands, then the one command to run next.

    Scenarios, routes (or a library's API), code run and every port's parity.
    """
    names = runner.all_scenarios(project)
    missing = unrecorded(project)
    reference = runner.Implementation(project, project.reference)
    impls: dict[str, object] = json.loads((project.root / "impls.json").read_text(encoding="utf-8"))
    ports = [n for n in impls if n != project.reference]
    print(
        f"{reference.folder.resolve().name}: {len(names) - len(missing)} of {len(names)} scenario"
        f"{'s' * (len(names) != 1)} recorded"
    )
    visited, to_visit = _visited(project)
    lines = ""
    reports = code_coverage.golden_reports(project.golden)
    if reports:
        s = code_coverage.summary(code_coverage.merge(reports), reference.folder)
        lines = f"{s['percent']:.0f} % of the PHP code run"
        print(f"  {lines} ({s['run']}/{s['lines']} lines)")
    for n in ports:
        got = last_parity(project, n)
        print(
            f"  {n}: "
            + (f"parity {100 * got[0] / got[1]:.1f} % at its last check" if got and got[1] else "not checked yet")
        )
    step = _next_step(project, names, to_visit, ports)
    if step is None:
        proof = " and ".join(x for x in (visited, lines) if x)
        print(
            f"\nDone: {', '.join(ports)} behave{'s' * (len(ports) == 1)} like the original on {len(names)} "
            f"scenario{'s' * (len(names) != 1)}{f' ({proof})' if proof else ''}."
        )
        step = f"parityrun check --impl {ports[0]}", "after every change, to see it still does"
    print(f"\nNext: {step[0]}\n      {step[1]}")
    return 0


def _parser(project: Project | None) -> argparse.ArgumentParser:
    """Return the command line's parser."""
    parser = argparse.ArgumentParser(prog=project.prog if project else "parityrun")
    if project is None:
        _ = parser.add_argument(
            "--project",
            default=os.environ.get("PARITYRUN_PROJECT") or "parityrun_project.py",
            help="the file that defines PROJECT (default: $PARITYRUN_PROJECT, else parityrun_project.py)",
        )
    _ = parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="cmd")
    rec = sub.add_parser("record", help="new golden recordings from the reference implementation")
    _ = rec.add_argument("scenarios", nargs="*")
    _ = rec.add_argument("--force", action="store_true", help="record even if two reference runs differ")
    chk = sub.add_parser("check", help="compare an implementation with the golden recordings")
    _ = chk.add_argument("scenarios", nargs="*")
    _ = chk.add_argument("--impl", help="the implementation in impls.json (default: the reference)")
    _ = chk.add_argument("--quick", action="store_true", help="only the project's quick scenarios")
    _ = chk.add_argument("--slow", "--video", action="store_true", dest="slow", help="only the others")
    _ = sub.add_parser("coverage", help="which endpoints in golden/endpoints.json the scenarios call")
    first = sub.add_parser("init", help="set parityrun up in an existing app's folder (writes parityrun/)")
    _ = first.add_argument("path", nargs="?", default=".", help="the app's folder (default: here)")
    _ = first.add_argument("--force", action="store_true", help="write parityrun/ again even if it exists")
    _ = first.add_argument("--no-start", action="store_true", help="don't start the app to look at it")
    _ = sub.add_parser("routes", help="read the app's routes from its code into golden/endpoints.json")
    cap = sub.add_parser("capture", help="use the app in your browser: what you do becomes a scenario")
    _ = cap.add_argument("name", nargs="?", help="the scenario's name (default: visit, visit-2, ...)")
    _ = cap.add_argument("--port", type=int, default=8000, help="where the browser opens the app (default: 8000)")
    _ = cap.add_argument("--impl", help="the implementation in impls.json (default: the reference)")
    _ = cap.add_argument("--no-browser", action="store_true", help="don't open the browser")
    _port_parser(
        sub.add_parser(
            "port", help="port the app to another language with your AI tool, turn by turn, until check passes"
        )
    )
    return parser


def _port_parser(tr: argparse.ArgumentParser) -> None:
    """Add `port`'s options."""
    _ = tr.add_argument(
        "--to",
        required=True,
        type=language,
        metavar="{javascript,typescript,rust}",
        help="the language of the port (or js, ts, rs)",
    )
    _ = tr.add_argument("--into", help="the port's folder (default: beside the app, <app>-ts, -js or -rs)")
    _ = tr.add_argument("--name", help="its name in impls.json (default: ts, js or rust)")
    _ = tr.add_argument(
        "--agent",
        default="claude",
        help="claude (Claude Code, the default) or your own tool's command line, with {prompt_file}",
    )
    _ = tr.add_argument("--turns", type=int, default=20, help="at most this many agent turns (default: 20)")
    _ = tr.add_argument(
        "--patience",
        type=int,
        default=3,
        help="turns a scenario may stay red without getting better before it is set aside (default: 3)",
    )
    _ = tr.add_argument(
        "--turn-minutes", type=float, default=60, help="the longest one agent turn may take (default: 60)"
    )
    _ = tr.add_argument(
        "--setup-only",
        action="store_true",
        help="write the port's folder, its impls.json entry and PORTING.md; start no agent",
    )


def _project_file(args: argparse.Namespace) -> str | None:
    """Return the project file to load, after setting parityrun up when there is none; None when that failed."""
    name: str = args.project
    if name != "parityrun_project.py" or pathlib.Path(name).exists():
        return name
    written = str(pathlib.Path("parityrun", "parityrun_project.py"))
    if pathlib.Path(written).exists():  # what `init` wrote, from the app's folder
        return written
    print("parityrun isn't set up here yet: setting it up first (parityrun init).\n")
    if init(pathlib.Path(), guide=False):
        return None
    print()
    return written


def _capture(project: Project, args: argparse.Namespace) -> int:
    """Run `parityrun capture`, then record the scenario twice to prove it replays."""
    if project.library:
        print(
            "A library has no pages to visit: write its calls in scenarios/*.yaml (fn: and new: steps; "
            "`parityrun routes` lists the API), then `parityrun record`."
        )
        return 1
    name = capture(project, args.name, args.impl, args.port, open_browser=not args.no_browser)
    if name is None:
        return 1
    print(f"\nRecording {name} twice to check it replays on a fresh app ...")
    return record(project, [name], force=False)


def _check(project: Project, args: argparse.Namespace) -> int:
    """Run `parityrun check` on the chosen scenarios."""
    everything = runner.all_scenarios(project)
    if args.quick:
        chosen = project.quick
    elif args.slow:
        chosen = [n for n in everything if n not in project.quick]
    else:
        chosen = everything
    return check(project, args.impl or project.reference, args.scenarios or chosen)


def _port(project: Project, args: argparse.Namespace, source: pathlib.Path | None) -> int:
    """Run `parityrun port`."""
    options = Options(
        args.to,
        into=args.into,
        name=args.name,
        agent=args.agent,
        turns=args.turns,
        patience=args.patience,
        turn_minutes=args.turn_minutes,
        setup_only=args.setup_only,
        project_file=source,
    )
    return port(project, options)


def _run(project: Project, args: argparse.Namespace, source: pathlib.Path | None) -> int:
    """Run the command `args` names on the project."""
    failed = 0
    if args.cmd in (None, "check", "port") and unrecorded(project):
        missing = unrecorded(project)
        print(f"Recording first: {', '.join(missing)}")
        failed = record(project, missing, force=False)
        if failed and args.cmd:
            return 1
        print()
    if args.cmd is None:
        return status(project) or failed
    handlers: dict[str, Callable[[], int]] = {
        "record": lambda: record(project, args.scenarios or runner.all_scenarios(project), force=args.force),
        "check": lambda: _check(project, args),
        "routes": lambda: list_routes(project),
        "capture": lambda: _capture(project, args),
        "port": lambda: _port(project, args, source),
    }
    return handlers.get(args.cmd, lambda: coverage(project))()


def main(argv: list[str] | None = None, project: Project | None = None) -> int:
    """Run the command line; a project's own entry point passes its PROJECT instead of --project."""
    cast("io.TextIOWrapper", sys.stdout).reconfigure(encoding="utf-8")
    args = _parser(project).parse_args(argv)
    if args.cmd == "init":
        return init(pathlib.Path(args.path), force=args.force, check_start=not args.no_start)
    source = None
    if project is None:
        name = _project_file(args)
        if name is None:
            return 1
        source = pathlib.Path(name).resolve()  # what `port` lets the agent check with
        project = load(name)
    return _run(project, args, source)


def list_routes(project: Project) -> int:
    """Read the reference's routes from its code, save them as golden/endpoints.json, show which are visited."""
    impl = runner.Implementation(project, project.reference)
    if project.library:
        return list_api(project, impl.folder)
    found, notes = routes.scan(impl.folder)
    for note in notes:
        print(f"note: {note}")
    if not found:
        print(f'No routes found in {impl.folder}: list them in golden/endpoints.json yourself ("GET /path").')
        return 1
    path = write_endpoints(project, found)
    done = covered_routes(project, [r.key for r in found])
    width = max(len(r.key) for r in found)
    for r in found:
        print(f"  {'x' if r.key in done else ' '} {r.key.ljust(width)}   {r.source}")
    print(
        f"\n{len(found)} routes, {len(done)} visited by the scenarios. Saved {path.relative_to(project.root.parent)}."
        f"\nVisit the rest with: parityrun capture"
    )
    return 0


def list_api(project: Project, folder: pathlib.Path) -> int:
    """Read a library's API from its code into golden/endpoints.json, and show which parts the scenarios call."""
    found, notes = api.scan(folder)
    for note in notes:
        print(f"note: {note}")
    if not found:
        return 1
    path = project.golden / "endpoints.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_text(json.dumps([part.key for part in found], indent=1) + "\n", encoding="utf-8")
    done = covered(project)
    width = max(len(part.key + part.params) for part in found)
    for part in found:
        print(f"  {'x' if part.key in done else ' '} {(part.key + part.params).ljust(width)}   {part.source}")
    print(
        f"\n{len(found)} functions, methods and classes, {len(done)} called by the scenarios. Saved "
        f"{path.relative_to(project.root.parent)}.\nCall the rest from scenarios/*.yaml (fn: and new: steps)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
