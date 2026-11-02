"""The bare `parityrun`: it sets parityrun up and records what isn't yet, then shows where the project stands and the
one command to run next; and the short names of the port languages."""

import argparse
import json
import pathlib
import re

import pytest

from parityrun import __version__, cli, load

APP = '''import http.server, os


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'<p>Hi</p><a href="/about">About</a>' if self.path == "/" else b"<p>About us</p>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


http.server.HTTPServer(("127.0.0.1", int(os.environ["PORT"])), Handler).serve_forever()
'''

PROJECT_FILE = '''import pathlib

from parityrun import Project

PROJECT = Project(root=pathlib.Path(__file__).parent, reference="py", html=True, library={library})
'''


def _project(tmp_path: pathlib.Path, impls: tuple[str, ...] = (), routes: tuple[str, ...] = ("GET /",),
             library: bool = False, recorded: bool = True) -> pathlib.Path:
    """An app (shop/) whose reference is a small Python server, with one scenario visiting /; its project file."""
    app = tmp_path / "shop"
    root = app / "parityrun"
    (root / "scenarios").mkdir(parents=True)
    (app / "app.py").write_text(APP, encoding="utf-8")
    spec = {"py": {"cmd": ["{python}", "app.py"], "env": {"PORT": "{port}"}},
            **{name: {"cmd": ["node", "server.js"]} for name in impls}}
    (root / "impls.json").write_text(json.dumps(spec), encoding="utf-8")
    (root / "parityrun_project.py").write_text(PROJECT_FILE.format(library=library), encoding="utf-8")
    (root / "scenarios" / "a.yaml").write_text("steps:\n- call: GET /\n", encoding="utf-8")
    (root / "golden").mkdir()
    (root / "golden" / "endpoints.json").write_text(json.dumps(list(routes)), encoding="utf-8")
    if recorded:
        (root / "golden" / "a").mkdir()
        (root / "golden" / "a" / "recording.json").write_text(cli.dump({"steps": []}), encoding="utf-8")
    return root / "parityrun_project.py"


def _checked(file: pathlib.Path, impl: str, passed: int, total: int) -> None:
    """out/report-<impl>.json as `check` leaves it."""
    out = file.parent / "out"
    out.mkdir(exist_ok=True)
    report = {"a": [{"name": f"c{i}", "ok": i < passed, "detail": []} for i in range(total)]}
    (out / f"report-{impl}.json").write_text(json.dumps(report), encoding="utf-8")


def _status(file: pathlib.Path, capsys) -> str:
    capsys.readouterr()
    assert cli.status(load(file)) == 0
    return capsys.readouterr().out


def test_the_installers_install_this_version(capsys):
    root = pathlib.Path(__file__).resolve().parents[1]
    for name, pattern in (("install.ps1", r'\$version = "([^"]+)"'), ("install.sh", r'^version="([^"]+)"$')):
        found = re.search(pattern, (root / name).read_text(encoding="utf-8"), re.M)
        assert found and found.group(1) == __version__, name
    assert b"\r\n" not in (root / "install.sh").read_bytes()  # sh reads a CR as part of the line
    with pytest.raises(SystemExit):
        cli.main(["--version"])
    assert capsys.readouterr().out == f"parityrun {__version__}\n"


def test_port_languages_have_short_names():
    assert [cli.language(x) for x in ("ts", "JS", "rs", "rust", "TypeScript")] == \
        ["typescript", "javascript", "rust", "rust", "typescript"]
    with pytest.raises(argparse.ArgumentTypeError, match="typescript \\(ts\\)"):
        cli.language("go")


def test_next_is_capture_while_routes_are_not_visited(tmp_path, capsys):
    out = _status(_project(tmp_path, routes=("GET /", "GET /about", "POST /buy")), capsys)
    assert out.startswith("shop: 1 of 1 scenario recorded\n  1/3 routes visited\n")
    assert "Next: parityrun capture\n      use the app in your browser: 2 routes not visited yet" in out


def test_next_is_a_port_once_every_route_is_visited(tmp_path, capsys):
    out = _status(_project(tmp_path), capsys)
    assert "  1/1 routes visited\n" in out and "Next: parityrun port --to ts\n" in out


def test_next_goes_on_with_a_port_that_is_behind_then_checks_one_never_checked(tmp_path, capsys):
    file = _project(tmp_path, impls=("ts", "mine"))
    _checked(file, "ts", 3, 4)
    out = _status(file, capsys)
    assert "  ts: parity 75.0 % at its last check\n  mine: not checked yet\n" in out
    assert "Next: parityrun port --to ts\n      your AI tool goes on from where the port is" in out
    _checked(file, "ts", 4, 4)
    assert "Next: parityrun check --impl mine\n      see what differs" in _status(file, capsys)


def test_done_says_what_the_ports_are_proven_on(tmp_path, capsys):
    file = _project(tmp_path, impls=("ts", "rust"))
    _checked(file, "ts", 4, 4)
    _checked(file, "rust", 4, 4)
    out = _status(file, capsys)
    assert "\nDone: ts, rust behave like the original on 1 scenario (1/1 routes visited).\n" in out
    assert "Next: parityrun check --impl ts\n" in out


def test_a_library_is_told_to_call_more_of_itself(tmp_path, capsys):
    out = _status(_project(tmp_path, routes=("fn slug", "fn price"), library=True), capsys)
    assert "  0/2 functions, methods and classes called\n" in out
    assert "Next: edit scenarios/*.yaml, then: parityrun\n      call the rest of the library: 2 not called" in out


def test_the_bare_command_records_what_isnt_yet_then_shows_the_status(tmp_path, capsys):
    file = _project(tmp_path, recorded=False)
    assert cli.main(["--project", str(file)]) == 0
    out = capsys.readouterr().out
    assert "Recording first: a\na: recorded 1 steps" in out and "shop: 1 of 1 scenario recorded" in out
    assert (file.parent / "golden" / "a" / "recording.json").exists()
    assert cli.main(["--project", str(file)]) == 0
    assert "Recording first" not in capsys.readouterr().out


def test_port_records_first_and_takes_a_short_language_name(tmp_path, capsys):
    file = _project(tmp_path, recorded=False)
    assert cli.main(["--project", str(file), "port", "--to", "rs", "--setup-only"]) == 0
    assert "Recording first: a" in capsys.readouterr().out
    assert "rust" in json.loads((file.parent / "impls.json").read_text(encoding="utf-8"))


def test_in_an_app_with_no_parityrun_yet_the_bare_command_sets_it_up_records_and_says_what_next(
        tmp_path, capsys, monkeypatch):
    app = tmp_path / "shop"
    app.mkdir()
    (app / "app.py").write_text(APP, encoding="utf-8")
    monkeypatch.chdir(app)
    assert cli.main([]) == 0
    out = capsys.readouterr().out
    assert out.startswith("parityrun isn't set up here yet: setting it up first (parityrun init).\n")
    assert "Found: Python app" in out and "Recording first: smoke" in out and "smoke: recorded" in out
    assert out.count("Next:") == 1 and "shop: 1 of 1 scenario recorded" in out
    assert (app / "parityrun" / "golden" / "smoke" / "recording.json").exists()
    assert cli.main([]) == 0
    out = capsys.readouterr().out
    assert "setting it up" not in out and "Recording first" not in out
