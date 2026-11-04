"""End to end: the lines of a real PHP site the scenarios ran, measured by PCOV (or Xdebug) while recording.

Skips without php, and the coverage test also without PCOV or Xdebug; the test of a PHP without them runs with php.
"""

import json
import pathlib
import shutil

import pytest

from parityrun import Project, cli, coverage

SITE = {
    "index.php": """<?php
require __DIR__ . '/lib.php';
if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    echo shout($_POST['word'] ?? '');
    return;
}
echo '<p>Home</p>';
""",
    "lib.php": """<?php
function shout(string $word): string
{
    return strtoupper($word) . '!';
}
""",
    "never.php": "<?php\necho 'never';\n",
}


def _site(tmp_path: pathlib.Path) -> Project:
    app = tmp_path / "site"
    for rel, text in SITE.items():
        (app / rel).parent.mkdir(parents=True, exist_ok=True)
        _ = (app / rel).write_text(text, encoding="utf-8")
    root = app / "parityrun"
    (root / "scenarios").mkdir(parents=True)
    _ = (root / "impls.json").write_text(
        json.dumps({"php": {"cmd": ["php", "-S", "127.0.0.1:{port}", "-t", "."]}}), encoding="utf-8"
    )
    _ = (root / "scenarios" / "home.yaml").write_text("steps:\n  - call: GET /\n", encoding="utf-8")
    _ = (root / "scenarios" / "shout.yaml").write_text(
        "steps:\n  - call: POST /\n    form: {word: hey}\n", encoding="utf-8"
    )
    return Project(root=root, reference="php", isolate=True, coverage=True)


def test_recording_measures_the_lines_each_scenario_ran(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    if not shutil.which("php"):
        pytest.skip("php isn't on PATH")
    if coverage.detect(shutil.which("php") or "php")[1] is None:
        pytest.skip("PHP has neither PCOV nor Xdebug")
    project = _site(tmp_path)
    assert cli.record(project, ["home"], force=False) == 0
    home = json.loads((project.golden / "home" / "coverage.json").read_text(encoding="utf-8"))
    index, lib = home["files"]["index.php"], home["files"]["lib.php"]
    assert 7 in index["run"]
    assert 4 in index["lines"]
    assert 4 not in index["run"]
    assert 4 in lib["lines"]
    assert 4 not in lib["run"]
    assert "never.php" not in home["files"]
    said = capsys.readouterr().out
    assert "PHP code covered:" in said
    assert "never loaded: never.php" in said
    before = coverage.summary(coverage.merge(coverage.golden_reports(project.golden)), project.repo)["percent"]

    assert cli.record(project, ["shout"], force=False) == 0
    after = coverage.summary(coverage.merge(coverage.golden_reports(project.golden)), project.repo)
    assert after["percent"] > before
    shout = json.loads((project.golden / "shout" / "coverage.json").read_text(encoding="utf-8"))
    assert 4 in shout["files"]["lib.php"]["run"]
    assert 4 in shout["files"]["index.php"]["run"]
    rows = {r[0]: r for r in after["rows"]}
    assert rows["never.php"][5] is False
    assert rows["never.php"][1] == 0.0
    assert (
        json.loads((project.golden / "shout" / "recording.json").read_text(encoding="utf-8"))["steps"][0]["text"]
        == "HEY!"
    )  # the measuring doesn't change what the site answers
    _ = capsys.readouterr()
    assert cli.coverage(project) == 0
    report = capsys.readouterr().out
    assert "by 2 scenarios" in report
    assert "never.php" in report
    assert "never loaded" in report


def test_a_php_without_a_coverage_tool_records_as_before_and_says_so_once(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    if not shutil.which("php"):
        pytest.skip("php isn't on PATH")

    def without(_php: str) -> tuple[list[str], str | None]:
        return [], None

    monkeypatch.setattr(coverage, "detect", without)
    monkeypatch.setattr(coverage, "_warned", set[str]())
    project = _site(tmp_path)
    assert cli.record(project, ["home", "shout"], force=False) == 0
    assert not list(project.golden.glob("*/coverage.json"))
    assert capsys.readouterr().out.count("needs the PCOV") == 1
