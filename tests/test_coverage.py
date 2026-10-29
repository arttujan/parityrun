"""PHP line coverage: requests' coverage merged, files nothing loaded estimated, the php command, the report."""

import json
import os
import pathlib

from parityrun import Project, coverage


def _write(root: pathlib.Path, files: dict) -> pathlib.Path:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    return root


def test_requests_coverage_is_merged_inside_the_app_only(tmp_path):
    root = tmp_path / "app"
    folder = tmp_path / "coverage"
    _write(folder, {
        "1.json": json.dumps({"tool": "pcov", "files": {
            str(root / "index.php"): {"3": 1, "4": -1, "5": -2},
            str(root / "vendor" / "lib" / "x.php"): {"1": 1},
            str(tmp_path / "coverage-prepend.php"): {"1": 1}}}),
        "2.json": json.dumps({"tool": "pcov", "files": {
            str(root / "index.php"): {"4": 1, "9": -1},
            str(root / "src" / "Lib.php"): {"2": -1}}})})
    assert coverage.collect(folder, root) == {"tool": "pcov", "files": {
        "index.php": {"lines": [3, 4, 9], "run": [3, 4]},  # -2 is dead code: never runnable, not counted
        "src/Lib.php": {"lines": [2], "run": []}}}
    assert coverage.collect(tmp_path / "missing", root) is None


def test_scenarios_together_and_line_ranges():
    merged = coverage.merge([{"tool": "pcov", "files": {"a.php": {"lines": [1, 2, 3], "run": [1]}}},
                             {"tool": "pcov", "files": {"a.php": {"lines": [2, 3, 4], "run": [3]}}}])
    assert merged == {"tool": "pcov", "files": {"a.php": {"lines": [1, 2, 3, 4], "run": [1, 3]}}}
    assert coverage.ranges([3, 4, 5, 9, 11, 12]) == "3-5, 9, 11-12" and coverage.ranges([]) == ""


def test_a_file_nothing_loaded_counts_with_an_estimate(tmp_path):
    root = _write(tmp_path, {
        "never.php": "<?php\n// a comment\nnamespace App;\n\nfunction a()\n{\n    return 1;\n}\n#[Attr]\necho a();\n",
        "index.php": "<?php echo 1;\n", "vendor/v.php": "<?php x();\n", "tests/t.php": "<?php y();\n"})
    assert coverage.estimate(root / "never.php") == 4  # function a(), return 1;, #[Attr], echo a();
    assert coverage.php_files(root) == ["index.php", "never.php"]
    s = coverage.summary({"tool": "pcov", "files": {"index.php": {"lines": [1, 2], "run": [1, 2]}}}, root)
    assert (s["run"], s["lines"]) == (2, 6) and round(s["percent"], 1) == 33.3
    assert [(r[0], r[1], r[5]) for r in s["rows"]] == [("never.php", 0.0, False), ("index.php", 100.0, True)]


def test_php_starts_with_coverage_only_when_it_has_a_tool(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(coverage, "_warned", set())
    monkeypatch.setattr(coverage, "detect", lambda php: (["-d", "extension=pcov"], "pcov"))
    cmd = coverage.php_command(["php", "-S", "127.0.0.1:1", "-t", "."], tmp_path, tmp_path / "prepend.php")
    assert cmd == ["php", "-d", "extension=pcov", "-d", "pcov.enabled=1",
                   "-d", f"pcov.directory={os.path.realpath(tmp_path)}",
                   "-d", f"auto_prepend_file={tmp_path / 'prepend.php'}", "-S", "127.0.0.1:1", "-t", "."]
    assert "PARITYRUN_COVERAGE" in (tmp_path / "prepend.php").read_text(encoding="utf-8")
    monkeypatch.setattr(coverage, "detect", lambda php: ([], None))
    plain = ["php", "-S", "127.0.0.1:1"]
    assert coverage.php_command(plain, tmp_path, tmp_path / "p.php") == plain
    assert coverage.php_command(plain, tmp_path, tmp_path / "p.php") == plain
    assert capsys.readouterr().out.count("PCOV") == 1  # said once, not per run
    assert coverage.is_php(["C:/php/php.exe"]) and not coverage.is_php(["node", "x.php"])


def test_the_report_lists_what_no_scenario_ran(tmp_path, capsys):
    root = _write(tmp_path / "app", {"index.php": "<?php\n", "lib.php": "<?php\nf();\ng();\n"})
    _write(tmp_path / "golden", {
        "a/coverage.json": json.dumps({"tool": "pcov", "files": {"index.php": {"lines": [2, 3, 4], "run": [2]}}}),
        "b/coverage.json": json.dumps({"tool": "pcov", "files": {"index.php": {"lines": [2, 3, 4], "run": [3]}}})})
    assert coverage.show(tmp_path / "golden", root)
    said = capsys.readouterr().out
    assert said.splitlines() == ["PHP code covered: 40.0 % of lines (2/5) by 2 scenarios",
                                 "  lib.php      0.0 %  never loaded (~2 lines)",
                                 "  index.php   66.7 %  not run: lines 4"]
    assert not coverage.show(tmp_path / "nothing", root)


def test_coverage_is_off_by_default(tmp_path):
    assert Project(root=tmp_path).coverage is False
