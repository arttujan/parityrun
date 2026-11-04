"""End to end with a real AI tool: `parityrun port` ports the examples from scratch, and fixes a port's bug.

It ports the guestbook's PHP site and the cart's PHP library to JavaScript, TypeScript and Rust from scratch, and
fixes a TypeScript port that has a typical porting bug from the failing checks it is given.

These runs use your AI tool's account and take minutes each, so they only run when asked:

  PARITYRUN_TEST_AGENT=claude                  Claude Code (`claude` on PATH, logged in)
  PARITYRUN_TEST_AGENT="yourtool {prompt_file}"  any other tool, as `port --agent` takes it

Each test also skips when its toolchain is missing: php for the reference, node for javascript, node 23.6+ for
typescript, cargo for rust. Only the PHP version and the scenarios are copied, so the tool can't see the example's
own ports.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess
from typing import TYPE_CHECKING

import pytest

from parityrun import cli

if TYPE_CHECKING:
    from parityrun.jsontypes import Data

GUESTBOOK = pathlib.Path(__file__).resolve().parents[1] / "examples" / "guestbook"
CART = GUESTBOOK.parent / "cart"
AGENT = os.environ.get("PARITYRUN_TEST_AGENT")
TURNS = {"javascript": 5, "typescript": 5, "rust": 8}
MAIN = {"javascript": "server.js", "typescript": "server.ts", "rust": "src/main.rs"}
EXAMPLE = {"javascript": "js/server.js", "typescript": "ts/server.ts", "rust": "rust/src/main.rs"}
SUFFIX = {"javascript": "js", "typescript": "ts", "rust": "rs"}

pytestmark = pytest.mark.skipif(not AGENT, reason='set PARITYRUN_TEST_AGENT=claude (or "yourtool {prompt_file}")')


def _needs(language: str) -> None:
    if AGENT == "claude" and not shutil.which("claude"):
        pytest.skip("claude isn't on PATH")
    for tool in ("php", "cargo" if language == "rust" else "node"):
        if not shutil.which(tool):
            pytest.skip(f"{tool} isn't on PATH")
    if language == "typescript":
        node = shutil.which("node") or "node"
        version = subprocess.run([node, "--version"], capture_output=True, text=True, check=False).stdout  # noqa: S603
        if tuple(int(x) for x in re.findall(r"\d+", version)[:2]) < (23, 6):
            pytest.skip(f"node {version.strip()} can't run TypeScript as is (needs 23.6 or newer)")


def _site(tmp_path: pathlib.Path, example: pathlib.Path = GUESTBOOK) -> pathlib.Path:
    """Record an example's PHP version and its scenarios alone in a folder; return the project file."""
    root = tmp_path / example.name
    for folder in ("php", "scenarios"):
        _ = shutil.copytree(example / folder, root / folder)
    for name in ("parityrun_project.py", "impls.json"):
        _ = shutil.copy(example / name, root / name)
    file = root / "parityrun_project.py"
    assert cli.main(["--project", str(file), "record"]) == 0
    return file


def _port(file: pathlib.Path, language: str, name: str) -> list[Data]:
    """Run `parityrun port` with the tool; return every turn's log line."""
    code = cli.main(
        [
            "--project",
            str(file),
            "port",
            "--to",
            language,
            "--name",
            name,
            "--agent",
            AGENT or "",
            "--turns",
            str(TURNS[language]),
            "--turn-minutes",
            "30",
        ]
    )
    log = file.parent / "out" / "port-log.jsonl"
    turns: list[Data] = (
        [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
    )
    assert code == 0, f"not at 100 %: {turns[-1] if turns else 'no turn ran'}"
    return turns


@pytest.mark.parametrize("language", ["javascript", "typescript", "rust"])
def test_the_ai_tool_ports_the_php_site_to_100_percent(tmp_path: pathlib.Path, language: str) -> None:
    _needs(language)
    file = _site(tmp_path)
    turns = _port(file, language, f"{language}-ai")
    assert turns[-1]["parity"] == 100
    assert turns[-1]["problem"] is None
    assert all(t["restored"] == [] for t in turns)  # the tool kept to its folder
    written = tmp_path / "guestbook" / f"php-{SUFFIX[language]}" / MAIN[language]  # beside php/, by default
    assert written.read_text(encoding="utf-8") != (GUESTBOOK / EXAMPLE[language]).read_text(encoding="utf-8")
    original = (GUESTBOOK / "php" / "index.php").read_bytes()
    assert (tmp_path / "guestbook" / "php" / "index.php").read_bytes() == original
    assert cli.main(["--project", str(file), "check", "--impl", f"{language}-ai"]) == 0  # and it stays at 100 %


def test_the_ai_tool_fixes_a_porting_bug_from_the_failing_checks(tmp_path: pathlib.Path) -> None:
    """Fix a TypeScript port that trims like JavaScript, from the failing checks of `validation`.

    String.trim also strips a no-break space; PHP's trim keeps it. The first turn is told which checks of `validation`
    fail, and is to make them pass.
    """
    _needs("typescript")
    file = _site(tmp_path)
    port = tmp_path / "guestbook" / "php-ts"
    port.mkdir()
    source = (GUESTBOOK / "ts" / "server.ts").read_text(encoding="utf-8")
    broken = source.replace('phpTrim(v.field("name"))', 'v.field("name").trim()').replace(
        "Number(process.argv[2] || 8080)", "Number(process.env.PORT)"
    )
    assert broken.count("process.env.PORT") == 1
    assert 'phpTrim(v.field("name"))' not in broken
    _ = (port / "server.ts").write_text(broken, encoding="utf-8")
    _ = (port / "package.json").write_text(
        json.dumps({"name": "php-ts", "private": True, "type": "module"}), encoding="utf-8"
    )
    turns = _port(file, "typescript", "ts-ai")
    assert turns[0]["focus"] == "validation"
    assert turns[-1]["parity"] == 100
    first = (file.parent / "out" / "port" / "turn-1.prompt.md").read_text(encoding="utf-8")
    assert "This turn: make scenario `validation` pass, and keep delete, sign green." in first
    assert "FAIL step 6 POST /sign status" in first  # the no-break-space name, refused by mistake


@pytest.mark.parametrize("language", ["javascript", "typescript", "rust"])
def test_the_ai_tool_ports_the_php_library_to_100_percent(tmp_path: pathlib.Path, language: str) -> None:
    """A library: the port is an empty library and parityrun's shim template, which the tool fills in."""
    _needs(language)
    file = _site(tmp_path, CART)
    turns = _port(file, language, f"{language}-ai")
    assert turns[-1]["parity"] == 100
    assert all(t["restored"] == [] for t in turns)
    original = (CART / "php" / "src" / "functions.php").read_bytes()
    assert (tmp_path / "cart" / "php" / "src" / "functions.php").read_bytes() == original
    assert cli.main(["--project", str(file), "check", "--impl", f"{language}-ai"]) == 0
