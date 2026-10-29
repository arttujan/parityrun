"""What v0.1 records, frozen: a change here breaks every golden recording already made with parityrun.

If one of these fails, a feature changed default behaviour. New behaviour must be opt-in (a Project setting or a
scenario key); only a deliberate breaking release may change these expectations.
"""

import json
import pathlib
import shutil

import httpx

from parityrun import Project, Stub, cli, load, runner
from parityrun.normalize import Normalizer

EXAMPLE = pathlib.Path(__file__).resolve().parents[1] / "examples" / "notes"
TEXT = "Buy oat milk and call the plumber before Friday"
NOTE = {"id": "<id:1>", "text": TEXT, "title": "Errands before Friday"}

# The notes example's whole recording, exactly as `record` saves it.
NOTES_RECORDING = {
    "db": {
        "rows": {"notes": [NOTE]},
        "schema": {"notes": [["id", "TEXT", 0, None, 1], ["title", "TEXT", 1, None, 0], ["text", "TEXT", 1, None, 0]]},
    },
    "files": {"exports/<id:1>.md": {"text": f"# Errands before Friday\n\n{TEXT}\n"}},
    "steps": [
        {"call": "POST /notes", "json": NOTE, "status": 201,
         "llm": [{"model": "conformance", "messages": [
             {"role": "system", "content": "Give the note a title of at most five words."},
             {"role": "user", "content": TEXT}]}]},
        {"call": "GET /notes", "json": [NOTE], "status": 200},
        {"call": "POST /notes", "json": {"error": "a note needs text"}, "status": 400},
        {"call": "GET /missing", "json": {"error": "no such page"}, "status": 404},
    ],
}


def test_the_notes_recording_is_saved_byte_for_byte_as_before(tmp_path, capsys):
    root = tmp_path / "notes"
    shutil.copytree(EXAMPLE, root, ignore=shutil.ignore_patterns("golden", "out"))
    project_file = str(root / "parityrun_project.py")
    assert cli.main(["--project", project_file, "record"]) == 0
    saved = (root / "golden" / "notes" / "recording.json").read_bytes()
    assert saved == cli.dump(NOTES_RECORDING).encode("utf-8")
    assert cli.main(["--project", project_file, "check"]) == 0
    assert "python: parity 100.0 % (12/12 checks)" in capsys.readouterr().out
    report = json.loads((root / "out" / "report-python.json").read_text(encoding="utf-8"))
    assert [c["name"] for c in report["notes"]] == [
        "step 0 POST /notes status", "step 0 POST /notes json", "step 0 POST /notes prompts",
        "step 1 GET /notes status", "step 1 GET /notes json",
        "step 2 POST /notes status", "step 2 POST /notes json",
        "step 3 GET /missing status", "step 3 GET /missing json",
        "schema notes", "rows notes", "file exports/<id:1>.md"]


def test_the_dump_format_is_stable():
    assert cli.dump({"b": {"y": None, "x": 1.5}, "a": ["ä", True]}) == \
        '{\n "a": [\n  "ä",\n  true\n ],\n "b": {\n  "x": 1.5,\n  "y": null\n }\n}\n'


def test_check_names_for_every_part_of_a_step_are_stable(tmp_path):
    step = {"call": "GET /x", "status": 200, "json": {"a": 1}, "text": "t", "disposition": "attachment",
            "events": [{"e": 1}], "video": {"duration": 1.0}, "range": {"bytes": 1}, "cli": {"exit": 0},
            "llm": [{"m": 1}], "platform": [{"p": 1}]}
    golden = {"steps": [step], "db": {"schema": {"t": []}, "rows": {"t": []}}, "files": {"a.txt": {"text": "x"}}}
    names = [c["name"] for c in runner.checks(golden, json.loads(json.dumps(golden)), tmp_path, tmp_path)]
    assert names == ["step 0 GET /x status", "step 0 GET /x json", "step 0 GET /x text", "step 0 GET /x disposition",
                     "step 0 GET /x events", "step 0 GET /x video", "step 0 GET /x range", "step 0 GET /x cli",
                     "step 0 GET /x prompts", "step 0 GET /x platform calls", "schema t", "rows t", "file a.txt"]


def test_the_llm_stubs_answers_are_stable():
    stub = Stub()
    try:
        stub.queue(["plain", {"k": 1}, "claude", {"$http": 429, "message": "slow down"}])
        chat = httpx.post(f"{stub.url}/v1/chat/completions", json={"model": "m", "messages": [], "stream": False})
        assert chat.json() == {
            "id": "chatcmpl-conformance", "object": "chat.completion", "created": 0, "model": "m",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "plain"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 200, "total_tokens": 1200}}
        anthropic = httpx.post(f"{stub.url}/v1/messages", json={"model": "a"})
        assert anthropic.json() == {
            "id": "msg_conformance", "type": "message", "role": "assistant", "model": "a",
            "content": [{"type": "text", "text": '{"k": 1}'}], "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 1000, "output_tokens": 200, "cache_read_input_tokens": 300,
                      "cache_creation_input_tokens": 0}}
        claude = httpx.post(f"{stub.url}/v1/claude", json={"args": []}).json()
        assert claude["result"] == '"claude"' and claude["total_cost_usd"] == 0.0125 and claude["is_error"] is False
        limited = httpx.post(f"{stub.url}/v1/chat/completions", json={"model": "m"})
        assert limited.status_code == 429 and limited.json() == {"error": {"message": "slow down"}}
        assert stub.take() == [{"model": "m", "messages": []}, {"anthropic": {"model": "a"}},
                               {"claude_cli": {"args": []}}, {"model": "m"}]
        empty = httpx.post(f"{stub.url}/v1/chat/completions", json={})
        assert empty.status_code == 400 and empty.json() == {
            "error": {"message": "conformance stub: no LLM reply queued for this call"}}
        assert httpx.post(f"{stub.url}/v1/claude", json={}).json() == {
            "type": "result", "is_error": True, "result": "conformance stub: no LLM reply queued for this call"}
        assert httpx.get(f"{stub.url}/v1/other").json() == {"error": {"message": "conformance stub has no /v1/other"}}
        assert httpx.get(f"{stub.url}/v1/models").json() == {
            "object": "list", "data": [{"id": "conformance", "object": "model"}]}
        assert httpx.get(f"{stub.url}/host.example/x").json() == {
            "error": {"message": "conformance stub has no GET /host.example/x"}}
    finally:
        stub.close()


def test_normalisation_is_stable():
    norm = Normalizer({"C:/w/data": "<data>", "127.0.0.1:1": "<stub>"})
    raw = {"id": "0123abcd", "long": "0123456789abcdef0123456789abcdef", "twelve": "0123456789ab",
           "seen": "/x/0123abcd/y", "at": "2026-01-02 03:04:05.123Z", "epoch": 1760000000.5, "big": 9999999999,
           "path": "C:/w/data/f.txt", "url": "http://127.0.0.1:1/v1", "short": "abc123", "UPPER": "0123ABCD",
           "state": "https://x/auth?state=abcdefghijklmnop1234", "num": 7}
    norm.collect(raw)
    assert norm.apply(raw) == {
        "id": "<id:1>", "long": "<id:2>", "twelve": "<id:3>", "seen": "/x/<id:1>/y", "at": "<time>",
        "epoch": "<epoch>", "big": 9999999999, "path": "<data>/f.txt", "url": "http://<stub>/v1", "short": "abc123",
        "UPPER": "0123ABCD", "state": "https://x/auth?state=<state>", "num": 7}


def test_requests_without_new_keys_are_sent_as_before(tmp_path):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.headers.get("content-type"), request.content))
        return httpx.Response(200, json={})

    client = httpx.Client(base_url="http://svc", transport=httpx.MockTransport(handler))
    (tmp_path / "up.bin").write_bytes(b"\x00\x01")
    runner._request(client, {"call": "POST /a/{{x.id}}", "body": {"n": "{{x.id}}"}}, {"x": {"id": 5}}, tmp_path)
    runner._request(client, {"call": "GET /b"}, {}, tmp_path)
    runner._request(client, {"call": "PUT /c", "raw": "up.bin"}, {}, tmp_path)
    assert seen == [("POST", "/a/5", "application/json", b'{"n":5}'), ("GET", "/b", None, b""),
                    ("PUT", "/c", "application/octet-stream", b"\x00\x01")]


def test_a_project_with_only_a_root_keeps_every_default(tmp_path):
    project = Project(root=tmp_path)
    assert (project.reference, project.ready, project.ready_timeout, project.shutdown, project.databases,
            project.image_ssim, project.work_prefix) == ("main", "/", 90, None, [], 0.98, "parityrun-")


def test_the_example_project_file_still_loads():
    project = load(EXAMPLE / "parityrun_project.py")
    assert project.reference == "python" and project.databases == ["notes.db"]
