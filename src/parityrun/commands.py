"""What the commands do: record golden runs, check an implementation against them, show what the scenarios cover.

The command line (parityrun.cli) and the port loop (parityrun.port) both run these.
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import tempfile
from typing import TYPE_CHECKING

from . import api, runner
from . import coverage as code_coverage

if TYPE_CHECKING:
    from .jsontypes import Data
    from .project import Project

DETAIL_LINES = 4  # the lines of a failing check's differences `check` prints


def dump(recording: Data) -> str:
    """Return a recording as it is saved: stable key order, one space indents, UTF-8 kept readable."""
    return json.dumps(recording, indent=1, ensure_ascii=False, sort_keys=True) + "\n"


def record(project: Project, names: list[str], *, force: bool) -> int:
    """Record the scenarios from the reference, twice: 1 when the two runs of one differ (unless `force`)."""
    impl = runner.Implementation(project, project.reference)
    broken = impl.build()
    if broken:
        print(f"{impl.name}: the build failed:\n{broken}")
        return 1
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
        _ = shutil.copytree(dirs[0], target)
        _ = (target / "recording.json").write_text(dump(runs[0]), encoding="utf-8", newline="\n")
        print(
            f"{name}: recorded {len(runs[0]['steps'])} steps, {len(runs[0]['db'].get('rows', {}))} tables, "
            f"{len(runs[0]['files'])} files"
        )
        for d in dirs:
            shutil.rmtree(d, ignore_errors=True)
    if project.coverage:
        print()
        _ = code_coverage.show(project.golden, impl.folder, detail=False)
    return 1 if failed else 0


def _check_one(project: Project, impl: runner.Implementation, name: str, golden_dir: pathlib.Path) -> list[Data]:
    """Run one scenario on the implementation and return its checks; keep what it did in out/<impl>/<name>."""
    golden: Data = json.loads((golden_dir / "recording.json").read_text(encoding="utf-8"))
    out = pathlib.Path(tempfile.mkdtemp(prefix=f"chk-{name}-"))
    try:
        got = runner.run(impl, name, out, strict=False)
        results = runner.checks(golden, got, golden_dir, out, project)
        _ = (out / "recording.json").write_text(dump(got), encoding="utf-8", newline="\n")  # to set beside golden's
    except Exception as exc:  # noqa: BLE001 (any crash of the run is a failed check, not the end of `check`)
        # Count them all, so the parity percentage always has the same denominator (the golden comparisons).
        expected = runner.checks(golden, golden, golden_dir, golden_dir, project)
        results = [{"name": "run", "ok": False, "detail": [str(exc)]}] + [
            {**c, "ok": False, "detail": ["the run crashed"]} for c in expected[1:]
        ]
    kept = project.out / impl.name / name  # its recording, images and frames, to look at next to golden/<name>
    shutil.rmtree(kept, ignore_errors=True)
    _ = shutil.copytree(out, kept)
    shutil.rmtree(out, ignore_errors=True)
    return results


def _print_checks(name: str, results: list[Data]) -> int:
    """Print a scenario's failing checks; return how many pass."""
    ok = sum(r["ok"] for r in results)
    print(f"{name}: {ok}/{len(results)} checks pass")
    for r in results:
        if not r["ok"]:
            print(f"  FAIL {r['name']}")
            for line in r["detail"][:DETAIL_LINES]:
                print(f"       {line}")
    return ok


def check(project: Project, impl_name: str, names: list[str]) -> int:
    """Check an implementation against the recordings: 0 at parity 100 %. The report is out/report-<impl>.json."""
    impl = runner.Implementation(project, impl_name)
    report: Data = {}
    total, passed = 0, 0
    broken = impl.build()
    if broken:  # nothing to run: every scenario fails
        print(f"{impl_name}: the build failed:\n{broken}")
        project.out.mkdir(exist_ok=True)
        failed = {"build": [{"name": "build", "ok": False, "detail": broken.splitlines()[-40:]}]}
        _ = (project.out / f"report-{impl_name}.json").write_text(json.dumps(failed, indent=1), encoding="utf-8")
        print(f"\n{impl_name}: parity 0.0 % (the build failed)")
        return 1
    for name in names:
        golden_dir = project.golden / name
        if not (golden_dir / "recording.json").exists():
            print(f"{name}: no golden recording; run `record {name}` first")
            continue
        results = _check_one(project, impl, name, golden_dir)
        ok = _print_checks(name, results)
        total, passed = total + len(results), passed + ok
        report[name] = results
    project.out.mkdir(exist_ok=True)
    _ = (project.out / f"report-{impl_name}.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    parity = 100 * passed / total if total else 0
    print(f"\n{impl_name}: parity {parity:.1f} % ({passed}/{total} checks)")
    if (project.golden / "endpoints.json").exists():
        _ = coverage(project, quiet=True)
    return 0 if total and passed == total else 1


def unrecorded(project: Project) -> list[str]:
    """Return the scenarios that have no recording yet."""
    return [n for n in runner.all_scenarios(project) if not (project.golden / n / "recording.json").exists()]


def last_parity(project: Project, impl: str) -> tuple[int, int] | None:
    """Return the (passed, total) checks of the implementation's last `check`; None before one."""
    path = project.out / f"report-{impl}.json"
    if not path.exists():
        return None
    report: Data = json.loads(path.read_text(encoding="utf-8"))
    results: list[Data] = [r for found in report.values() for r in found]
    return sum(r["ok"] for r in results), len(results)


def templates(project: Project) -> list[tuple[str, str]]:
    """Return golden/endpoints.json's endpoints as (method, path), or a library's parts as ("fn", name)."""
    path = project.golden / "endpoints.json"
    if not path.exists():
        msg = "golden/endpoints.json is missing: it lists every endpoint of the contract"
        raise SystemExit(msg)
    keys: list[str] = json.loads(path.read_text(encoding="utf-8"))
    return [(method, rest) for method, _, rest in (e.partition(" ") for e in keys)]


def _route_hit(step: Data, endpoints: list[tuple[str, str]]) -> list[str]:
    """Return the endpoint templates an HTTP step's call matches."""
    method, path = step["call"].split(" ", 1)
    path = runner.TEMPLATE.sub("X", path.split("?")[0])
    hits: list[str] = []
    for t_method, t_path in endpoints:
        pattern = "^" + re.sub(r"\\\{[^/]+?\\\}", "[^/]+", re.escape(t_path)) + "$"
        if method == t_method and re.match(pattern, path):
            hits.append(f"{t_method} {t_path}")
    return hits


def covered(project: Project) -> set[str]:
    r"""Return the endpoint templates ("GET /api/jobs/{id}") and library parts ("fn Shop\\slug") steps call."""
    endpoints = templates(project)
    keys = {f"{m} {p}" for m, p in endpoints}
    hit: set[str] = set()
    for name in runner.all_scenarios(project):
        objects: dict[str, str] = {}  # `as` name -> the class a `new` step made
        steps: list[Data] = runner.load(project, name)["steps"]
        for step in steps:
            if step.get("sse") and project.events:  # the step also listens to the live events
                hit.add(f"GET {project.events}")
            if "fn" in step or "new" in step:  # a call into a library
                part = api.key(step, objects)
                if part in keys:
                    hit.add(part)
            elif "call" in step:  # not a command-line step
                hit.update(_route_hit(step, endpoints))
    return hit


def coverage(project: Project, *, quiet: bool = False) -> int:
    """Show the routes the scenarios call (golden/endpoints.json) and, unless quiet, the PHP lines they ran."""
    shown = False
    if (project.golden / "endpoints.json").exists():
        endpoints = templates(project)
        hit = covered(project)
        missing = [f"{m} {p}" for m, p in endpoints if f"{m} {p}" not in hit]
        what = "API coverage" if project.library else "Endpoint coverage"
        print(
            f"{what}: {len(hit)}/{len(endpoints)} ({100 * len(hit) / len(endpoints):.0f} %)"
            if endpoints
            else "Endpoint coverage: golden/endpoints.json lists no routes"
        )
        if not quiet:
            for e in missing:
                print(f"  not covered: {e}")
        shown = True
    if not quiet:
        root = runner.Implementation(project, project.reference).folder
        shown = code_coverage.show(project.golden, root) or shown
    if not shown:
        print(
            "Nothing to show yet: run `parityrun routes` for the routes, and record with coverage=True for the "
            "lines of PHP code the scenarios run."
        )
        return 1
    return 0
