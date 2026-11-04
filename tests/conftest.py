"""Live status while the tests run, for the tests that run for minutes.

Pytest only reports a test when it ends, and it captures what the test prints, so a test that runs for minutes (a real
AI tool porting a site) looks stuck. Here a test that runs past FIRST seconds gets its name and running time on the
terminal, again every EVERY seconds, and what `parityrun port` says (each turn as it starts and ends) as it says it.
Quick tests print nothing extra. PARITYRUN_TEST_LIVE="FIRST,EVERY" sets the two times (test_live.py does).

The lines go to a copy of the terminal's own file descriptor, past pytest's capture, so a test still sees all of
its output (capsys) and nothing of this.
"""

from __future__ import annotations

import contextlib
import os
import sys
import threading
import time
from typing import TYPE_CHECKING, cast

import pytest

from parityrun import port

if TYPE_CHECKING:
    from typing import TextIO

    from _pytest.capture import CaptureManager
    from _pytest.terminal import TerminalReporter

FIRST, EVERY = (float(x) for x in os.environ.get("PARITYRUN_TEST_LIVE", "10,30").split(","))


def _clock(seconds: float) -> str:
    return f"{int(seconds) // 60}:{int(seconds) % 60:02d}"


def _terminal(config: pytest.Config) -> TextIO | None:
    """Return a copy of the terminal's stdout, past pytest's capture; None when stdout isn't a real terminal."""
    capman = cast("CaptureManager | None", config.pluginmanager.getplugin("capturemanager"))
    try:
        if capman:
            with capman.global_and_fixture_disabled():
                fd = os.dup(sys.stdout.fileno())
        else:
            fd = os.dup(sys.stdout.fileno())
        # kept open for the whole session; pytest_unconfigure closes it
        return os.fdopen(fd, "w", encoding=sys.stdout.encoding or "utf-8", errors="replace", buffering=1)
    except (AttributeError, OSError, ValueError):  # no real terminal behind stdout: nothing to show live
        return None


class Live:
    """The test running now, and what `parityrun port` said in it."""

    def __init__(self, config: pytest.Config, out: TextIO) -> None:
        """Start the beat that shows a long test."""
        super().__init__()
        self.plugins = config.pluginmanager
        self.echo = config.getoption("capture") != "no"  # with -s, port's lines are on the terminal already
        self.lock = threading.Lock()
        self.test: str | None = None
        self.started, self.next, self.shown = 0.0, 0.0, False
        self.said: list[str] = []
        self.out = out
        self.done = threading.Event()
        threading.Thread(target=self._beat, daemon=True).start()

    @property
    def reporter(self) -> TerminalReporter | None:
        """Pytest's terminal reporter, looked up when needed: this conftest is configured before pytest makes it."""
        return cast("TerminalReporter | None", self.plugins.getplugin("terminalreporter"))

    def _write(self, lines: list[str]) -> None:
        with contextlib.suppress(OSError, ValueError):
            _ = self.out.write("".join(line + "\n" for line in lines))

    def _beat(self) -> None:
        while not self.done.wait(0.2):
            with self.lock:
                now = time.time()
                if self.test is None or now < self.next:
                    continue
                self.next = now + EVERY
                lines = [f"[running {_clock(now - self.started)}] {self.test}"]
                if not self.shown:  # off the dots line pytest is in the middle of
                    reporter = self.reporter
                    lines = ([""] if reporter and reporter._tw.width_of_current_line else []) + lines
                    lines += [f"    {line}" for line in self.said]
                    self.shown = True
                self._write(lines)

    def start(self, test: str) -> None:
        """Begin timing a test."""
        with self.lock:
            now = time.time()
            self.test, self.started, self.next, self.shown, self.said = test, now, now + FIRST, False, []

    def finish(self) -> None:
        """End the test; say how long it ran if it was shown."""
        with self.lock:
            test, seconds, shown = self.test, time.time() - self.started, self.shown
            self.test = None
        reporter = self.reporter
        if shown and reporter:  # through pytest's own writer, so its dots and percentages go on from here
            writer = reporter._tw  # its write_line breaks the line only after a file name, not after dots
            if writer.width_of_current_line:
                writer.line()
            writer.line(f"[done {_clock(seconds)}] {test}")

    def port_said(self, text: str) -> None:
        """Keep a line `parityrun port` printed, and show it if the test is shown."""
        if not self.echo:
            return
        with self.lock:
            if self.test is None:
                return
            lines = [line for line in text.splitlines() if line.strip()]
            self.said += lines
            if self.shown:
                self._write([f"    {line}" for line in lines])


_LIVE: dict[str, Live] = {}  # "live": the session's Live, when there is a terminal to show it on


def pytest_configure(config: pytest.Config) -> None:
    """Show long tests live, and what `parityrun port` says in them."""
    out = _terminal(config)
    if not out:
        return
    live = _LIVE["live"] = Live(config, out)
    real = print

    def print_live(
        *args: object, sep: str | None = " ", end: str | None = "\n", file: TextIO | None = None, flush: bool = False
    ) -> None:
        real(*args, sep=sep, end=end, file=file, flush=flush)
        if file is None:
            live.port_said((" " if sep is None else sep).join(map(str, args)))

    port.__dict__["print"] = print_live


def pytest_unconfigure() -> None:
    """Stop showing, and close the terminal's copy."""
    live = _LIVE.pop("live", None)
    if live:
        live.done.set()
        _ = port.__dict__.pop("print", None)
        with live.lock:
            live.out.close()


def pytest_runtest_logstart(nodeid: str) -> None:
    """Begin timing the test."""
    if "live" in _LIVE:
        _LIVE["live"].start(nodeid)


@pytest.hookimpl(trylast=True)
def pytest_runtest_logfinish() -> None:
    """End timing the test."""
    if "live" in _LIVE:
        _LIVE["live"].finish()
