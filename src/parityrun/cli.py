"""The parityrun command: record golden runs from the reference implementation, check any implementation against
them, and list the endpoints the scenarios don't use yet.

  parityrun [--project FILE] record [scenario ...]                 new golden recordings from the reference (run twice:
                                                                   anything that differs between the runs is reported)
  parityrun [--project FILE] check [--quick | --slow] [--impl NAME] [scenario ...]
                                                                   compare an implementation with the recordings
  parityrun [--project FILE] coverage                              which endpoints (golden/endpoints.json) are used
  parityrun init [PATH] [--force] [--no-start]                     set parityrun up in an existing app's folder
  parityrun [--project FILE] routes                                read the app's routes from its code
  parityrun [--project FILE] capture [NAME] [--port 8000]          use the app in a browser; it becomes a scenario

FILE (default parityrun_project.py, else parityrun/parityrun_project.py) defines PROJECT = parityrun.Project(...).
"""

import argparse
import json
import pathlib
import re
import shutil
import sys
import tempfile

from . import runner
from .project import Project, load


def dump(recording: dict) -> str:
    """A recording as it is saved: stable key order, one space indents, UTF-8 kept readable."""
    return json.dumps(recording, indent=1, ensure_ascii=False, sort_keys=True) + "\n"


def record(project: Project, names: list[str], force: bool) -> int:
    impl = runner.Implementation(project, project.reference)
    failed = 0
    for name in names:
        dirs = [pathlib.Path(tempfile.mkdtemp(prefix=f"rec-{name}-")) for _ in range(2)]
        runs = [runner.run(impl, name, d) for d in dirs]
        unstable = [c for c in runner.checks(runs[0], runs[1], dirs[0], dirs[1], project) if not c["ok"]]
        if unstable and not force:
            failed += 1
            print(f"{name}: UNSTABLE between two reference runs (normalise or remove these):")
            for c in unstable:
                print(f"  {c['name']}: {'; '.join(c['detail'][:3])}")
            continue
        target = project.golden / name
        shutil.rmtree(target, ignore_errors=True)
        shutil.copytree(dirs[0], target)
        (target / "recording.json").write_text(dump(runs[0]), encoding="utf-8", newline="\n")
        print(f"{name}: recorded {len(runs[0]['steps'])} steps, {len(runs[0]['db'].get('rows', {}))} tables, "
              f"{len(runs[0]['files'])} files")
        for d in dirs:
            shutil.rmtree(d, ignore_errors=True)
    return 1 if failed else 0


def check(project: Project, impl_name: str, names: list[str]) -> int:
    impl = runner.Implementation(project, impl_name)
    report, total, passed = {}, 0, 0
    for name in names:
        golden_dir = project.golden / name
        if not (golden_dir / "recording.json").exists():
            print(f"{name}: no golden recording; run `record {name}` first")
            continue
        golden = json.loads((golden_dir / "recording.json").read_text(encoding="utf-8"))
        out = pathlib.Path(tempfile.mkdtemp(prefix=f"chk-{name}-"))
        try:
            got = runner.run(impl, name, out, strict=False)
            results = runner.checks(golden, got, golden_dir, out, project)
        except Exception as exc:  # noqa: BLE001 - a crash fails every comparison of the scenario
            # Count them all, so the parity percentage always has the same denominator (the golden comparisons).
            expected = runner.checks(golden, golden, golden_dir, golden_dir, project)
            results = [{"name": "run", "ok": False, "detail": [str(exc)]}] + \
                      [{**c, "ok": False, "detail": ["the run crashed"]} for c in expected[1:]]
        ok = sum(r["ok"] for r in results)
        total, passed = total + len(results), passed + ok
        report[name] = results
        print(f"{name}: {ok}/{len(results)} checks pass")
        for r in results:
            if not r["ok"]:
                print(f"  FAIL {r['name']}")
                for line in r["detail"][:4]:
                    print(f"       {line}")
        kept = project.out / impl_name / name  # its images and frames, to look at next to golden/<name>
        shutil.rmtree(kept, ignore_errors=True)
        shutil.copytree(out, kept)
        shutil.rmtree(out, ignore_errors=True)
    project.out.mkdir(exist_ok=True)
    (project.out / f"report-{impl_name}.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    parity = 100 * passed / total if total else 0
    print(f"\n{impl_name}: parity {parity:.1f} % ({passed}/{total} checks)")
    if (project.golden / "endpoints.json").exists():
        coverage(project, quiet=True)
    return 0 if total and passed == total else 1


def _templates(project: Project) -> list[tuple[str, str]]:
    path = project.golden / "endpoints.json"
    if not path.exists():
        raise SystemExit("golden/endpoints.json is missing: it lists every endpoint of the contract")
    return [tuple(e.split(" ", 1)) for e in json.loads(path.read_text(encoding="utf-8"))]


def covered(project: Project) -> set[str]:
    """The endpoint templates ("GET /api/jobs/{id}") some scenario step calls."""
    templates = _templates(project)
    hit = set()
    for name in runner.all_scenarios(project):
        for step in runner.load(project, name)["steps"]:
            if step.get("sse") and project.events:  # the step also listens to the live events
                hit.add(f"GET {project.events}")
            if "call" not in step:  # a command-line step
                continue
            method, path = step["call"].split(" ", 1)
            path = runner.TEMPLATE.sub("X", path.split("?")[0])
            for t_method, t_path in templates:
                pattern = "^" + re.sub(r"\\\{[^/]+?\\\}", "[^/]+", re.escape(t_path)) + "$"
                if method == t_method and re.match(pattern, path):
                    hit.add(f"{t_method} {t_path}")
    return hit


def coverage(project: Project, quiet: bool = False) -> int:
    templates = _templates(project)
    hit = covered(project)
    missing = [f"{m} {p}" for m, p in templates if f"{m} {p}" not in hit]
    print(f"Endpoint coverage: {len(hit)}/{len(templates)} ({100 * len(hit) / len(templates):.0f} %)")
    if not quiet:
        for e in missing:
            print(f"  not covered: {e}")
    return 0


def main(argv: list[str] | None = None, project: Project | None = None) -> int:
    """The command line; a project's own entry point passes its PROJECT instead of --project."""
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog=project.prog if project else "parityrun")
    if project is None:
        parser.add_argument("--project", default="parityrun_project.py",
                            help="the file that defines PROJECT (default: parityrun_project.py)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    rec = sub.add_parser("record", help="new golden recordings from the reference implementation")
    rec.add_argument("scenarios", nargs="*")
    rec.add_argument("--force", action="store_true", help="record even if two reference runs differ")
    chk = sub.add_parser("check", help="compare an implementation with the golden recordings")
    chk.add_argument("scenarios", nargs="*")
    chk.add_argument("--impl", help="the implementation in impls.json (default: the reference)")
    chk.add_argument("--quick", action="store_true", help="only the project's quick scenarios")
    chk.add_argument("--slow", "--video", action="store_true", dest="slow", help="only the others")
    sub.add_parser("coverage", help="which endpoints in golden/endpoints.json the scenarios call")
    first = sub.add_parser("init", help="set parityrun up in an existing app's folder (writes parityrun/)")
    first.add_argument("path", nargs="?", default=".", help="the app's folder (default: here)")
    first.add_argument("--force", action="store_true", help="write parityrun/ again even if it exists")
    first.add_argument("--no-start", action="store_true", help="don't start the app to look at it")
    sub.add_parser("routes", help="read the app's routes from its code into golden/endpoints.json")
    cap = sub.add_parser("capture", help="use the app in your browser: what you do becomes a scenario")
    cap.add_argument("name", nargs="?", help="the scenario's name (default: visit, visit-2, ...)")
    cap.add_argument("--port", type=int, default=8000, help="where the browser opens the app (default: 8000)")
    cap.add_argument("--impl", help="the implementation in impls.json (default: the reference)")
    cap.add_argument("--no-browser", action="store_true", help="don't open the browser")
    args = parser.parse_args(argv)
    if args.cmd == "init":
        from .init import init
        return init(pathlib.Path(args.path), force=args.force, check_start=not args.no_start)
    if project is None and args.project == "parityrun_project.py" and not pathlib.Path(args.project).exists() \
            and pathlib.Path("parityrun", "parityrun_project.py").exists():  # what `init` wrote, from the app's folder
        args.project = str(pathlib.Path("parityrun", "parityrun_project.py"))
    project = project or load(args.project)
    if args.cmd == "record":
        return record(project, args.scenarios or runner.all_scenarios(project), args.force)
    if args.cmd == "check":
        everything = runner.all_scenarios(project)
        chosen = project.quick if args.quick else [n for n in everything if n not in project.quick] \
            if args.slow else everything
        return check(project, args.impl or project.reference, args.scenarios or chosen)
    if args.cmd == "routes":
        return list_routes(project)
    if args.cmd == "capture":
        from .capture import capture
        return capture(project, args.name, args.impl, args.port, open_browser=not args.no_browser)
    return coverage(project)


def list_routes(project: Project) -> int:
    """Read the reference's routes from its code, save them as golden/endpoints.json, show which are visited."""
    from . import routes
    from .capture import _covered, write_endpoints

    impl = runner.Implementation(project, project.reference)
    found, notes = routes.scan(impl.folder)
    for note in notes:
        print(f"note: {note}")
    if not found:
        print(f"No routes found in {impl.folder}: list them in golden/endpoints.json yourself (\"GET /path\").")
        return 1
    path = write_endpoints(project, found)
    done = _covered(project, [r.key for r in found])
    width = max(len(r.key) for r in found)
    for r in found:
        print(f"  {'x' if r.key in done else ' '} {r.key.ljust(width)}   {r.source}")
    print(f"\n{len(found)} routes, {len(done)} visited by the scenarios. Saved {path.relative_to(project.root.parent)}."
          f"\nVisit the rest with: parityrun capture")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
