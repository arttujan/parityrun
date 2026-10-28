"""A tiny notes service to try parityrun on: notes in SQLite, each titled by an LLM (an OpenAI-compatible API).

  python notes_service.py --port 8080      (NOTES_DATA: the data folder, LLM_BASE_URL: the API, e.g. .../v1)
"""

import argparse
import json
import os
import pathlib
import sqlite3
import threading
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DATA = pathlib.Path(os.environ.get("NOTES_DATA", "data"))
LLM = os.environ.get("LLM_BASE_URL", "http://127.0.0.1:11434/v1")


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DATA / "notes.db")
    conn.execute("CREATE TABLE IF NOT EXISTS notes (id TEXT PRIMARY KEY, title TEXT NOT NULL, text TEXT NOT NULL)")
    return conn


def title_for(text: str) -> str:
    body = {"model": os.environ.get("LLM_MODEL", "small"),
            "messages": [{"role": "system", "content": "Give the note a title of at most five words."},
                         {"role": "user", "content": text}]}
    req = urllib.request.Request(f"{LLM}/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())["choices"][0]["message"]["content"].strip()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def _send(self, status: int, body) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        if self.path == "/health":
            return self._send(200, {"ok": True})
        if self.path == "/notes":
            with db() as conn:
                rows = conn.execute("SELECT id, title, text FROM notes ORDER BY rowid").fetchall()
            return self._send(200, [{"id": i, "title": t, "text": x} for i, t, x in rows])
        return self._send(404, {"error": "no such page"})

    def do_POST(self):  # noqa: N802
        if self.path == "/shutdown":
            self._send(200, {"ok": True})
            threading.Thread(target=self.server.shutdown).start()
            return
        if self.path != "/notes":
            return self._send(404, {"error": "no such page"})
        note = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        text = (note.get("text") or "").strip()
        if not text:
            return self._send(400, {"error": "a note needs text"})
        made = {"id": uuid.uuid4().hex[:8], "title": title_for(text), "text": text}
        with db() as conn:
            conn.execute("INSERT INTO notes VALUES (:id, :title, :text)", made)
        (DATA / "exports").mkdir(exist_ok=True)
        (DATA / "exports" / f"{made['id']}.md").write_text(f"# {made['title']}\n\n{text}\n", encoding="utf-8")
        return self._send(201, made)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8080)
    port = parser.parse_args().port
    DATA.mkdir(parents=True, exist_ok=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
