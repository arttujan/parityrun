"""conftest.py's live status, in a pytest run of its own: a slow test shows up while it runs, with what `parityrun
port` says, and a quick one doesn't."""

import os
import pathlib
import shutil
import subprocess
import sys

import pytest

TESTS = '''
import time

from parityrun import port


def test_quick():
    port.print("Turn 1: the quick test")


def test_slow(capsys):
    port.print("Turn 1: porting the whole app ...")
    time.sleep(2)
    port.print("  turn 1: 0.0 min; parity 100 %")
    time.sleep(0.5)
    assert "porting the whole app" in capsys.readouterr().out  # the test still gets its output
'''


def _run(tmp_path: pathlib.Path, *flags: str) -> str:
    shutil.copy(pathlib.Path(__file__).with_name("conftest.py"), tmp_path / "conftest.py")
    (tmp_path / "test_two.py").write_text(TESTS, encoding="utf-8")
    run = subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *flags, "test_two.py"],
                         cwd=tmp_path, env={**os.environ, "PARITYRUN_TEST_LIVE": "1,30"}, capture_output=True,
                         text=True, timeout=120)
    assert run.returncode == 0, run.stdout + run.stderr
    return run.stdout


@pytest.mark.parametrize("flags", [["-q"], ["-v"]])
def test_a_slow_test_shows_its_name_time_and_port_lines_while_it_runs(tmp_path, flags):
    out = _run(tmp_path, *flags)
    lines = out.splitlines()
    assert "[running 0:01] test_two.py::test_slow" in lines
    assert "    Turn 1: porting the whole app ..." in lines  # said before the first notice: shown with it
    assert "    turn 1: 0.0 min; parity 100 %" in lines  # said after: shown as it comes
    assert any(line.startswith("[done 0:0") and line.endswith("] test_two.py::test_slow") for line in lines)
    assert "the quick test" not in out
    assert "2 passed" in lines[-1]


def test_with_s_port_lines_are_not_shown_twice(tmp_path):
    out = _run(tmp_path, "-q", "-s")
    assert "[running 0:01] test_two.py::test_slow" in out.splitlines()
    assert out.count("porting the whole app") == 1
