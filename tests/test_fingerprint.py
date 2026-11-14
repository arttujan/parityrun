from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from parityrun import Project, fingerprint

if TYPE_CHECKING:
    import pathlib


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _scenario(project: Project, text: str, name: str = "a") -> None:
    project.scenarios.mkdir(parents=True, exist_ok=True)
    _ = (project.scenarios / f"{name}.yaml").write_bytes(text.encode("utf-8"))


def _recorded(project: Project, name: str = "a") -> None:
    folder = project.golden / name
    folder.mkdir(parents=True, exist_ok=True)
    _ = (folder / "recording.json").write_bytes(b"{}")
    fingerprint.write(folder, fingerprint.inputs(project, name))


def test_a_scenarios_fingerprint_is_its_parsed_steps(tmp_path: pathlib.Path) -> None:
    project = Project(root=tmp_path)
    _scenario(project, "steps:\n- call: GET /\n")
    want = {"scenarios/a.yaml": _sha(b'{"steps":[{"call":"GET /"}]}')}
    assert fingerprint.inputs(project, "a") == want


def test_line_endings_comments_and_key_order_dont_count(tmp_path: pathlib.Path) -> None:
    project = Project(root=tmp_path)
    _scenario(project, "steps:\n- call: GET /\n  status: 200\n")
    before = fingerprint.inputs(project, "a")
    _scenario(project, "# a comment\r\nsteps:\r\n    -   status: 200\r\n        call: GET /\r\n")
    assert fingerprint.inputs(project, "a") == before


def test_a_changed_step_is_a_changed_scenario(tmp_path: pathlib.Path) -> None:
    project = Project(root=tmp_path)
    _scenario(project, "steps:\n- call: GET /\n")
    _recorded(project)
    assert fingerprint.changed(project, "a") == []
    _scenario(project, "steps:\n- call: GET /about\n")
    assert fingerprint.changed(project, "a") == ["scenarios/a.yaml"]


def test_the_fixtures_a_scenario_uses_are_part_of_it(tmp_path: pathlib.Path) -> None:
    project = Project(root=tmp_path)
    project.fixtures.mkdir()
    for fixture in ("up.png", "body.bin", "data.txt", "reply.json", "unused.txt"):
        _ = (project.fixtures / fixture).write_bytes(b"one\n")
    _scenario(
        project,
        "files:\n  data/x.txt: data.txt\n"
        "steps:\n"
        "- call: POST /up\n  upload:\n    f: up.png\n"
        "- call: POST /raw\n  raw: body.bin\n"
        "- call: POST /llm\n  llm:\n  - $file: reply.json\n  - {plain: 1}\n",
    )
    found = fingerprint.inputs(project, "a")
    keys = {"scenarios/a.yaml", "fixtures/up.png", "fixtures/body.bin", "fixtures/data.txt", "fixtures/reply.json"}
    assert set(found) == keys
    _recorded(project)
    _ = (project.fixtures / "unused.txt").write_bytes(b"other\n")
    assert fingerprint.changed(project, "a") == []
    _ = (project.fixtures / "data.txt").write_bytes(b"two\n")
    assert fingerprint.changed(project, "a") == ["fixtures/data.txt"]
    _ = (project.fixtures / "data.txt").write_bytes(b"one\r\n")
    assert fingerprint.changed(project, "a") == []


def test_a_missing_fixture_is_noted_then_seen_when_it_appears(tmp_path: pathlib.Path) -> None:
    project = Project(root=tmp_path)
    _scenario(project, "steps:\n- call: POST /raw\n  raw: body.bin\n")
    assert fingerprint.inputs(project, "a")["fixtures/body.bin"] == "missing"
    _recorded(project)
    project.fixtures.mkdir()
    _ = (project.fixtures / "body.bin").write_bytes(b"x")
    assert fingerprint.changed(project, "a") == ["fixtures/body.bin"]


def test_a_recording_without_a_fingerprint_is_never_changed(tmp_path: pathlib.Path) -> None:
    project = Project(root=tmp_path)
    _scenario(project, "steps:\n- call: GET /\n")
    assert fingerprint.changed(project, "a") is None  # no recording.json
    folder = project.golden / "a"
    folder.mkdir(parents=True)
    _ = (folder / "recording.json").write_bytes(b"{}")
    _scenario(project, "steps:\n- call: GET /about\n")
    assert fingerprint.changed(project, "a") is None  # no sidecar
    _ = (folder / fingerprint.FILE).write_bytes(json.dumps({"format": 99, "inputs": {}}).encode("utf-8"))
    assert fingerprint.changed(project, "a") is None
    _ = (folder / fingerprint.FILE).write_bytes(b"not json")
    assert fingerprint.changed(project, "a") is None


def test_the_fingerprint_file_is_saved_in_a_stable_form(tmp_path: pathlib.Path) -> None:
    fingerprint.write(tmp_path, {"scenarios/a.yaml": "ab", "fixtures/x": "cd"})
    want = b'{\n "format": 1,\n "inputs": {\n  "fixtures/x": "cd",\n  "scenarios/a.yaml": "ab"\n }\n}\n'
    assert (tmp_path / fingerprint.FILE).read_bytes() == want
    project = Project(root=tmp_path)
    _scenario(project, "steps:\n- call: GET /\n")
    _recorded(project)
    _scenario(project, "steps: [unclosed\n")
    assert fingerprint.changed(project, "a") == []
