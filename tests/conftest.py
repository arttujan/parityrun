"""Live status while the tests run: pytest only reports a test when it ends, and it captures what the test prints,
so a test that runs for minutes (a real AI tool porting a site) looks stuck. Here a test that runs past FIRST
seconds gets its name and running time on the terminal, again every EVERY seconds, and what `parityrun port` says
(each turn as it starts and ends) as it says it. Quick tests print nothing extra. PARITYRUN_TEST_LIVE="FIRST,EVERY"
sets the two times (test_live.py does).

The lines go to a copy of the terminal's own file descriptor, past pytest's capture, so a test still sees all of
its output (capsys) and nothing of this.
"""

import os
import sys
import threading
import time

import pytest

from parityrun import port

FIRST, EVERY = (float(x) for x in os.environ.get("PARITYRUN_TEST_LIVE", "10,30").split(","))


def _clock(seconds: float) -> str:
    return f"{int(seconds) // 60}:{int(seconds) % 60:02d}"


class Live:
    def __init__(self, config: pytest.Config) -> None:
        self.plugins = config.pluginmanager
        self.echo = config.getoption("capture") != "no"  # with -s, port's lines are on the terminal already
        self.lock = threading.Lock()
        self.test, self.started, self.next, self.shown, self.said = None, 0.0, 0.0, False, []
        capman = config.pluginmanager.getplugin("capturemanager")
        try:
            if capman:
                with capman.global_and_fixture_disabled():
                    fd = os.dup(sys.stdout.fileno())
            else:
                fd = os.dup(sys.stdout.fileno())
            self.out = open(fd, "w", encoding=sys.stdout.encoding or "utf-8", errors="replace", buffering=1)
        except (AttributeError, OSError, ValueError):  # no real terminal behind stdout: nothing to show live
            self.out = None
        self.done = threading.Event()
        if self.out:
            threading.Thread(target=self._beat, daemon=True).start()

    @property
    def reporter(self):
        """pytest's terminal reporter, looked up when needed: this conftest is configured before pytest makes it."""
        return self.plugins.getplugin("terminalreporter")

    def _write(self, lines: list[str]) -> None:
        try:
            self.out.write("".join(line + "\n" for line in lines))
        except (OSError, ValueError):
            pass

    def _beat(self) -> None:
        while not self.done.wait(0.2):
            with self.lock:
                now = time.time()
                if self.test is None or now < self.next:
                    continue
                self.next = now + EVERY
                lines = [f"[running {_clock(now - self.started)}] {self.test}"]
                if not self.shown:  # off the dots line pytest is in the middle of
                    lines = ([""] if self.reporter and self.reporter._tw.width_of_current_line else []) + lines
                    lines += [f"    {line}" for line in self.said]
                    self.shown = True
                self._write(lines)

    def start(self, test: str) -> None:
        with self.lock:
            now = time.time()
            self.test, self.started, self.next, self.shown, self.said = test, now, now + FIRST, False, []

    def finish(self) -> None:
        with self.lock:
            test, seconds, shown = self.test, time.time() - self.started, self.shown
            self.test = None
        if shown and self.reporter:  # through pytest's own writer, so its dots and percentages go on from here
            self.reporter.write_line(f"[done {_clock(seconds)}] {test}")

    def port_said(self, text: str) -> None:
        if not self.echo:
            return
        with self.lock:
            if self.test is None:
                return
            lines = [line for line in text.splitlines() if line.strip()]
            self.said += lines
            if self.shown:
                self._write([f"    {line}" for line in lines])


_live: Live | None = None


def pytest_configure(config: pytest.Config) -> None:
    global _live
    _live = Live(config)
    if not _live.out:
        return
    real = print

    def print_live(*args, sep=" ", end="\n", file=None, flush=False):
        real(*args, sep=sep, end=end, file=file, flush=flush)
        if file is None:
            _live.port_said(sep.join(map(str, args)))

    port.print = print_live


def pytest_unconfigure(config: pytest.Config) -> None:
    if _live and _live.out:
        _live.done.set()
        vars(port).pop("print", None)
        with _live.lock:
            _live.out.close()


def pytest_runtest_logstart(nodeid: str, location) -> None:
    if _live and _live.out:
        _live.start(nodeid)


@pytest.hookimpl(trylast=True)
def pytest_runtest_logfinish(nodeid: str, location) -> None:
    if _live and _live.out:
        _live.finish()
