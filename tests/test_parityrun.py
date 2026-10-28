"""parityrun's own logic: normalising, templates, comparisons, the stub, project settings, and one real run."""

import json
import pathlib
import shutil

import httpx
import pytest
from PIL import Image, ImageDraw

from parityrun import Project, Stub, cli, load, runner, snapshot
from parityrun.normalize import Normalizer

EXAMPLE = pathlib.Path(__file__).resolve().parents[1] / "examples" / "notes"


def test_ids_are_numbered_by_a_sorted_walk_and_found_inside_strings():
    norm = Normalizer()
    raw = {"z": "ab12cd34", "a": {"id": "0f0f0f0f", "url": "/api/posts/ab12cd34/image",
                                  "at": "2026-10-18T09:15:02+00:00", "note": "made 2026-10-18 12:00"}}
    norm.collect(raw)
    out = norm.apply(raw)
    assert out["a"]["id"] == "<id:1>" and out["z"] == "<id:2>"   # "a" sorts before "z"
    assert out["a"]["url"] == "/api/posts/<id:2>/image"
    assert out["a"]["at"] == "<time>" and out["a"]["note"] == "made <time>"
    assert norm.apply({"n": 1760000000, "small": 42, "flag": True}) == {"n": "<epoch>", "small": 42, "flag": True}


def test_salted_hashes_and_run_paths_are_normalised():
    norm = Normalizer({"C:/tmp/run1/data": "<data>", "127.0.0.1:5555": "<stub>"})
    out = norm.apply({"hash": "pbkdf2_sha256$240000$abcdef0123$fedcba9876", "dir": "C:/tmp/run1/data/jobs",
                      "url": "http://127.0.0.1:5555/v1"})
    assert out == {"hash": "pbkdf2_sha256$240000$<salt>$<hash>", "dir": "<data>/jobs", "url": "http://<stub>/v1"}


def test_templates_keep_the_type_of_a_whole_value():
    ctx = {"made": [{"id": "ab12cd34", "n": 3}]}
    assert runner.render("/api/posts/{{made[0].id}}/image", ctx) == "/api/posts/ab12cd34/image"
    assert runner.render({"count": "{{made[0].n}}", "list": ["{{ made[0].id }}"]}, ctx) == {"count": 3, "list": ["ab12cd34"]}


def test_merge_ignore_until_and_comma_guard():
    ctx = {"ch": {"id": "a", "name": "A", "niche": "x"}}
    assert runner.render({"$merge": "{{ch}}", "name": "B"}, ctx) == {"id": "a", "name": "B", "niche": "x"}
    data = {"tools": {"ollama": {"running": True, "found": True}}, "gpus": [{"driver": "1"}, {"driver": "2"}]}
    runner._blank(data, "tools.*.running".split("."))
    runner._blank(data, "gpus.*.driver".split("."))
    assert data == {"tools": {"ollama": {"running": "<ignored>", "found": True}},
                    "gpus": [{"driver": "<ignored>"}, {"driver": "<ignored>"}]}
    assert runner._comma_split([{"body": {"title": "Ask AI", "then pick one": None}}]) == ["then pick one"]
    assert runner._met({"worker": {"busy": False}}, {"path": "worker.busy", "equals": False})
    assert runner._met([1, 2, 3], {"length": 3}) and not runner._met([1], {"length": 3})


def test_measured_numbers_and_their_companions_are_blanked_only_together():
    out = runner.measured({"a": {"seconds": 1.5, "cost_usd": 0.2}, "b": {"cost_usd": 0.2}, "c": {"seconds": True}},
                          {"seconds"}, {"cost_usd"})
    assert out == {"a": {"seconds": "<measured>", "cost_usd": "<measured>"}, "b": {"cost_usd": 0.2},
                   "c": {"seconds": True}}   # a cost with no time beside it is real; a flag isn't a measurement


def test_checks_name_what_differs(tmp_path):
    golden = {"steps": [{"call": "GET /api/x", "status": 200, "json": {"a": 1, "b": [1, 2]}}],
              "db": {"schema": {"t": [["id", "TEXT", 0, None, 1]]}, "rows": {"t": [{"id": "1"}, {"id": "2"}]}},
              "files": {"note.txt": {"text": "hi"}}}
    same = json.loads(json.dumps(golden))
    same["db"]["rows"]["t"].reverse()   # row order isn't part of the contract
    assert all(c["ok"] for c in runner.checks(golden, same, tmp_path, tmp_path))
    other = json.loads(json.dumps(golden))
    other["steps"][0]["json"]["b"] = [2, 1]   # list order in a response is
    other["files"]["note.txt"]["text"] = "hello"
    failed = {c["name"]: c["detail"] for c in runner.checks(golden, other, tmp_path, tmp_path) if not c["ok"]}
    assert set(failed) == {"step 0 GET /api/x json", "file note.txt"}
    assert failed["step 0 GET /api/x json"][0] == "b[0]: 1 != 2"


def test_interleaved_files_compare_without_line_order_only_when_the_project_says_so(tmp_path):
    golden = {"steps": [], "db": {}, "files": {"jobs/1/log.jsonl": {"jsonl": [{"a": 1}, {"b": 2}]}}}
    swapped = {"steps": [], "db": {}, "files": {"jobs/1/log.jsonl": {"jsonl": [{"b": 2}, {"a": 1}]}}}
    project = Project(root=tmp_path, interleaved_files={"log.jsonl"})
    assert all(c["ok"] for c in runner.checks(golden, swapped, tmp_path, tmp_path, project))
    assert not all(c["ok"] for c in runner.checks(golden, swapped, tmp_path, tmp_path))


def test_a_crashed_tracebacks_tail_is_not_compared(tmp_path):
    golden = {"steps": [{"call": "GET /x", "json": {"error": "boom\nTraceback (most recent call last):\n  py"}}],
              "db": {}, "files": {}}
    got = {"steps": [{"call": "GET /x", "json": {"error": "boom\nTraceback (most recent call last):\n  rust"}}],
           "db": {}, "files": {}}
    assert all(c["ok"] for c in runner.checks(golden, got, tmp_path, tmp_path))


def test_ssim_tells_a_shifted_picture_from_the_same_one(tmp_path):
    def draw(path, x):
        img = Image.new("RGB", (200, 200), "white")
        ImageDraw.Draw(img).rectangle([x, 40, x + 80, 160], fill="black")
        img.save(path)
        return path

    a, b, c = draw(tmp_path / "a.png", 40), draw(tmp_path / "b.png", 40), draw(tmp_path / "c.png", 52)
    assert snapshot.ssim(a, b) == 1.0 and snapshot.ssim(a, c) < runner.IMAGE_SSIM


def test_kept_images_are_small(tmp_path):
    big = tmp_path / "big.png"
    Image.new("RGB", (1080, 1920), "white").save(big)
    small = snapshot.shrink(big, tmp_path / "small.png")
    with Image.open(small) as img:
        assert max(img.size) == snapshot.KEEP_PX and img.size[0] < img.size[1]


def test_snapshot_skips_databases_and_keeps_random_files_by_size(tmp_path):
    (tmp_path / "app.db").write_bytes(b"x" * 10)
    (tmp_path / "secret").write_bytes(b"\x01\x02\x03")
    (tmp_path / "blob.bin").write_bytes(b"\x01\x02\x03")
    (tmp_path / "log.jsonl").write_text('{"b": 2}\n{"a": 1}\n', encoding="utf-8")
    described, _ = snapshot.files(tmp_path, tmp_path / "frames", skip={"app.db"}, random={"secret"},
                                  interleaved={"log.jsonl"})
    assert "app.db" not in described
    assert described["secret"] == {"random_bytes": 3} and "sha256" in described["blob.bin"]
    assert described["log.jsonl"] == {"jsonl": [{"a": 1}, {"b": 2}]}


def test_llm_stub_answers_in_order_and_records_the_requests():
    stub = Stub()
    try:
        stub.queue([{"text": "first"}, {"text": "second"}])
        url = f"{stub.url}/v1/chat/completions"
        body = {"model": "conformance", "messages": [{"role": "user", "content": "hi"}]}
        answer = httpx.post(url, json=body).json()
        assert json.loads(answer["choices"][0]["message"]["content"]) == {"text": "first"}
        assert stub.take() == [body] and stub.unused() == 1   # the rest waits for a later (background) call
        assert json.loads(httpx.post(url, json=body).json()["choices"][0]["message"]["content"]) == {"text": "second"}
        assert httpx.post(url, json=body).status_code == 400   # nothing queued: the scenario is missing a reply
    finally:
        stub.close()


def test_outside_calls_reach_the_project_handler_with_its_queues():
    def handler(stub, method, host, rest, query, body, headers):
        if host == "chat.example" and rest == "/updates":
            return 200, {"updates": stub.drain("updates"), "n": stub.next_id()}, {}
        return None

    stub = Stub(handler)
    try:
        stub.feed("updates", [{"tap": "approve"}])
        got = httpx.get(f"{stub.url}/chat.example/updates?since=0").json()
        assert got == {"updates": [{"tap": "approve"}], "n": 1}
        assert httpx.get(f"{stub.url}/chat.example/updates").json() == {"updates": [], "n": 2}
        assert httpx.post(f"{stub.url}/other.example/x", json={"a": 1}).status_code == 404
        calls = stub.take_platform()
        assert calls[0] == {"method": "GET", "path": "/chat.example/updates", "query": {"since": "0"}, "body": None}
        assert calls[2]["body"] == {"a": 1}
    finally:
        stub.close()


def test_a_project_file_is_loaded_and_its_paths_resolved(tmp_path):
    (tmp_path / "proj").mkdir()
    (tmp_path / "proj" / "p.py").write_text(
        "import pathlib\nfrom parityrun import Project\nPROJECT = Project(root=pathlib.Path(__file__).parent,"
        " quick=['a'], labels={'stub': '<llm>'})\n", encoding="utf-8")
    project = load(tmp_path / "proj" / "p.py")
    assert project.root == (tmp_path / "proj").resolve() and project.repo == tmp_path.resolve()
    assert project.labels == {"data": "<data>", "work": "<work>", "stub": "<llm>", "service": "<service>"}
    (tmp_path / "bad.py").write_text("PROJECT = 1\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        load(tmp_path / "bad.py")


def test_recordings_are_saved_in_a_stable_readable_form():
    assert cli.dump({"b": "ä", "a": [1]}) == '{\n "a": [\n  1\n ],\n "b": "ä"\n}\n'


def test_the_example_runs_end_to_end_and_matches_itself(tmp_path):
    """A real service through a whole scenario: response, prompt, row and file are recorded, ids normalised."""
    root = tmp_path / "notes"
    shutil.copytree(EXAMPLE, root)
    project = load(root / "parityrun_project.py")
    impl = runner.Implementation(project, "python")
    first, second = tmp_path / "a", tmp_path / "b"
    one, two = runner.run(impl, "notes", first), runner.run(impl, "notes", second)
    assert all(c["ok"] for c in runner.checks(one, two, first, second, project))
    made = one["steps"][0]
    assert made["status"] == 201 and made["json"] == {"id": "<id:1>", "title": "Errands before Friday",
                                                      "text": "Buy oat milk and call the plumber before Friday"}
    assert made["llm"][0]["messages"][1]["content"] == "Buy oat milk and call the plumber before Friday"
    assert one["steps"][2]["status"] == 400 and one["steps"][3]["status"] == 404
    assert one["db"]["rows"]["notes"] == [{"id": "<id:1>", "title": "Errands before Friday",
                                           "text": "Buy oat milk and call the plumber before Friday"}]
    assert one["files"] == {"exports/<id:1>.md": {"text": "# Errands before Friday\n\n"
                                                          "Buy oat milk and call the plumber before Friday\n"}}
    changed = json.loads(json.dumps(two))
    changed["steps"][0]["llm"][0]["messages"][0]["content"] = "Give the note a title."
    failed = [c["name"] for c in runner.checks(one, changed, first, second, project) if not c["ok"]]
    assert failed == ["step 0 POST /notes prompts"]
