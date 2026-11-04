"""A tiny notes service to try parityrun on: notes in SQLite, each titled by an LLM (an OpenAI-compatible API).

python notes_service.py --port 8080      (NOTES_DATA: the data folder, LLM_BASE_URL: the API, e.g. .../v1)
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sqlite3
import sys
import threading
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override

DATA = pathlib.Path(os.environ.get("NOTES_DATA", "data"))
LLM = os.environ.get("LLM_BASE_URL", "http://127.0.0.1:11434/v1")


def db() -> sqlite3.Connection:
    """Open the notes database, made if it isn't there."""
    conn = sqlite3.connect(DATA / "notes.db")
    _ = conn.execute("CREATE TABLE IF NOT EXISTS notes (id TEXT PRIMARY KEY, title TEXT NOT NULL, text TEXT NOT NULL)")
    return conn


def title_for(text: str) -> str:
    """Ask the LLM for a note's title."""
    body = {
        "model": os.environ.get("LLM_MODEL", "small"),
        "messages": [
            {"role": "system", "content": "Give the note a title of at most five words."},
            {"role": "user", "content": text},
        ],
    }
    req = urllib.request.Request(  # noqa: S310 (the LLM's address from LLM_BASE_URL)
        f"{LLM}/chat/completions", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 (the LLM's address from LLM_BASE_URL)
        answer: dict[str, Any] = json.loads(resp.read())
    title: str = answer["choices"][0]["message"]["content"]
    return title.strip()


class Handler(BaseHTTPRequestHandler):
    """The notes API: GET /notes, POST /notes, GET /health and POST /shutdown."""

    @override
    def log_message(self, format: str, *args: object) -> None:
        pass

    def _send(self, status: int, body: object) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        _ = self.wfile.write(data)

    def do_GET(self) -> None:
        """Answer the health check and the list of notes."""
        if self.path == "/health":
            return self._send(200, {"ok": True})
        if self.path == "/notes":
            with db() as conn:
                rows = conn.execute("SELECT id, title, text FROM notes ORDER BY rowid").fetchall()
            return self._send(200, [{"id": i, "title": t, "text": x} for i, t, x in rows])
        return self._send(404, {"error": "no such page"})

    def do_POST(self) -> None:
        """Make a note (titled by the LLM), or shut down."""
        if self.path == "/shutdown":
            self._send(200, {"ok": True})
            threading.Thread(target=self.server.shutdown).start()
            return None
        if self.path != "/notes":
            return self._send(404, {"error": "no such page"})
        note: dict[str, Any] = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        text = str(note.get("text") or "").strip()
        if not text:
            return self._send(400, {"error": "a note needs text"})
        made = {"id": uuid.uuid4().hex[:8], "title": title_for(text), "text": text}
        with db() as conn:
            _ = conn.execute("INSERT INTO notes VALUES (:id, :title, :text)", made)
        (DATA / "exports").mkdir(exist_ok=True)
        _ = (DATA / "exports" / f"{made['id']}.md").write_text(f"# {made['title']}\n\n{text}\n", encoding="utf-8")
        return self._send(201, made)


def main() -> None:
    """Serve the notes on --port."""
    parser = argparse.ArgumentParser()
    _ = parser.add_argument("--port", type=int, default=8080)
    port = parser.parse_args().port
    DATA.mkdir(parents=True, exist_ok=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
