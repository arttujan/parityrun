"""A per-scenario fingerprint of what a recording was made from.

Covered: the scenario in its parsed YAML form (comments, formatting, key order and line endings don't count) and
the fixtures the scenario names, hashed as bytes with CRLF converted to LF.

Not covered: the project's settings, impls.json and the app's code.

The fingerprint is saved next to the recording as `golden/<name>/fingerprint.json`, never inside recording.json.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, cast

import yaml

if TYPE_CHECKING:
    import pathlib

    from parityrun.project import Project

FILE = "fingerprint.json"  # inside golden/<name>/
FORMAT = 1
NOTED = ".older-recordings-noted"  # inside out/: the note about recordings without a fingerprint has been shown


def _scenario_digest(text: str) -> tuple[str, object]:
    """Return the digest of the scenario's parsed form, and the parsed scenario."""
    parsed = cast("object", yaml.safe_load(text))
    try:
        canonical = json.dumps(parsed, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    except TypeError:  # keys of mixed types cannot be sorted
        canonical = text.replace("\r\n", "\n")
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest(), parsed


def _fixtures(scenario: object) -> list[str]:
    """Return the sorted names of the fixtures the scenario uses."""
    if not isinstance(scenario, dict):
        return []
    top = cast("dict[object, object]", scenario)
    names: set[str] = set()
    files = top.get("files")
    if isinstance(files, dict):
        names.update(str(v) for v in cast("dict[object, object]", files).values())
    steps = top.get("steps")
    if isinstance(steps, list):
        for item in cast("list[object]", steps):
            if isinstance(item, dict):
                names |= _step_fixtures(cast("dict[object, object]", item))
    return sorted(names)


def _step_fixtures(step: dict[object, object]) -> set[str]:
    """Return the fixtures one step reads: its raw body, uploads and llm replies from files."""
    names: set[str] = set()
    if "raw" in step:
        names.add(str(step["raw"]))
    upload = step.get("upload")
    if isinstance(upload, dict):
        names.update(str(v) for v in cast("dict[object, object]", upload).values())
    replies = step.get("llm")
    if isinstance(replies, list):
        for reply in cast("list[object]", replies):
            if isinstance(reply, dict) and "$file" in cast("dict[object, object]", reply):
                names.add(str(cast("dict[object, object]", reply)["$file"]))
    return names


def inputs(project: Project, name: str) -> dict[str, str]:
    """Return the digest of the scenario `name` and of each fixture it names."""
    text = (project.scenarios / f"{name}.yaml").read_text(encoding="utf-8")
    digest, parsed = _scenario_digest(text)
    found = {f"scenarios/{name}.yaml": digest}
    for rel in _fixtures(parsed):
        path = project.fixtures / rel
        found[f"fixtures/{rel}"] = (
            hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest() if path.is_file() else "missing"
        )
    return found


def write(folder: pathlib.Path, found: dict[str, str]) -> None:
    """Save the fingerprint `found` in `folder`."""
    text = json.dumps({"format": FORMAT, "inputs": found}, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
    with (folder / FILE).open("w", encoding="utf-8", newline="\n") as f:
        _ = f.write(text)


def changed(project: Project, name: str) -> list[str] | None:
    """Return the inputs that differ from the recording's fingerprint; None when there is nothing to compare.

    An empty list means unchanged; so does a scenario that can't be read now (the run reports that itself).
    """
    folder = project.golden / name
    if not (folder / "recording.json").exists() or not (folder / FILE).exists():
        return None
    try:
        data = cast("object", json.loads((folder / FILE).read_text(encoding="utf-8")))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    saved = cast("dict[str, object]", data)
    stored = saved.get("inputs")
    if saved.get("format") != FORMAT or not isinstance(stored, dict):
        return None
    before = cast("dict[str, object]", stored)
    try:
        now = inputs(project, name)
    except (OSError, ValueError, yaml.YAMLError):  # unreadable, not UTF-8 or not YAML
        return []  # can't tell; the run reports the error itself
    return sorted(k for k in set(before) | set(now) if before.get(k) != now.get(k))
