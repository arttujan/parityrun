"""The stub's every path: multipart bodies taken apart, how each kind of outside-service body is recorded, pages and
pictures a project handler answers, the model list and the paths it has no answer for.
"""

from __future__ import annotations

import io
import json
from typing import TYPE_CHECKING

import httpx
from PIL import Image

from parityrun import Stub
from parityrun.stubs import _parts, _platform_body

if TYPE_CHECKING:
    from email.message import Message

    from parityrun.jsontypes import JSON
    from parityrun.stubs import Answer


def _png(size: tuple[int, int] = (3, 2), kind: str = "PNG") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, "red").save(buf, kind)
    return buf.getvalue()


def test_a_multipart_body_becomes_its_parts() -> None:
    body = (
        b"--XyZ\r\n"
        b'Content-Disposition: form-data; name="title"\r\n\r\nCat\r\n'
        b"--XyZ\r\n"
        b'Content-Disposition: form-data; name="photo"; filename="init_1a2b3c4d5e.png"\r\n'
        b"Content-Type: image/png\r\n\r\n\x89PNG...\r\n"
        b"--XyZ\r\n"
        b'Content-Type: application/json\n\n{"a": 1}\n'  # LF headers, as Google's client writes them
        b"--XyZ\r\n"
        b"Content-Type: text/plain\r\n\r\nhello\r\n"
        b"--XyZ\r\n"
        b"Content-Type: application/json\r\n\r\n{not json\r\n"
        b"--XyZ\r\n"
        b"Content-Type: application/octet-stream\r\n\r\n\xff\xfe\r\n"
        b"--XyZ\r\n"
        b"no blank line, so no part\r\n"
        b"--XyZ--\r\n"
        b"after the closing boundary\r\n"
    )
    assert _parts('multipart/form-data; boundary="XyZ"', body) == [
        {"name": "title", "value": "Cat"},
        {"name": "photo", "filename": "init_<hex>.png", "type": "image/png"},  # the random part is noise
        {"type": "application/json", "json": {"a": 1}},
        {"type": "text/plain", "text": "hello"},
        {"type": "application/json", "bytes": 9},
        {"type": "application/octet-stream", "bytes": 2},
    ]


def test_each_kind_of_outside_service_body_is_recorded_as_what_it_means() -> None:
    assert _platform_body("application/octet-stream", b"chunk", upload_chunk=True) == {"upload_chunk": True}
    related = b'--b\r\nContent-Type: application/json\r\n\r\n{"x": 2}\r\n--b--'
    assert _platform_body("multipart/related; boundary=b", related, upload_chunk=False) == {
        "parts": [{"type": "application/json", "json": {"x": 2}}]
    }
    assert _platform_body("application/json", b"", upload_chunk=False) is None
    assert _platform_body("application/x-www-form-urlencoded", b"a=1&b=two", upload_chunk=False) == {
        "a": "1",
        "b": "two",
    }
    assert _platform_body("", _png((4, 5)), upload_chunk=False) == {"image": [4, 5]}
    assert _platform_body("", _png((6, 2), "JPEG"), upload_chunk=False) == {"image": [6, 2]}
    raw = _platform_body("", b"abc", upload_chunk=False)
    assert raw == {"bytes": 3, "sha256": "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"}
    assert _platform_body("", b"", upload_chunk=False) is None


def test_a_handler_answers_pages_and_pictures_and_puts_and_uploads_are_recorded() -> None:
    picture = _png()

    def handler(  # noqa: PLR0913, PLR0917 (a stub handler's signature)
        stub: Stub,  # noqa: ARG001
        method: str,
        host: str,  # noqa: ARG001
        rest: str,
        query: dict[str, str],  # noqa: ARG001
        body: JSON,  # noqa: ARG001
        headers: Message,  # noqa: ARG001
    ) -> Answer | None:
        if rest == "/page":
            return 200, b"<p>hi</p>", {}
        if rest == "/picture":
            return 200, picture, {}
        if method == "PUT":
            return 201, {"ok": True}, {"X-Upload": "done"}
        return None

    stub = Stub(handler)
    try:
        page = httpx.get(f"{stub.url}/cdn.example/page")
        assert (page.headers["content-type"], page.text) == ("text/html; charset=utf-8", "<p>hi</p>")
        image = httpx.get(f"{stub.url}/cdn.example/picture")
        assert (image.headers["content-type"], image.content) == ("image/png", picture)
        put = httpx.put(f"{stub.url}/files.example/up", content=b"part", headers={"Content-Range": "bytes 0-3/8"})
        assert (put.status_code, put.json(), put.headers["x-upload"]) == (201, {"ok": True}, "done")
        assert stub.take_platform()[2] == {
            "method": "PUT",
            "path": "/files.example/up",
            "query": {},
            "body": {"upload_chunk": True},
        }
    finally:
        stub.close()


def test_without_a_handler_every_outside_call_is_a_404() -> None:
    stub = Stub()
    try:
        answer = httpx.get(f"{stub.url}/api.example/x")
        assert answer.status_code == 404
        assert answer.json() == {"error": {"message": "conformance stub has no GET /api.example/x"}}
    finally:
        stub.close()


def test_the_llm_paths_the_stub_knows_and_the_ones_it_doesnt() -> None:
    stub = Stub()
    try:
        models = httpx.get(f"{stub.url}/v1/models/").json()
        assert models == {"object": "list", "data": [{"id": "conformance", "object": "model"}]}
        assert httpx.get(f"{stub.url}/v1/files").status_code == 404
        unknown = httpx.post(f"{stub.url}/v1/embeddings", json={})
        assert unknown.status_code == 404
        assert unknown.json() == {"error": {"message": "conformance stub has no /v1/embeddings"}}
        assert httpx.post(f"{stub.url}/v1/messages", content=b"").status_code == 400  # no body, no reply queued
        missing = httpx.post(f"{stub.url}/v1/claude", json={"args": ["-p"]}).json()
        assert missing == {
            "type": "result",
            "is_error": True,
            "result": "conformance stub has no LLM reply queued for this call",
        }
        stub.queue([{"$http": 429, "message": "slow down"}, "plain text"])
        busy = httpx.post(f"{stub.url}/v1/chat/completions", json={"model": "m", "stream": True})
        assert (busy.status_code, busy.json(), busy.headers["retry-after-ms"]) == (
            429,
            {"error": {"message": "slow down"}},
            "10",
        )
        text = httpx.post(f"{stub.url}/v1/chat/completions", json={"model": "m"}).json()
        assert text["choices"][0]["message"]["content"] == "plain text"  # a string reply is sent as it is
        assert stub.take() == [
            {"anthropic": {}},
            {"claude_cli": {"args": ["-p"]}},
            {"model": "m"},  # stream isn't part of the prompt
            {"model": "m"},
        ]
        assert json.loads(json.dumps(stub.drain("nothing"))) == []
    finally:
        stub.close()
