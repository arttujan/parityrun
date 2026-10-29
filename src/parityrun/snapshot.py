"""What a scenario leaves behind: SQLite databases (schema + rows) and the files in the data folder; pictures are
kept small and compared by structural similarity, videos by their duration, size, sound and a few frames."""

import hashlib
import io
import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import wave

import numpy as np
from PIL import Image
from scipy import ndimage

TEXT = {".txt", ".md", ".srt", ".ass", ".env", ".yaml", ".yml", ".html", ".csv", ".xml"}
IMAGES = {".png", ".jpg", ".jpeg", ".webp"}
VIDEOS = {".mp4", ".mov", ".webm"}
FRAMES = (0.1, 0.3, 0.5, 0.7, 0.9)
KEEP_PX = 480  # images are kept and compared at this size: small golden files, and a 4 px shift still shows


def _maybe_json(value):
    if isinstance(value, str) and value[:1] in "{[":
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def database(path: pathlib.Path) -> dict:
    """A SQLite database's schema (columns, types, defaults, keys, index SQL) and every table's rows."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return _sqlite(conn)
    finally:
        conn.close()


def _plain(value):
    """A database value as JSON: decimals and times as text, bytes by their hash."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _maybe_json(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"sha256": hashlib.sha256(bytes(value)).hexdigest()}
    if hasattr(value, "isoformat"):  # datetime, date, time
        return value.isoformat()
    if isinstance(value, (list, tuple)):  # PostgreSQL arrays
        return [_plain(v) for v in value]
    if isinstance(value, dict):  # PostgreSQL json columns
        return value
    return str(value)  # Decimal, UUID, ...


QUERIES = {
    "mysql": ("SELECT table_name FROM information_schema.tables WHERE table_schema = DATABASE() "
              "AND table_type = 'BASE TABLE' ORDER BY table_name",
              "SELECT column_name, column_type, is_nullable, column_default, column_key "
              "FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = '{table}' "
              "ORDER BY ordinal_position", "`{table}`"),
    "postgres": ("SELECT table_name FROM information_schema.tables WHERE table_schema = '{schema}' "
                 "AND table_type = 'BASE TABLE' ORDER BY table_name",
                 "SELECT column_name, data_type, is_nullable, column_default, '' "
                 "FROM information_schema.columns WHERE table_schema = '{schema}' AND table_name = '{table}' "
                 "ORDER BY ordinal_position", '"{schema}"."{table}"'),
}


def dbapi(conn, dialect: str = "sqlite", schema: str = "public", skip: set[str] = frozenset()) -> dict:
    """Schema and rows from an open DB-API connection, for Project.snapshot: "sqlite" (sqlite3), "mysql" (MySQL or
    MariaDB: the connection's database; pymysql, mysqlclient, ...) or "postgres" (`schema`; psycopg, ...).
    `skip`: tables left out (e.g. a migrations table). Columns are [name, type, nullable, default, key]."""
    if dialect == "sqlite":
        out = _sqlite(conn)
    elif dialect in QUERIES:
        tables_sql, columns_sql, quoted = QUERIES[dialect]
        cur = conn.cursor()
        cur.execute(tables_sql.format(schema=schema))
        out = {"schema": {}, "rows": {}}
        for (table,) in cur.fetchall():
            cur.execute(columns_sql.format(schema=schema, table=table))
            out["schema"][table] = [[_plain(v) for v in col] for col in cur.fetchall()]
            cur.execute(f"SELECT * FROM {quoted.format(schema=schema, table=table)}")
            names = [d[0] for d in cur.description]
            out["rows"][table] = [{k: _plain(v) for k, v in zip(names, row)} for row in cur.fetchall()]
        cur.close()
        conn.rollback()  # end the read transaction, so the next run's setup isn't blocked by it
    else:
        raise ValueError(f"dialect {dialect!r}: use sqlite, mysql or postgres")
    for table in skip:
        out["schema"].pop(table, None)
        out["rows"].pop(table, None)
    return out


def _sqlite(conn: sqlite3.Connection) -> dict:
    factory, conn.row_factory = conn.row_factory, sqlite3.Row
    try:
        out = {"schema": {}, "rows": {}}
        objects = conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master "
                               "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name").fetchall()
        for obj in objects:
            if obj["type"] == "table":
                cols = conn.execute(f'PRAGMA table_info("{obj["name"]}")').fetchall()
                out["schema"][obj["name"]] = [[c["name"], c["type"], c["notnull"], c["dflt_value"], c["pk"]]
                                              for c in cols]
                out["rows"][obj["name"]] = [{k: _maybe_json(r[k]) for k in r.keys()}
                                            for r in conn.execute(f'SELECT * FROM "{obj["name"]}"')]
            elif obj["type"] == "index" and obj["sql"]:
                out["schema"][f"index {obj['name']}"] = " ".join(obj["sql"].split())
        return out
    finally:
        conn.row_factory = factory


def _tool(name: str) -> str:
    found = os.environ.get(name.upper()) or shutil.which(name)
    if not found:
        raise RuntimeError(f"{name} isn't on PATH (or set {name.upper()}); the harness needs it to compare videos")
    return found


def video(path: pathlib.Path, frames_prefix: pathlib.Path) -> tuple[dict, list[pathlib.Path]]:
    """Duration (0.1 s), size, whether it has sound, and frames at 10/30/50/70/90 % of the duration."""
    info = json.loads(subprocess.run(
        [_tool("ffprobe"), "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height",
         "-of", "json", str(path)], capture_output=True, text=True, check=True).stdout)
    streams = info.get("streams", [])
    picture = next((s for s in streams if s.get("codec_type") == "video"), {})
    duration = float(info.get("format", {}).get("duration") or 0)
    desc = {"duration": round(duration, 1), "size": [picture.get("width", 0), picture.get("height", 0)],
            "audio": any(s.get("codec_type") == "audio" for s in streams)}
    frames = []
    for k, at in enumerate(FRAMES):
        out = pathlib.Path(f"{frames_prefix}#frame{k}.png")
        subprocess.run([_tool("ffmpeg"), "-v", "error", "-y", "-ss", f"{duration * at:.3f}", "-i", str(path),
                        "-frames:v", "1", str(out)], check=True, capture_output=True)
        frames.append(shrink(out, out))
    return desc, frames


def files(data_dir: pathlib.Path, frames_dir: pathlib.Path | None = None, skip: set[str] = frozenset(),
          random: set[str] = frozenset(), interleaved: set[str] = frozenset()) -> tuple[dict, dict[str, pathlib.Path]]:
    """{relative path: description} and the images to compare by eye-distance (SSIM); a video's frames count as
    images named <video>#frame<k>. `skip`: file names left out (the databases); `random`: compared by size only;
    `interleaved`: JSON Lines files whose lines may come in any order."""
    frames_dir = frames_dir or data_dir.parent / "frames"
    described, images = {}, {}
    for path in sorted(p for p in data_dir.rglob("*") if p.is_file()):
        rel = path.relative_to(data_dir).as_posix()
        if path.name in skip:
            continue
        ext = path.suffix.lower()
        if ext == ".json":
            try:
                described[rel] = {"json": json.loads(path.read_text(encoding="utf-8"))}
                continue
            except ValueError:
                pass
        if ext == ".jsonl":
            lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
            if path.name in interleaved:  # written by parallel work, in either order
                lines.sort(key=lambda x: json.dumps(x, sort_keys=True))
            described[rel] = {"jsonl": lines}
        elif ext in TEXT:
            described[rel] = {"text": path.read_text(encoding="utf-8", errors="replace")}
        elif ext in IMAGES:
            with Image.open(path) as img:
                described[rel] = {"image": list(img.size)}
            images[rel] = path
        elif ext == ".wav":
            with wave.open(str(path)) as w:
                described[rel] = {"audio_sec": round(w.getnframes() / w.getframerate(), 2)}
        elif ext in VIDEOS:  # duration, size and a few frames (compared by SSIM like images)
            frames_dir.mkdir(parents=True, exist_ok=True)
            desc, frames = video(path, frames_dir / rel.replace("/", "_"))
            described[rel] = {"video": desc}
            for k, frame in enumerate(frames):
                described[f"{rel}#frame{k}"] = {"image": list(desc["size"])}
                images[f"{rel}#frame{k}"] = frame
        elif path.name in random:
            described[rel] = {"random_bytes": path.stat().st_size}
        else:
            described[rel] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    return described, images


def shrink(src: pathlib.Path | bytes, dst: pathlib.Path) -> pathlib.Path:
    """Save an image (a file or its bytes) as a PNG no larger than KEEP_PX on its longest side."""
    with Image.open(io.BytesIO(src) if isinstance(src, bytes) else src) as img:
        img = img.convert("RGB")
        img.thumbnail((KEEP_PX, KEEP_PX), Image.LANCZOS)
        dst.parent.mkdir(parents=True, exist_ok=True)
        img.save(dst, "PNG", optimize=True)
    return dst


def ssim(a: pathlib.Path, b: pathlib.Path) -> float:
    """Structural similarity of two images (grey, Gaussian window); 1.0 = the same picture."""
    with Image.open(a) as ia, Image.open(b) as ib:
        if ia.size != ib.size:
            return 0.0
        x = np.asarray(ia.convert("L"), dtype=np.float64)
        y = np.asarray(ib.convert("L"), dtype=np.float64)
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    blur = lambda m: ndimage.gaussian_filter(m, 1.5)  # noqa: E731
    mx, my = blur(x), blur(y)
    vx, vy, cov = blur(x * x) - mx * mx, blur(y * y) - my * my, blur(x * y) - mx * my
    s = ((2 * mx * my + c1) * (2 * cov + c2)) / ((mx * mx + my * my + c1) * (vx + vy + c2))
    return float(s.mean())
