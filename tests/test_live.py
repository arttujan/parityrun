"""conftest.py's live status, in a pytest run of its own.

A slow test shows up while it runs, with what `parityrun port` says, and a quick one doesn't.
"""

import os
import pathlib
import shutil
import subprocess
import sys

import pytest

TESTS = """
import time

from parityrun import port


def test_quick():
    port.print("Turn 1: the quick test")


def test_slow():
    port.print("Turn 1: porting the whole app ...")
    time.sleep(2)
    port.print("  turn 1: 0.0 min; parity 100 %")
    time.sleep(0.5)


def test_capsys_still_gets_what_port_says(capsys):  # not in test_slow: capsys takes it even with -s
    port.print("Turn 2: fixing what fails ...")
    assert capsys.readouterr().out == "Turn 2: fixing what fails ...\\n"
"""


def _run(tmp_path: pathlib.Path, *flags: str) -> str:
    _ = shutil.copy(pathlib.Path(__file__).with_name("conftest.py"), tmp_path / "conftest.py")
    _ = (tmp_path / "test_two.py").write_text(TESTS, encoding="utf-8")
    run = subprocess.run(  # noqa: S603 (this Python's pytest)
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *flags, "test_two.py"],
        check=False,
        cwd=tmp_path,
        env={**os.environ, "PARITYRUN_TEST_LIVE": "1,30"},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    return run.stdout


@pytest.mark.parametrize("flags", [["-q"], ["-v"]])
def test_a_slow_test_shows_its_name_time_and_port_lines_while_it_runs(tmp_path: pathlib.Path, flags: list[str]) -> None:
    out = _run(tmp_path, *flags)
    lines = out.splitlines()
    assert "[running 0:01] test_two.py::test_slow" in lines
    assert "    Turn 1: porting the whole app ..." in lines  # said before the first notice: shown with it
    assert "      turn 1: 0.0 min; parity 100 %" in lines  # said after: shown as it comes, with port's own indent
    assert any(line.startswith("[done 0:0") and line.endswith("] test_two.py::test_slow") for line in lines)
    assert "the quick test" not in out
    assert "fixing what fails" not in out
    assert "3 passed" in lines[-1]


def test_with_s_port_lines_are_not_shown_twice(tmp_path: pathlib.Path) -> None:
    out = _run(tmp_path, "-q", "-s")
    lines = out.splitlines()
    assert "[running 0:01] test_two.py::test_slow" in lines
    assert any(line.startswith("[done 0:0") for line in lines)  # on a line of its own, after the dots
    assert out.count("porting the whole app") == 1
    assert "    Turn 1:" not in out
