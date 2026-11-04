"""`parityrun port`: the port's folder and impls.json entry, PORTING.md, the loop, and a stand-in AI tool at work.

Also the impls.json keys a port needs (env, build, skip), the loop's focus, guard and stop rules, and end to end, per
language, the stand-in AI tool porting the guestbook in two turns.

The end-to-end tests skip when their toolchain is missing: node for javascript, node 23.6+ for typescript, cargo for
rust (the JavaScript guestbook is the reference, so PHP isn't needed).
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess
import sys
from typing import TYPE_CHECKING

import httpx
import pytest

from parityrun import cli, commands, load, runner
from parityrun import port as porting

if TYPE_CHECKING:
    from parityrun.jsontypes import Data

GUESTBOOK = pathlib.Path(__file__).resolve().parents[1] / "examples" / "guestbook"
CART = GUESTBOOK.parent / "cart"
AGENT = pathlib.Path(__file__).resolve().parent / "port_agent.py"

PROJECT_FILE = """import pathlib

from parityrun import Project, RunInfo


def env(run: RunInfo) -> dict:
    return {"SHOP_DATA": str(run.data_dir)}


PROJECT = Project(root=pathlib.Path(__file__).parent, reference="php", env=env, html=True, isolate=True)
"""

RECORDING: Data = {"steps": [{"call": "GET /", "status": 200, "html": ["<p>", "Hi", "</p>"]}], "db": {}, "files": {}}

SERVER = """import http.server, os
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(os.environ["GREETING"].encode())
http.server.HTTPServer(("127.0.0.1", int(os.environ["PORT"])), Handler).serve_forever()
"""


def _shop(tmp_path: pathlib.Path, impls: Data | None = None) -> pathlib.Path:
    """Set an app (shop/) up as `init` leaves it, with one recorded scenario; return its project file."""
    root = tmp_path / "shop" / "parityrun"
    (root / "scenarios").mkdir(parents=True)
    (root / "golden" / "a").mkdir(parents=True)
    _ = (tmp_path / "shop" / "index.php").write_text("<p>Hi</p>\n", encoding="utf-8")
    spec = impls or {"php": {"cmd": ["php", "-S", "127.0.0.1:{port}", "-t", "."]}}
    _ = (root / "impls.json").write_text(json.dumps(spec), encoding="utf-8")
    _ = (root / "parityrun_project.py").write_text(PROJECT_FILE, encoding="utf-8")
    _ = (root / "scenarios" / "a.yaml").write_text("steps:\n- call: GET /\n", encoding="utf-8")
    _ = (root / "golden" / "a" / "recording.json").write_text(commands.dump(RECORDING), encoding="utf-8")
    return root / "parityrun_project.py"


def test_a_typescript_port_is_set_up_beside_the_app_with_its_task(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    file = _shop(tmp_path)
    monkeypatch.setenv("PARITYRUN_PROJECT", str(file))  # the project, from any folder
    monkeypatch.chdir(tmp_path)
    assert cli.main(["port", "--to", "typescript", "--setup-only"]) == 0
    folder = tmp_path / "shop-ts"
    assert f"port is in {folder} (new); impls.json calls it ts" in capsys.readouterr().out
    assert json.loads((file.parent / "impls.json").read_text(encoding="utf-8"))["ts"] == {
        "cwd": "../shop-ts",
        "cmd": ["node", "server.ts"],
        "env": {"PORT": "{port}"},
    }
    assert json.loads((folder / "package.json").read_text(encoding="utf-8"))["type"] == "module"
    assert "process.env.PORT" in (folder / "server.ts").read_text(encoding="utf-8")
    task = (folder / "PORTING.md").read_text(encoding="utf-8")
    root = file.parent.as_posix()
    assert task.startswith("# Port shop to TypeScript")
    assert "| `SHOP_DATA` | `<data>` |" in task
    assert "| `PORT` | `8123` |" in task
    assert f"`a`: 1 step in `{root}/scenarios/a.yaml`" in task
    assert f"`{root}/golden/a/recording.json`" in task
    assert "parityrun check --impl ts" in task
    assert file.as_posix() in task
    assert "no `enum`" in task
    assert "app/<path>" in task

    _ = (folder / "server.ts").write_text("// ported\n", encoding="utf-8")
    target = porting.plan(load(file), "typescript")
    assert porting.set_up(load(file), target) is False  # there is work in it: the next turn goes on from there
    assert (folder / "server.ts").read_text(encoding="utf-8") == "// ported\n"


def test_a_rust_port_is_built_by_cargo_and_copied_without_target(tmp_path: pathlib.Path) -> None:
    project = load(_shop(tmp_path))
    target = porting.plan(project, "rust", into=str(tmp_path / "Shop 2"))
    assert target.spec == {
        "cwd": "../Shop 2",
        "cmd": ["{repo}/../Shop 2/target/release/shop-2"],
        "env": {"PORT": "{port}"},
        "build": ["cargo", "build", "--release", "--quiet"],
        "skip": ["target"],
    }
    assert porting.set_up(project, target) is True
    manifest = (tmp_path / "Shop 2" / "Cargo.toml").read_text(encoding="utf-8")
    assert 'name = "shop-2"' in manifest
    assert "\n[workspace]\n" in manifest
    (tmp_path / "Shop 2" / "target" / "release").mkdir(parents=True)
    _ = (tmp_path / "Shop 2" / "target" / "release" / "shop-2").write_text("built", encoding="utf-8")
    copied = runner.copy_app(runner.Implementation(project, "rust"), tmp_path / "copy")
    assert sorted(copied) == [".gitignore", "Cargo.toml", "src/main.rs"]


def test_a_name_impls_json_gives_another_folder_is_refused(tmp_path: pathlib.Path) -> None:
    project = load(_shop(tmp_path, {"php": {"cmd": ["php"]}, "js": {"cwd": "../elsewhere", "cmd": ["node"]}}))
    with pytest.raises(SystemExit, match=r"impls\.json has 'js' already"):
        _ = porting.set_up(project, porting.plan(project, "javascript"))
    target = porting.plan(project, "javascript", name="js-port")
    assert porting.set_up(project, target) is True
    spec = (project.root / "impls.json").read_text(encoding="utf-8")
    assert spec.splitlines()[1:3] == ['  "php": {"cmd": ["php"]},', '  "js": {"cwd": "../elsewhere", "cmd": ["node"]},']


def test_an_implementation_is_built_first_and_started_with_its_own_environment(tmp_path: pathlib.Path) -> None:
    fail = ["{python}", "-c", "import sys; print('no compiler here'); sys.exit(2)"]
    project = load(
        _shop(
            tmp_path,
            {
                "php": {"cmd": ["php"]},
                "py": {
                    "cmd": ["{python}", "-c", SERVER],
                    "env": {"PORT": "{port}", "GREETING": "hi from {repo}"},
                    "build": ["{python}", "-c", "pass"],
                },
                "broken": {"cmd": ["{python}", "-c", SERVER], "build": fail},
            },
        )
    )
    impl = runner.Implementation(project, "py")
    assert impl.build() is None
    assert "no compiler here" in (runner.Implementation(project, "broken").build() or "")
    (tmp_path / "data").mkdir()
    proc, base = impl.start(tmp_path / "data", 1)
    try:
        assert httpx.get(base + "/").text == f"hi from {project.repo}"
    finally:
        proc.kill()
        _ = proc.wait()
    assert commands.check(project, "broken", ["a"]) == 1  # a failed build: nothing runs, parity 0
    report = json.loads((project.out / "report-broken.json").read_text(encoding="utf-8"))
    assert report["build"][0]["ok"] is False
    assert "no compiler here" in report["build"][0]["detail"]


def test_focus_takes_one_red_scenario_at_a_time_and_sets_aside_one_that_stays_red() -> None:
    focus = porting.Focus(patience=2)
    for passed, want in [((1, 0), "a"), ((2, 0), "b"), ((2, 1), "b"), ((2, 1), "b")]:
        focus.update(porting.Result({"a": (passed[0], 2), "b": (passed[1], 3)}))
        assert focus.current == want
        assert not focus.stuck
    focus.update(porting.Result({"a": (1, 2), "b": (1, 3)}))  # b's second turn without getting better; a broke
    assert (focus.current, focus.set_aside, focus.stuck) == ("a", ["b"], False)
    focus.update(porting.Result({"a": (1, 2), "b": (1, 3)}))
    focus.update(porting.Result({"a": (1, 2), "b": (3, 3)}))
    assert (focus.current, focus.set_aside, focus.stuck) == (None, ["b", "a"], True)

    broken = porting.Focus(patience=2)
    for stuck in (False, True):
        broken.update(porting.Result({"a": (0, 2)}, problem="It doesn't build"))
        assert broken.stuck is stuck


def test_guard_puts_the_parityrun_folder_back_and_sees_the_original_change(tmp_path: pathlib.Path) -> None:
    project = load(_shop(tmp_path))
    guard = porting.Guard(project, tmp_path / "shop", tmp_path / "shop-ts")
    golden = project.golden / "a" / "recording.json"
    _ = golden.write_text("{}", encoding="utf-8")
    (project.scenarios / "a.yaml").unlink()
    _ = (project.scenarios / "new.yaml").write_text("steps: []\n", encoding="utf-8")
    (project.root / "__pycache__").mkdir(exist_ok=True)  # loading the project file may have made it already
    _ = (project.root / "__pycache__" / "p.pyc").write_bytes(b"")
    (project.out / "ts").mkdir(parents=True)
    _ = (project.out / "ts" / "recording.json").write_text("{}", encoding="utf-8")  # what check writes
    assert guard.restore() == ["golden/a/recording.json", "scenarios/a.yaml", "scenarios/new.yaml"]
    assert json.loads(golden.read_text(encoding="utf-8")) == RECORDING
    assert not (project.scenarios / "new.yaml").exists()
    assert (project.root / "__pycache__" / "p.pyc").exists()
    assert guard.restore() == []
    assert guard.original_changed() == []
    _ = (tmp_path / "shop" / "index.php").write_text("<p>Changed</p>\n", encoding="utf-8")
    assert guard.original_changed() == ["index.php"]


def test_the_agent_command_for_claude_code_and_for_your_own_tool(tmp_path: pathlib.Path) -> None:
    project = load(_shop(tmp_path))
    target = porting.plan(project, "javascript")
    reads = [tmp_path / "shop", project.root]
    cmd = porting.agent_command("claude", target, reads, tmp_path / "turn-1.prompt.md")
    assert cmd[1:4] == ["-p", "--permission-mode", "acceptEdits"]
    assert "Bash(parityrun check *)" in cmd
    assert "Bash(npm *)" in cmd
    assert "Bash(cargo *)" not in cmd
    assert cmd[-4:] == ["--add-dir", str(reads[0]), "--add-dir", str(reads[1])]  # the prompt comes on stdin
    own = porting.agent_command("mytool --task {prompt_file} --in {folder}", target, reads, tmp_path / "t.md")
    assert own[1:] == ["--task", str(tmp_path / "t.md"), "--in", str(target.folder)]


def test_the_loop_feeds_back_the_failures_puts_its_folder_back_and_stops_when_nothing_gets_better(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    file = _shop(tmp_path)
    project = load(file)
    failing: list[Data] = [{"name": "step 0 GET / html", "ok": False, "detail": ["[1]: 'Hi' != 'Hello'"]}]

    def evaluate(*_: object) -> porting.Result:
        return porting.Result({"a": (1, 2)}, {"a": failing})

    def which(name: str) -> str:
        return f"/usr/bin/{name}"  # node and the tool "are there"

    monkeypatch.setattr(porting, "evaluate", evaluate)
    monkeypatch.setattr(porting.shutil, "which", which)
    prompts: list[str] = []

    def agent(run: porting.Agent, task: str, _log: pathlib.Path) -> int:
        prompts.append(task)
        assert run.env["PARITYRUN_PROJECT"] == str(file)
        assert run.cmd[0] == "/usr/bin/mytool"
        _ = (project.golden / "a" / "recording.json").write_text("{}", encoding="utf-8")  # not allowed
        return 0

    monkeypatch.setattr(porting, "run_agent", agent)
    options = porting.Options("javascript", agent="mytool {prompt_file}", patience=3, project_file=file)
    code = porting.port(project, options)
    assert code == 1
    assert len(prompts) == 4
    assert prompts[0].startswith("Turn 1 of at most 20.")
    assert "Port the whole app now" in prompts[0]
    assert "This turn: make scenario `a` pass." in prompts[1]
    assert "FAIL step 0 GET / html" in prompts[1]
    assert "[1]: 'Hi' != 'Hello'" in prompts[1]
    assert "put back what the last turn changed" in prompts[1]
    assert json.loads((project.golden / "a" / "recording.json").read_text(encoding="utf-8")) == RECORDING
    said = capsys.readouterr().out
    assert "Stopped: a stayed red for 3 turns each without getting better" in said
    log = [json.loads(line) for line in (project.out / "port-log.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [e["focus"] for e in log] == [None, "a", "a", "a"]
    assert log[0]["restored"] == ["golden/a/recording.json"]
    assert log[0]["parity"] == 50.0
    assert (project.out / "port" / "turn-4.prompt.md").read_text(encoding="utf-8") == prompts[3]


def test_porting_needs_recordings_first(tmp_path: pathlib.Path) -> None:
    project = load(_shop(tmp_path))
    (project.golden / "a" / "recording.json").unlink()
    with pytest.raises(SystemExit, match=r"Record first: `parityrun record` \(a not recorded yet\)"):
        _ = porting.port(project, porting.Options("rust"))


def _needs(language: str) -> None:
    tool = "cargo" if language == "rust" else "node"
    if not shutil.which(tool) or not shutil.which("node"):  # node also runs the reference
        pytest.skip(f"{tool} isn't on PATH")
    if language == "typescript":
        node = shutil.which("node") or "node"
        version = subprocess.run([node, "--version"], capture_output=True, text=True, check=False).stdout  # noqa: S603
        if tuple(int(x) for x in re.findall(r"\d+", version)[:2]) < (23, 6):
            pytest.skip(f"node {version.strip()} can't run TypeScript as is (needs 23.6 or newer)")


@pytest.mark.parametrize("language", ["javascript", "typescript", "rust"])
def test_an_ai_tool_ports_the_guestbook_in_two_turns(tmp_path: pathlib.Path, language: str) -> None:
    """Turn 1 ports it with a reworded message, so `validation` fails; turn 2 is told exactly that, and fixes it."""
    _needs(language)
    root = tmp_path / "guestbook"
    _ = shutil.copytree(GUESTBOOK, root, ignore=shutil.ignore_patterns("golden", "out", "php", "ts", "rust"))
    _ = (root / "impls.json").write_text(
        json.dumps({"js": {"cwd": "js", "cmd": ["node", "server.js", "{port}"]}}), encoding="utf-8"
    )
    file = root / "parityrun_project.py"
    _ = file.write_text(file.read_text(encoding="utf-8").replace('reference="php"', 'reference="js"'), encoding="utf-8")
    assert cli.main(["--project", str(file), "record"]) == 0
    agent = f'"{pathlib.Path(sys.executable).as_posix()}" "{AGENT.as_posix()}" {{prompt_file}}'
    assert (
        cli.main(
            [
                "--project",
                str(file),
                "port",
                "--to",
                language,
                "--into",
                str(tmp_path / "port"),
                "--name",
                "port",
                "--agent",
                agent,
            ]
        )
        == 0
    )
    log = [json.loads(line) for line in (root / "out" / "port-log.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [e["agent_exit"] for e in log] == [0, 0]
    assert [e["focus"] for e in log] == [None, "validation"]
    assert [n for n, (p, t) in log[0]["scenarios"].items() if p < t] == ["validation"]
    assert log[0]["problem"] is None
    assert log[1]["parity"] == 100
    second = (root / "out" / "port" / "turn-2.prompt.md").read_text(encoding="utf-8")
    assert "This turn: make scenario `validation` pass, and keep delete, sign green." in second
    assert "Please write your name." in second
    assert "Please enter your name." in second
    assert json.loads((root / "impls.json").read_text(encoding="utf-8"))["port"]["env"] == {"PORT": "{port}"}
    assert cli.main(["--project", str(file), "check", "--impl", "port"]) == 0


def _cart(tmp_path: pathlib.Path) -> pathlib.Path:
    """Copy the cart library's scenarios, its JavaScript port the reference (node is all it needs)."""
    root = tmp_path / "cart"
    _ = shutil.copytree(CART, root, ignore=shutil.ignore_patterns("golden", "out", "php", "ts", "rust"))
    _ = (root / "impls.json").write_text(
        json.dumps({"js": {"cwd": "js", "cmd": ["node", "shim.js"], "env": {"PORT": "{port}"}}}), encoding="utf-8"
    )
    file = root / "parityrun_project.py"
    _ = file.write_text(file.read_text(encoding="utf-8").replace('reference="php"', 'reference="js"'), encoding="utf-8")
    return file


@pytest.mark.parametrize(
    ("language", "files"),
    [
        ("javascript", ["index.js", "shim.js"]),
        ("typescript", ["index.ts", "shim.ts"]),
        ("rust", ["src/lib.rs", "src/main.rs", "src/json.rs"]),
    ],
)
def test_a_librarys_port_starts_as_an_empty_library_and_parityruns_shim(
    tmp_path: pathlib.Path, language: str, files: list[str]
) -> None:
    project = load(_cart(tmp_path))
    target = porting.plan(project, language, into=str(tmp_path / "Cart Port"), name="port")
    assert set(files) <= set(target.files)
    if language == "rust":
        assert "use cart_port as lib;" in target.files["src/main.rs"]
        assert target.files["src/json.rs"] == (runner.SHIMS / "json.rs").read_text(encoding="utf-8")
    else:
        assert target.spec["cmd"] == ["node", files[1]]
        assert target.files[files[1]] == (runner.SHIMS / f"library.{files[1][-2:]}").read_text(encoding="utf-8")
    assert porting.set_up(project, target) is True
    task = porting.porting_md(project, target, None)
    assert "You are porting the library in" in task
    assert "## How the scenarios call it" in task
    assert ("`Object`" if language == "rust" else "FUNCTIONS") in task
    assert "`result`" in task
    assert "pages as normalised lines" not in task
    first = porting.prompt(1, 20, project, target, porting.Progress(porting.Focus(3)))
    assert "Port the whole library now" in first


@pytest.mark.parametrize("language", ["javascript", "typescript", "rust"])
def test_an_ai_tool_ports_the_cart_library_in_two_turns(tmp_path: pathlib.Path, language: str) -> None:
    """Turn 1 rounds like the target language does, so one call of `functions` fails; turn 2 is told, and fixes it."""
    _needs(language)
    file = _cart(tmp_path)
    assert cli.main(["--project", str(file), "record"]) == 0
    agent = f'"{pathlib.Path(sys.executable).as_posix()}" "{AGENT.as_posix()}" {{prompt_file}}'
    assert (
        cli.main(
            [
                "--project",
                str(file),
                "port",
                "--to",
                language,
                "--into",
                str(tmp_path / "port"),
                "--name",
                "port",
                "--agent",
                agent,
            ]
        )
        == 0
    )
    root = file.parent
    log = [json.loads(line) for line in (root / "out" / "port-log.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [e["agent_exit"] for e in log] == [0, 0]
    assert [e["focus"] for e in log] == [None, "functions"]
    assert log[0]["scenarios"]["functions"] == [8, 9]
    assert log[1]["parity"] == 100
    second = (root / "out" / "port" / "turn-2.prompt.md").read_text(encoding="utf-8")
    assert "This turn: make scenario `functions` pass, and keep cart, json green." in second
    assert "FAIL step 6 fn Cart\\vat result" in second
    assert "value: -1 != 0" in second
    assert cli.main(["--project", str(file), "check", "--impl", "port"]) == 0
