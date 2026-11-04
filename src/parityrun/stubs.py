"""Stand-ins for the services an implementation calls, so every implementation sees the same answers.

LLM: an OpenAI-compatible /v1/chat/completions, the Anthropic Messages API (/v1/messages) and the `claude` command
line (/v1/claude, through parityrun.fake_claude). A scenario step queues the replies its call needs; the stub answers
them in order and records every request (model, messages, response format), so the prompts themselves are compared.

Outside services: with the service's API base pointing at the stub, https://<host>/<path> calls arrive as
/<host>/<path>. Every one is recorded (method, host and path, query, body; never headers, which carry signatures and
nonces) and answered by the project's stub_handler. Uploaded chunks are recorded as such, multipart bodies as their
parts, pictures as their size.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, TypeAlias, cast

from PIL import Image

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override

if TYPE_CHECKING:
    from collections.abc import Callable
    from email.message import Message

    from .jsontypes import JSON, Data

Answer: TypeAlias = "tuple[int, JSON | bytes, dict[str, str]]"
"""What a stub handler answers: the status, a JSON body (or the bytes of a picture or page) and headers."""

Handler: TypeAlias = "Callable[[Stub, str, str, str, dict[str, str], JSON, Message], Answer | None]"
"""A project's stub_handler: handler(stub, method, host, rest, query, body, headers) -> (status, body, headers), or
None when it has no answer."""

NO_REPLY = "conformance stub: no LLM reply queued for this call"


def _header(head: bytes, name: bytes) -> str:
    """Return the value of a part's header (`name` lower case, with its colon), or ""."""
    return next(
        (line.split(b":", 1)[1].strip().decode() for line in head.splitlines() if line.lower().startswith(name)), ""
    )


def _parts(content_type: str, raw: bytes) -> list[JSON]:
    """Return a multipart body as its parts: JSON parsed, text as text, files by name and type.

    The boundary is random, so it isn't recorded.
    """
    boundary = content_type.split("boundary=", 1)[1].strip('"') if "boundary=" in content_type else ""
    out: list[JSON] = []
    for chunk in raw.split(b"--" + boundary.encode())[1:]:
        if chunk.startswith(b"--"):
            break
        part = chunk.lstrip(b"\r\n")
        # the headers end at the first blank line, CRLF or LF (Google's client writes LF headers)
        ends = [(i, len(sep)) for sep in (b"\r\n\r\n", b"\n\n") if (i := part.find(sep)) >= 0]
        if not ends:
            continue
        cut, width = min(ends)
        head, data = part[:cut], part[cut + width :].rstrip(b"\r\n")
        kind = _header(head, b"content-type:")
        disposition = _header(head, b"content-disposition:")
        if (
            disposition
        ):  # form-data: a field, or a file recorded by its name and kind (its bytes are compared elsewhere)
            pairs = (field.strip().partition("=") for field in disposition.split(";")[1:] if "=" in field)
            fields = {name: value for name, _, value in pairs}
            name = fields.get("name", "").strip('"')
            if "filename" in fields:
                # a random part of a file name (init_1a2b3c4d.png) is noise
                filename = re.sub(r"[0-9a-f]{8,}", "<hex>", fields["filename"].strip('"'))
                out.append({"name": name, "filename": filename, "type": kind})
            else:
                out.append({"name": name, "value": data.decode("utf-8", "replace")})
            continue
        try:
            out.append(
                {"type": kind, "json": json.loads(data)}
                if kind == "application/json"
                else {"type": kind, "text": data.decode("utf-8")}
            )
        except (ValueError, UnicodeDecodeError):
            out.append({"type": kind, "bytes": len(data)})
    return out


def _anthropic(model: JSON, content: str) -> JSON:
    """Return an Anthropic Messages API answer."""
    return {
        "id": "msg_conformance",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": content}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": 1000,
            "output_tokens": 200,
            "cache_read_input_tokens": 300,
            "cache_creation_input_tokens": 0,
        },
    }


def _claude_cli(reply: JSON) -> JSON:
    """Return the `claude -p --output-format json` envelope around a reply."""
    return {
        "type": "result",
        "is_error": False,
        "result": json.dumps(reply),
        "structured_output": reply,
        "total_cost_usd": 0.0125,
        "duration_ms": 4200,
        "usage": {
            "input_tokens": 900,
            "cache_read_input_tokens": 8000,
            "cache_creation_input_tokens": 0,
            "output_tokens": 200,
        },
        "modelUsage": {
            "claude-haiku-5-5": {"outputTokens": 12},
            "claude-sonnet-5-5": {"outputTokens": 200},
        },
    }


def _chat_completion(model: JSON, content: str) -> JSON:
    """Return an OpenAI-compatible chat completion."""
    return {
        "id": "chatcmpl-conformance",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1000, "completion_tokens": 200, "total_tokens": 1200},
    }


def _platform_body(content_type: str, raw: bytes, *, upload_chunk: bool) -> JSON:
    """Return an outside-service call's body as recorded."""
    kind = content_type.split(";", maxsplit=1)[0]
    if upload_chunk:
        return {"upload_chunk": True}
    if kind in ("multipart/related", "multipart/form-data"):
        return {"parts": _parts(content_type, raw)}
    if kind == "application/json":
        body: JSON = json.loads(raw or b"null")
        return body
    if kind == "application/x-www-form-urlencoded":
        return dict(urllib.parse.parse_qsl(raw.decode()))
    if raw[:8] == b"\x89PNG\r\n\x1a\n" or raw[:3] == b"\xff\xd8\xff":
        # an uploaded picture: its pixels are compared by SSIM elsewhere; encoders differ byte for byte
        img = Image.open(io.BytesIO(raw))
        with img:
            return {"image": list(img.size)}
    return {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()} if raw else None


class _Server(ThreadingHTTPServer):
    def __init__(self, stub: Stub) -> None:
        super().__init__(("127.0.0.1", 0), _RequestHandler)
        self.stub = stub


class _RequestHandler(BaseHTTPRequestHandler):
    @property
    def stub(self) -> Stub:
        return cast("_Server", self.server).stub

    @override
    def log_message(self, format: str, *args: object) -> None:
        pass

    def _send(self, status: int, body: JSON, headers: dict[str, str] | None = None) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        _ = self.wfile.write(data)

    def _body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def _platform_call(self, method: str) -> None:
        url = urllib.parse.urlsplit(self.path)
        query = dict(urllib.parse.parse_qsl(url.query))
        content_type = self.headers.get("Content-Type", "")
        body = _platform_body(content_type, self._body(), upload_chunk=bool(self.headers.get("Content-Range")))
        status, answer, headers = self.stub.platform_call(method, url.path, query, body, self.headers)
        if isinstance(answer, bytes):  # a picture or a web page
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8" if answer[:1] == b"<" else "image/png")
            self.send_header("Content-Length", str(len(answer)))
            self.end_headers()
            _ = self.wfile.write(answer)
            return
        self._send(status, answer, headers)

    def do_GET(self) -> None:
        """Answer the model list, or an outside-service call."""
        if self.path.startswith("/v1/"):
            if self.path.rstrip("/").endswith("/models"):
                return self._send(200, {"object": "list", "data": [{"id": "conformance", "object": "model"}]})
            return self._send(404, {"error": {"message": f"conformance stub has no {self.path}"}})
        return self._platform_call("GET")

    def do_PUT(self) -> None:
        """Answer an outside-service call."""
        return self._platform_call("PUT")

    def do_POST(self) -> None:
        """Answer an LLM request with the next queued reply, or an outside-service call."""
        if not self.path.startswith("/v1/"):
            return self._platform_call("POST")
        body: Data = json.loads(self._body() or b"{}")
        path = self.path.rstrip("/")
        if path.endswith("/messages"):  # the Anthropic API (its base URL = {stub})
            return self._anthropic(body)
        if path.endswith("/claude"):  # parityrun.fake_claude: one `claude -p` run
            return self._claude_cli(body)
        if path.endswith("/chat/completions"):
            return self._chat_completions(body)
        return self._send(404, {"error": {"message": f"conformance stub has no {self.path}"}})

    def _anthropic(self, body: Data) -> None:
        reply = self.stub.llm_call({"anthropic": body})
        if reply is None:
            error: JSON = {"type": "invalid_request_error", "message": NO_REPLY}
            return self._send(400, {"type": "error", "error": error})
        content = reply if isinstance(reply, str) else json.dumps(reply)
        return self._send(200, _anthropic(body.get("model", ""), content))

    def _claude_cli(self, body: Data) -> None:
        reply = self.stub.llm_call({"claude_cli": body})
        if reply is None:
            return self._send(200, {"type": "result", "is_error": True, "result": NO_REPLY})
        return self._send(200, _claude_cli(reply))

    def _chat_completions(self, body: Data) -> None:
        reply = self.stub.llm_call({k: v for k, v in body.items() if k != "stream"})
        if reply is None:
            return self._send(400, {"error": {"message": NO_REPLY}})
        if isinstance(reply, dict) and "$http" in reply:  # a failing service, e.g. {$http: 429, message: ...}
            error: JSON = {"error": {"message": reply.get("message", "")}}
            return self._send(int(str(reply["$http"])), error, {"retry-after-ms": "10"})
        content = reply if isinstance(reply, str) else json.dumps(reply)
        return self._send(200, _chat_completion(body.get("model", ""), content))


class Stub:
    """The LLM and outside-service stand-in for one run, on a free local port."""

    def __init__(self, handler: Handler | None = None) -> None:
        """Start serving; `handler` answers the outside-service calls (the project's stub_handler)."""
        super().__init__()
        self._replies: list[JSON] = []
        self._requests: list[Data] = []
        self._platform: list[Data] = []
        self._next = 0
        self._handler = handler
        self.queues: dict[str, list[JSON]] = {}  # step-fed lists the project's handler hands out (stub_queues)
        self.state: Data = {}  # the project's handler keeps its own state here
        self.lock = threading.Lock()
        self.server = _Server(self)
        self.port: int = self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        """The stub's address."""
        return f"http://127.0.0.1:{self.port}"

    def next_id(self) -> int:
        """Return 1, 2, 3, ... over the run: ids an outside service hands out, the same in every run."""
        with self.lock:
            self._next += 1
            return self._next

    def llm_call(self, request: Data) -> JSON:
        """Record an LLM request and return the next queued reply (None when none is left)."""
        with self.lock:
            self._requests.append(request)
            return self._replies.pop(0) if self._replies else None

    def platform_call(self, method: str, path: str, query: dict[str, str], body: JSON, headers: Message) -> Answer:
        """Record an outside-service call and return the project handler's answer (404 when it has none)."""
        with self.lock:
            self._platform.append({"method": method, "path": path, "query": query, "body": body})
        host, _, rest = path.lstrip("/").partition("/")
        answer = self._handler(self, method, host, "/" + rest, query, body, headers) if self._handler else None
        if answer is not None:
            return answer
        return 404, {"error": {"message": f"conformance stub has no {method} {path}"}}, {}

    def queue(self, replies: list[JSON]) -> None:
        """Add LLM replies after the ones still waiting (a background task may ask for them steps later)."""
        with self.lock:
            self._replies.extend(replies)

    def feed(self, name: str, items: list[JSON]) -> None:
        """Add items to a project queue (stub_queues) for the handler's next answers."""
        with self.lock:
            self.queues.setdefault(name, []).extend(items)

    def drain(self, name: str) -> list[JSON]:
        """Return everything waiting in a project queue, and empty it."""
        with self.lock:
            items, self.queues[name] = self.queues.get(name, []), []
        return items

    def take(self) -> list[Data]:
        """Return the LLM requests since the last take."""
        with self.lock:
            requests, self._requests = self._requests, []
        return requests

    def take_platform(self) -> list[Data]:
        """Return the outside-service requests since the last take."""
        with self.lock:
            requests, self._platform = self._platform, []
        return requests

    def unused(self) -> int:
        """Count the LLM replies no request has taken."""
        with self.lock:
            return len(self._replies)

    def close(self) -> None:
        """Stop serving."""
        self.server.shutdown()
        self.server.server_close()
