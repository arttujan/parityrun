"""Stand-ins for the services an implementation calls, so every implementation sees the same answers.

LLM: an OpenAI-compatible /v1/chat/completions, the Anthropic Messages API (/v1/messages) and the `claude` command
line (/v1/claude, through parityrun.fake_claude). A scenario step queues the replies its call needs; the stub answers
them in order and records every request (model, messages, response format), so the prompts themselves are compared.

Outside services: with the service's API base pointing at the stub, https://<host>/<path> calls arrive as
/<host>/<path>. Every one is recorded (method, host and path, query, body; never headers, which carry signatures and
nonces) and answered by the project's stub_handler. Uploaded chunks are recorded as such, multipart bodies as their
parts, pictures as their size.
"""

import hashlib
import io
import json
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _parts(content_type: str, raw: bytes) -> list:
    """A multipart body as its parts: JSON parsed, text as text, files by name and type (the boundary is random)."""
    boundary = content_type.split("boundary=", 1)[1].strip('"') if "boundary=" in content_type else ""
    out = []
    for chunk in raw.split(b"--" + boundary.encode())[1:]:
        if chunk.startswith(b"--"):
            break
        chunk = chunk.lstrip(b"\r\n")
        # the headers end at the first blank line, CRLF or LF (Google's client writes LF headers)
        ends = [(i, len(sep)) for sep in (b"\r\n\r\n", b"\n\n") if (i := chunk.find(sep)) >= 0]
        if not ends:
            continue
        cut, width = min(ends)
        head, data = chunk[:cut], chunk[cut + width:]
        kind = next((line.split(b":", 1)[1].strip().decode() for line in head.splitlines()
                     if line.lower().startswith(b"content-type:")), "")
        data = data.rstrip(b"\r\n")
        disposition = next((line.split(b":", 1)[1].strip().decode() for line in head.splitlines()
                            if line.lower().startswith(b"content-disposition:")), "")
        if disposition:  # form-data: a field, or a file recorded by its name and kind (its bytes are compared elsewhere)
            fields = dict(part.strip().split("=", 1) for part in disposition.split(";")[1:] if "=" in part)
            name = fields.get("name", "").strip('"')
            if "filename" in fields:
                # a random part of a file name (init_1a2b3c4d.png) is noise
                filename = re.sub(r"[0-9a-f]{8,}", "<hex>", fields["filename"].strip('"'))
                out.append({"name": name, "filename": filename, "type": kind})
            else:
                out.append({"name": name, "value": data.decode("utf-8", "replace")})
            continue
        try:
            out.append({"type": kind, "json": json.loads(data)} if kind == "application/json"
                       else {"type": kind, "text": data.decode("utf-8")})
        except (ValueError, UnicodeDecodeError):
            out.append({"type": kind, "bytes": len(data)})
    return out


class Stub:
    """The LLM and outside-service stand-in for one run, on a free local port."""

    def __init__(self, handler=None) -> None:
        self._replies: list = []
        self._requests: list[dict] = []
        self._platform: list[dict] = []
        self._next = 0
        self._handler = handler
        self.queues: dict[str, list] = {}  # step-fed lists the project's handler hands out (stub_queues)
        self.state: dict = {}  # the project's handler keeps its own state here
        self.lock = threading.Lock()
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def _send(self, status: int, body: dict, headers: dict | None = None) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def _body(self) -> bytes:
                return self.rfile.read(int(self.headers.get("Content-Length") or 0))

            def _platform_call(self, method: str) -> None:
                raw = self._body()
                url = urllib.parse.urlsplit(self.path)
                query = dict(urllib.parse.parse_qsl(url.query))
                kind = self.headers.get("Content-Type", "").split(";")[0]
                if self.headers.get("Content-Range"):
                    body = {"upload_chunk": True}
                elif kind in ("multipart/related", "multipart/form-data"):
                    body = {"parts": _parts(self.headers.get("Content-Type", ""), raw)}
                elif kind == "application/json":
                    body = json.loads(raw or b"null")
                elif kind == "application/x-www-form-urlencoded":
                    body = dict(urllib.parse.parse_qsl(raw.decode()))
                elif raw[:8] == b"\x89PNG\r\n\x1a\n" or raw[:3] == b"\xff\xd8\xff":
                    # an uploaded picture: its pixels are compared by SSIM elsewhere; encoders differ byte for byte
                    from PIL import Image
                    with Image.open(io.BytesIO(raw)) as img:
                        body = {"image": list(img.size)}
                else:
                    body = {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()} if raw else None
                with stub.lock:
                    stub._platform.append({"method": method, "path": url.path, "query": query, "body": body})
                status, answer, headers = stub._answer(method, url.path, query, body, self.headers)
                if isinstance(answer, bytes):  # a picture or a web page
                    self.send_response(status)
                    self.send_header("Content-Type", "text/html; charset=utf-8" if answer[:1] == b"<" else "image/png")
                    self.send_header("Content-Length", str(len(answer)))
                    self.end_headers()
                    self.wfile.write(answer)
                    return
                self._send(status, answer, headers)

            def do_GET(self):  # noqa: N802
                if self.path.startswith("/v1/"):
                    if self.path.rstrip("/").endswith("/models"):
                        return self._send(200, {"object": "list", "data": [{"id": "conformance", "object": "model"}]})
                    return self._send(404, {"error": {"message": f"conformance stub has no {self.path}"}})
                return self._platform_call("GET")

            def do_PUT(self):  # noqa: N802
                return self._platform_call("PUT")

            def do_POST(self):  # noqa: N802
                if not self.path.startswith("/v1/"):
                    return self._platform_call("POST")
                body = json.loads(self._body() or b"{}")
                if self.path.rstrip("/").endswith("/messages"):  # the Anthropic API (its base URL = {stub})
                    with stub.lock:
                        stub._requests.append({"anthropic": body})
                        reply = stub._replies.pop(0) if stub._replies else None
                    if reply is None:
                        return self._send(400, {"type": "error", "error": {
                            "type": "invalid_request_error", "message": "conformance stub: no LLM reply queued for this call"}})
                    content = reply if isinstance(reply, str) else json.dumps(reply)
                    return self._send(200, {
                        "id": "msg_conformance", "type": "message", "role": "assistant", "model": body.get("model", ""),
                        "content": [{"type": "text", "text": content}], "stop_reason": "end_turn", "stop_sequence": None,
                        "usage": {"input_tokens": 1000, "output_tokens": 200, "cache_read_input_tokens": 300,
                                  "cache_creation_input_tokens": 0}})
                if self.path.rstrip("/").endswith("/claude"):  # parityrun.fake_claude: one `claude -p` run
                    with stub.lock:
                        stub._requests.append({"claude_cli": body})
                        reply = stub._replies.pop(0) if stub._replies else None
                    if reply is None:
                        return self._send(200, {"type": "result", "is_error": True,
                                                "result": "conformance stub: no LLM reply queued for this call"})
                    return self._send(200, {
                        "type": "result", "is_error": False, "result": json.dumps(reply), "structured_output": reply,
                        "total_cost_usd": 0.0125, "duration_ms": 4200,
                        "usage": {"input_tokens": 900, "cache_read_input_tokens": 8000, "cache_creation_input_tokens": 0,
                                  "output_tokens": 200},
                        "modelUsage": {"claude-haiku-5-5": {"outputTokens": 12},
                                       "claude-sonnet-5-5": {"outputTokens": 200}}})
                if not self.path.rstrip("/").endswith("/chat/completions"):
                    return self._send(404, {"error": {"message": f"conformance stub has no {self.path}"}})
                with stub.lock:
                    stub._requests.append({k: v for k, v in body.items() if k != "stream"})
                    reply = stub._replies.pop(0) if stub._replies else None
                if reply is None:
                    return self._send(400, {"error": {"message": "conformance stub: no LLM reply queued for this call"}})
                if isinstance(reply, dict) and "$http" in reply:  # a failing service, e.g. {$http: 429, message: ...}
                    return self._send(int(reply["$http"]), {"error": {"message": reply.get("message", "")}},
                                      {"retry-after-ms": "10"})
                content = reply if isinstance(reply, str) else json.dumps(reply)
                return self._send(200, {
                    "id": "chatcmpl-conformance", "object": "chat.completion", "created": 0,
                    "model": body.get("model", ""),
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1000, "completion_tokens": 200, "total_tokens": 1200}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def next_id(self) -> int:
        """1, 2, 3, ... over the run: ids an outside service hands out, the same in every run."""
        with self.lock:
            self._next += 1
            return self._next

    def _answer(self, method: str, path: str, query: dict, body, headers=None) -> tuple[int, object, dict]:
        host, _, rest = path.lstrip("/").partition("/")
        answer = self._handler(self, method, host, "/" + rest, query, body, headers or {}) if self._handler else None
        if answer is not None:
            return answer
        return 404, {"error": {"message": f"conformance stub has no {method} {path}"}}, {}

    def queue(self, replies: list) -> None:
        """Add LLM replies after the ones still waiting (a background task may ask for them steps later)."""
        with self.lock:
            self._replies.extend(replies)

    def feed(self, name: str, items: list) -> None:
        """Add items to a project queue (stub_queues) for the handler's next answers."""
        with self.lock:
            self.queues.setdefault(name, []).extend(items)

    def drain(self, name: str) -> list:
        """Everything waiting in a project queue, emptied."""
        with self.lock:
            items, self.queues[name] = self.queues.get(name, []), []
        return items

    def take(self) -> list[dict]:
        """The LLM requests since the last take."""
        with self.lock:
            requests, self._requests = self._requests, []
        return requests

    def take_platform(self) -> list[dict]:
        """The outside-service requests since the last take."""
        with self.lock:
            requests, self._platform = self._platform, []
        return requests

    def unused(self) -> int:
        with self.lock:
            return len(self._replies)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
