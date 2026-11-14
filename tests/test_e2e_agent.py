"""End to end with a real AI tool: `parityrun port` ports the examples from scratch, and fixes a port's bug.

It ports the guestbook's PHP site and the cart's PHP library to JavaScript, TypeScript and Rust from scratch, and
fixes a TypeScript port that has a typical porting bug from the failing checks it is given.

They run with Claude Code whenever `claude` is on PATH (logged in): each takes minutes and uses its account.

  PARITYRUN_TEST_AGENT=off                     skip them
  PARITYRUN_TEST_AGENT=claude                  Claude Code, and skip none for want of it: a missing `claude` fails
  PARITYRUN_TEST_AGENT="yourtool {prompt_file}"  any other tool, as `port --agent` takes it

CI doesn't run them: an open-source repository's CI has no AI account to give them, so run them before a release.

Each test also skips when its toolchain is missing: php for the reference, node for javascript, node 23.6+ for
typescript, cargo for rust. Only the PHP version and the scenarios are copied, and the from-scratch ports start from
a variant of the example's PHP (other messages and number format), so a copy of one of the example's own ports,
which the tool could find in this repository, can't reach 100 %: the tool has to port the code it is given.
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
CHOSEN = os.environ.get("PARITYRUN_TEST_AGENT")
AGENT = None if CHOSEN == "off" else CHOSEN or ("claude" if shutil.which("claude") else None)
TURNS = {"javascript": 5, "typescript": 5, "rust": 8}
MAIN = {"javascript": "server.js", "typescript": "server.ts", "rust": "src/main.rs"}
SUFFIX = {"javascript": "js", "typescript": "ts", "rust": "rs"}
# What the from-scratch ports start from: the example's PHP with these changes (file -> old, new, what then shows in
# the recording). None of the example's ports says what the recording then says, so copying one fails the check.
VARIANT = {
    "guestbook": {
        "php/index.php": [
            ("'Please write your name.'", "'Tell us your name.'", "Tell us your name."),
            ('" · Guestbook</title>', '" · Visitor book</title>', "· Visitor book"),
        ]
    },
    "cart": {
        "php/src/functions.php": [("2, ',', ' ')", "2, ',', '.')", "1.234,50 €")],
        "php/src/Cart.php": [('"No $sku in the cart"', '"No $sku in this cart"', "No kiwi in this cart")],
    },
}

pytestmark = pytest.mark.skipif(
    not AGENT,
    reason="PARITYRUN_TEST_AGENT=off"
    if CHOSEN == "off"
    else 'claude isn\'t on PATH (or set PARITYRUN_TEST_AGENT="yourtool {prompt_file}")',
)


def _needs(language: str) -> None:
    assert AGENT != "claude" or shutil.which("claude"), "PARITYRUN_TEST_AGENT=claude, but claude isn't on PATH"
    for tool in ("php", "cargo" if language == "rust" else "node"):
        if not shutil.which(tool):
            pytest.skip(f"{tool} isn't on PATH")
    if language == "typescript":
        node = shutil.which("node") or "node"
        version = subprocess.run([node, "--version"], capture_output=True, text=True, check=False).stdout  # noqa: S603
        if tuple(int(x) for x in re.findall(r"\d+", version)[:2]) < (23, 6):
            pytest.skip(f"node {version.strip()} can't run TypeScript as is (needs 23.6 or newer)")


def _site(tmp_path: pathlib.Path, example: pathlib.Path = GUESTBOOK, *, variant: bool = True) -> pathlib.Path:
    """Record an example's PHP version (its VARIANT, unless told not to) and its scenarios alone in a folder.

    Return the project file. With the variant, the recording must say what none of the example's ports says.
    """
    root = tmp_path / example.name
    for folder in ("php", "scenarios"):
        _ = shutil.copytree(example / folder, root / folder)
    for name in ("parityrun_project.py", "impls.json"):
        _ = shutil.copy(example / name, root / name)
    changes = VARIANT[example.name] if variant else {}
    for rel, edits in changes.items():
        code = (root / rel).read_text(encoding="utf-8")
        for old, new, _shown in edits:
            assert code.count(old) == 1, f"{rel}: {old}"
            code = code.replace(old, new)
        _ = (root / rel).write_text(code, encoding="utf-8")
    file = root / "parityrun_project.py"
    assert cli.main(["--project", str(file), "record"]) == 0
    if changes:
        recorded = "\n".join(
            json.dumps(json.loads(p.read_text(encoding="utf-8")), ensure_ascii=False)
            for p in sorted((root / "golden").glob("*/recording.json"))
        )
        ports = "\n".join(
            p.read_text(encoding="utf-8")
            for folder in ("js", "ts", "rust/src")
            for p in sorted((example / folder).rglob("*"))
            if p.is_file() and p.suffix in {".js", ".ts", ".rs"}
        )
        for edits in changes.values():
            for _old, _new, shown in edits:
                assert shown in recorded, shown  # the variant behaves differently from the example ...
                assert shown not in ports, shown  # ... and no ready-made port of it says so
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
    php = tmp_path / "guestbook" / "php" / "index.php"
    ported = php.read_bytes()
    turns = _port(file, language, f"{language}-ai")
    assert turns[-1]["parity"] == 100
    assert turns[-1]["problem"] is None
    assert all(t["restored"] == [] for t in turns)  # the tool kept to its folder
    assert (tmp_path / "guestbook" / f"php-{SUFFIX[language]}" / MAIN[language]).is_file()  # beside php/, by default
    assert php.read_bytes() == ported  # the tool left the PHP it ported as it was
    assert cli.main(["--project", str(file), "check", "--impl", f"{language}-ai"]) == 0  # and it stays at 100 %


def test_the_ai_tool_fixes_a_porting_bug_from_the_failing_checks(tmp_path: pathlib.Path) -> None:
    """Fix a TypeScript port that trims like JavaScript, from the failing checks of `validation`.

    String.trim also strips a no-break space; PHP's trim keeps it. The first turn is told which checks of `validation`
    fail, and is to make them pass.
    """
    _needs("typescript")
    file = _site(tmp_path, variant=False)  # the example's own PHP: the port to fix is the example's own, with a bug
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
    src = tmp_path / "cart" / "php" / "src"
    ported = {p.name: p.read_bytes() for p in src.iterdir()}
    turns = _port(file, language, f"{language}-ai")
    assert turns[-1]["parity"] == 100
    assert turns[-1]["problem"] is None
    assert all(t["restored"] == [] for t in turns)
    assert {p.name: p.read_bytes() for p in src.iterdir()} == ported  # the tool left the PHP it ported as it was
    assert cli.main(["--project", str(file), "check", "--impl", f"{language}-ai"]) == 0
