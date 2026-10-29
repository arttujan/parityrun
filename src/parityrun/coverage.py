"""PHP line coverage of the reference: which lines of the app's code the scenarios ran.

With Project(coverage=True) a PHP implementation starts with an auto_prepend_file that records each request's line
coverage (PCOV, else Xdebug) into the run's folder. `record` keeps it beside each recording
(golden/<name>/coverage.json) and prints the total; `parityrun coverage` lists every file with the lines no scenario
ran. Files no request loaded count too, with an estimate of their lines, so the total isn't flattered.
Without PCOV or Xdebug nothing changes but a note.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess

from .routes import SKIP_DIRS

PREPEND = r"""<?php
// parityrun: this request's line coverage, written when it ends (PCOV, else Xdebug).
(function () {
    $dir = getenv('PARITYRUN_COVERAGE');
    if (!$dir) {
        return;
    }
    if (function_exists('pcov\start')) {
        \pcov\start();
        $tool = 'pcov';
    } elseif (function_exists('xdebug_start_code_coverage')) {
        xdebug_start_code_coverage(XDEBUG_CC_UNUSED | XDEBUG_CC_DEAD_CODE);
        $tool = 'xdebug';
    } else {
        return;
    }
    register_shutdown_function(function () use ($dir, $tool) {
        if ($tool === 'pcov') {
            \pcov\stop();
            $lines = \pcov\collect(\pcov\all);
        } else {
            $lines = xdebug_get_code_coverage();
            xdebug_stop_code_coverage();
        }
        if (!is_dir($dir)) {
            @mkdir($dir, 0777, true);
        }
        file_put_contents($dir . '/' . str_replace('.', '', uniqid('', true)) . '.json',
                          json_encode(['tool' => $tool, 'files' => $lines]));
    });
})();
"""

PROBE = "echo extension_loaded('pcov') ? 'pcov' : (extension_loaded('xdebug') ? 'xdebug' : '');"
_tools: dict[str, tuple[list[str], str | None]] = {}
_warned: set[str] = set()


def detect(php: str) -> tuple[list[str], str | None]:
    """How this php loads a coverage tool: (the -d options that load it, "pcov" or "xdebug"), or ([], None)."""
    if php in _tools:
        return _tools[php]
    ext = pathlib.Path(shutil.which(php) or php).resolve().parent / "ext"
    tries = [[], ["-d", "extension=pcov"], ["-d", f"extension={ext / 'php_pcov.dll'}"],
             ["-d", "zend_extension=xdebug"], ["-d", f"zend_extension={ext / 'php_xdebug.dll'}"]]
    found: tuple[list[str], str | None] = ([], None)
    for load in tries:
        if any(".dll" in part for part in load) and not pathlib.Path(load[1].split("=", 1)[1]).exists():
            continue
        try:
            out = subprocess.run([shutil.which(php) or php, *load, "-r", PROBE], capture_output=True, text=True,
                                 timeout=30).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            break
        if out in ("pcov", "xdebug"):
            found = (load, out)
            break
    _tools[php] = found
    return found


def is_php(cmd: list[str]) -> bool:
    return bool(cmd) and pathlib.Path(cmd[0]).stem.lower() == "php"


def php_command(cmd: list[str], root: pathlib.Path, prepend: pathlib.Path) -> list[str]:
    """The php command with coverage on (the prepend file written), or as it was when PHP has no coverage tool."""
    load, tool = detect(cmd[0])
    if tool is None:
        if cmd[0] not in _warned:
            _warned.add(cmd[0])
            print("note: PHP code coverage needs the PCOV (or Xdebug) extension, which this PHP doesn't have: "
                  "https://github.com/krakjoe/pcov (Windows: downloads.php.net/~windows/pecl/releases/pcov/)",
                  flush=True)
        return cmd
    prepend.write_text(PREPEND, encoding="utf-8")
    on = ["-d", "pcov.enabled=1", "-d", f"pcov.directory={os.path.realpath(root)}"] if tool == "pcov" else \
        ["-d", "xdebug.mode=coverage"]  # PCOV compares PHP's own (long) paths with it
    return [cmd[0], *load, *on, "-d", f"auto_prepend_file={prepend}", *cmd[1:]]


def _relative(path: str, root: pathlib.Path) -> str | None:
    """A covered file as a path inside the app, or None for one outside it or in vendor/ and the like."""
    full, base = os.path.realpath(path), os.path.realpath(root)  # a temp folder may come as C:\Users\ARTTU~1
    if not os.path.normcase(full).startswith(os.path.normcase(base) + os.sep):
        return None
    rel = pathlib.PurePath(full[len(base) + 1:]).as_posix()
    return None if set(rel.split("/")[:-1]) & SKIP_DIRS else rel


def collect(folder: pathlib.Path, root: pathlib.Path) -> dict | None:
    """Every request's coverage in `folder` merged: {"tool", "files": {path: {"lines": [...], "run": [...]}}}."""
    if not folder.exists():
        return None
    tool, files = None, {}
    for path in sorted(folder.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        tool = data.get("tool") or tool
        for name, lines in (data.get("files") or {}).items():
            rel = _relative(name, root)
            if rel is None or not isinstance(lines, dict):
                continue
            entry = files.setdefault(rel, {"lines": set(), "run": set()})
            for line, code in lines.items():
                if code != -2:  # -2: dead code, never runnable
                    entry["lines"].add(int(line))
                if code > 0:
                    entry["run"].add(int(line))
    if tool is None:
        return None
    return {"tool": tool, "files": {rel: {"lines": sorted(e["lines"]), "run": sorted(e["run"])}
                                    for rel, e in sorted(files.items())}}


def merge(reports: list[dict]) -> dict:
    """The coverage of several scenarios together."""
    files: dict[str, dict[str, set]] = {}
    for report in reports:
        for rel, e in report["files"].items():
            entry = files.setdefault(rel, {"lines": set(), "run": set()})
            entry["lines"].update(e["lines"])
            entry["run"].update(e["run"])
    tool = next((r["tool"] for r in reports), None)
    return {"tool": tool, "files": {rel: {"lines": sorted(e["lines"]), "run": sorted(e["run"])}
                                    for rel, e in sorted(files.items())}}


CODELESS = re.compile(r"^(?:<\?php|\?>|[{}()\[\];,]+|//.*|#(?!\[).*|/\*.*|\*.*|use\s.*|namespace\s.*)?$")


def estimate(path: pathlib.Path) -> int:
    """About how many runnable lines a PHP file no request loaded has (blank lines, comments and braces left out)."""
    text = path.read_text(encoding="utf-8", errors="replace")
    return sum(1 for line in text.splitlines() if not CODELESS.match(line.strip()))


def php_files(root: pathlib.Path) -> list[str]:
    """The app's own PHP files (not vendor/, tests/ and the like)."""
    out = []
    for path in sorted(root.rglob("*.php")):
        rel = path.relative_to(root).as_posix()
        if path.is_file() and not set(rel.split("/")[:-1]) & SKIP_DIRS:
            out.append(rel)
    return out


def ranges(lines: list[int]) -> str:
    """[3, 4, 5, 9] -> "3-5, 9"."""
    out, start = [], None
    for k, line in enumerate(lines):
        if start is None:
            start = line
        if k + 1 == len(lines) or lines[k + 1] != line + 1:
            out.append(str(start) if start == line else f"{start}-{line}")
            start = None
    return ", ".join(out)


def summary(report: dict, root: pathlib.Path) -> dict:
    """Totals and one row per file, the least covered first: {percent, run, lines, rows: [(file, %, run, lines,
    missed, loaded)]}; files nothing loaded are rows with an estimate."""
    rows = []
    for rel, e in report["files"].items():
        missed = sorted(set(e["lines"]) - set(e["run"]))
        rows.append((rel, 100 * len(e["run"]) / len(e["lines"]) if e["lines"] else 100.0, len(e["run"]),
                     len(e["lines"]), missed, True))
    for rel in php_files(root):
        if rel not in report["files"]:
            rows.append((rel, 0.0, 0, estimate(root / rel), [], False))
    rows = [r for r in rows if r[3]]
    run, lines = sum(r[2] for r in rows), sum(r[3] for r in rows)
    rows.sort(key=lambda r: (r[1], r[0]))
    return {"percent": 100 * run / lines if lines else 0.0, "run": run, "lines": lines, "rows": rows}


def golden_reports(golden: pathlib.Path) -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(golden.glob("*/coverage.json"))]


def show(golden: pathlib.Path, root: pathlib.Path, detail: bool = True) -> bool:
    """Print the code coverage of every recorded scenario; False when there is none."""
    reports = golden_reports(golden)
    if not reports:
        return False
    s = summary(merge(reports), root)
    print(f"PHP code covered: {s['percent']:.1f} % of lines ({s['run']}/{s['lines']}) by {len(reports)} "
          f"scenario{'s' * (len(reports) != 1)}")
    never = [r[0] for r in s["rows"] if not r[5]]
    if detail:
        width = max(len(r[0]) for r in s["rows"])
        for rel, percent, _, lines, missed, loaded in s["rows"]:
            if percent < 100:
                what = f"not run: lines {ranges(missed)}" if loaded else f"never loaded (~{lines} lines)"
                print(f"  {rel.ljust(width)}  {percent:5.1f} %  {what}")
    elif never:
        print(f"  never loaded: {', '.join(never[:8])}{' ...' if len(never) > 8 else ''}")
    return True
