"""What a scenario leaves behind: SQLite databases (schema + rows) and the files in the data folder.

Pictures are kept small and compared by structural similarity, videos by their duration, size, sound and a few frames.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import fnmatch
import hashlib
import io
import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import wave
from typing import TYPE_CHECKING, Protocol, cast

import numpy as np
from PIL import Image
from scipy import ndimage

if TYPE_CHECKING:
    from collections.abc import Collection, Iterable, Sequence

    from numpy.typing import NDArray

    from .jsontypes import JSON, Data

TEXT = {".txt", ".md", ".srt", ".ass", ".env", ".yaml", ".yml", ".html", ".csv", ".xml"}
IMAGES = {".png", ".jpg", ".jpeg", ".webp"}
VIDEOS = {".mp4", ".mov", ".webm"}
FRAMES = (0.1, 0.3, 0.5, 0.7, 0.9)
KEEP_PX = 480  # images are kept and compared at this size: small golden files, and a 4 px shift still shows


class Cursor(Protocol):
    """The part of a DB-API cursor `dbapi` uses."""

    @property
    def description(self) -> Sequence[Sequence[object]] | None:
        """The columns of the last query's rows, each with its name first."""
        ...

    def execute(self, operation: str, /) -> object:
        """Run a query."""
        ...

    def fetchall(self) -> Sequence[Sequence[object]]:
        """Return the rows of the last query."""
        ...

    def close(self) -> object:
        """Close the cursor."""
        ...


class Connection(Protocol):
    """The part of a DB-API connection `dbapi` uses."""

    def cursor(self) -> Cursor:
        """Return a new cursor."""
        ...

    def rollback(self) -> object:
        """End the transaction."""
        ...


def _maybe_json(value: object) -> object:
    if isinstance(value, str) and value[:1] in "{[":
        try:
            parsed: JSON = json.loads(value)
        except ValueError:
            return value
        return parsed
    return value


def database(path: pathlib.Path) -> Data:
    """Return a SQLite database's schema (columns, types, defaults, keys, index SQL) and every table's rows."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return _sqlite(conn)
    finally:
        conn.close()


def _plain(value: object) -> object:
    """Return a database value as JSON: decimals and times as text, bytes by their hash."""
    if isinstance(value, str):
        return _maybe_json(value)
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, (bytes, bytearray)):
        return {"sha256": hashlib.sha256(value).hexdigest()}
    if isinstance(value, (dt.date, dt.time)):  # datetime, date, time
        return value.isoformat()
    if isinstance(value, (list, tuple)):  # PostgreSQL arrays
        return [_plain(v) for v in cast("Sequence[object]", value)]
    if value is None or isinstance(value, (bool, int, float, dict)):  # a dict: a PostgreSQL json column
        return cast("object", value)
    return str(value)  # Decimal, UUID, ...


QUERIES = {
    "mysql": (
        (
            "SELECT table_name FROM information_schema.tables WHERE table_schema = DATABASE() "
            "AND table_type = 'BASE TABLE' ORDER BY table_name"
        ),
        (
            "SELECT column_name, column_type, is_nullable, column_default, column_key "
            "FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = '{table}' "
            "ORDER BY ordinal_position"
        ),
        "`{table}`",
    ),
    "postgres": (
        (
            "SELECT table_name FROM information_schema.tables WHERE table_schema = '{schema}' "
            "AND table_type = 'BASE TABLE' ORDER BY table_name"
        ),
        (
            "SELECT column_name, data_type, is_nullable, column_default, '' "
            "FROM information_schema.columns WHERE table_schema = '{schema}' AND table_name = '{table}' "
            "ORDER BY ordinal_position"
        ),
        '"{schema}"."{table}"',
    ),
}


def dbapi(conn: Connection, dialect: str = "sqlite", schema: str = "public", skip: Collection[str] = ()) -> Data:
    """Return the schema and rows of an open DB-API connection's database, for Project.snapshot.

    `dialect`: "sqlite" (sqlite3), "mysql" (MySQL or MariaDB: the connection's database; pymysql, mysqlclient, ...) or
    "postgres" (`schema`; psycopg, ...). `skip`: tables left out (e.g. a migrations table). Columns are
    [name, type, nullable, default, key].
    """
    if dialect == "sqlite":
        if not isinstance(conn, sqlite3.Connection):
            msg = f"dialect 'sqlite' reads a sqlite3 connection, not {type(conn).__name__}"
            raise TypeError(msg)
        out = _sqlite(conn)
    elif dialect in QUERIES:
        tables_sql, columns_sql, quoted = QUERIES[dialect]
        cur = conn.cursor()
        _ = cur.execute(tables_sql.format(schema=schema))
        out: Data = {"schema": {}, "rows": {}}
        for (table,) in cur.fetchall():
            _ = cur.execute(columns_sql.format(schema=schema, table=table))
            out["schema"][table] = [[_plain(v) for v in col] for col in cur.fetchall()]
            _ = cur.execute(f"SELECT * FROM {quoted.format(schema=schema, table=table)}")  # noqa: S608 (names from the database's own catalog)
            names = [d[0] for d in cur.description or ()]
            out["rows"][table] = [{k: _plain(v) for k, v in zip(names, row, strict=False)} for row in cur.fetchall()]
        _ = cur.close()
        _ = conn.rollback()  # end the read transaction, so the next run's setup isn't blocked by it
    else:
        msg = f"dialect {dialect!r}: use sqlite, mysql or postgres"
        raise ValueError(msg)
    for table in skip:
        out["schema"].pop(table, None)
        out["rows"].pop(table, None)
    return out


def _sqlite(conn: sqlite3.Connection) -> Data:
    factory, conn.row_factory = conn.row_factory, sqlite3.Row
    try:
        out: Data = {"schema": {}, "rows": {}}
        objects: list[sqlite3.Row] = conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
        for item in objects:
            if item["type"] == "table":
                cols: list[sqlite3.Row] = conn.execute(f'PRAGMA table_info("{item["name"]}")').fetchall()
                out["schema"][item["name"]] = [
                    [c["name"], c["type"], c["notnull"], c["dflt_value"], c["pk"]] for c in cols
                ]
                rows: list[sqlite3.Row] = conn.execute(f'SELECT * FROM "{item["name"]}"').fetchall()  # noqa: S608 (a name from sqlite_master)
                out["rows"][item["name"]] = [{k: _maybe_json(r[k]) for k in r.keys()} for r in rows]  # noqa: SIM118 (a Row iterates its values)
            elif item["type"] == "index" and item["sql"]:
                out["schema"][f"index {item['name']}"] = " ".join(item["sql"].split())
        return out
    finally:
        conn.row_factory = factory


def _tool(name: str) -> str:
    found = os.environ.get(name.upper()) or shutil.which(name)
    if not found:
        msg = f"{name} isn't on PATH (or set {name.upper()}); the harness needs it to compare videos"
        raise RuntimeError(msg)
    return found


def video(path: pathlib.Path, frames_prefix: pathlib.Path) -> tuple[Data, list[pathlib.Path]]:
    """Return a video's duration (0.1 s), size and whether it has sound, and frames at 10/30/50/70/90 %."""
    probe = [
        _tool("ffprobe"),
        "-v",
        "error",
        "-show_entries",
        "format=duration:stream=codec_type,width,height",
        "-of",
        "json",
        str(path),
    ]
    info: Data = json.loads(subprocess.run(probe, capture_output=True, text=True, check=True).stdout)  # noqa: S603 (ffprobe on a recorded file)
    streams: list[Data] = info.get("streams", [])
    pictures = [s for s in streams if s.get("codec_type") == "video"]
    picture: Data = pictures[0] if pictures else {}
    duration = float(info.get("format", {}).get("duration") or 0)
    desc: Data = {
        "duration": round(duration, 1),
        "size": [picture.get("width", 0), picture.get("height", 0)],
        "audio": any(s.get("codec_type") == "audio" for s in streams),
    }
    frames: list[pathlib.Path] = []
    for k, at in enumerate(FRAMES):
        out = pathlib.Path(f"{frames_prefix}#frame{k}.png")
        grab = [_tool("ffmpeg"), "-v", "error", "-y", "-ss", f"{duration * at:.3f}", "-i", str(path)]
        _ = subprocess.run([*grab, "-frames:v", "1", str(out)], check=True, capture_output=True)  # noqa: S603 (ffmpeg on a recorded file)
        frames.append(shrink(out, out))
    return desc, frames


def ignored(rel: str, patterns: Iterable[str]) -> bool:
    """Tell whether one of the patterns (fnmatch: "storage/logs/*", "*.log") leaves out a relative path."""
    return any(fnmatch.fnmatch(rel, pattern) for pattern in patterns)


@dataclasses.dataclass(frozen=True)
class FileRules:
    """How `files` treats the files it finds."""

    skip: Collection[str] = ()
    """File names left out (the databases)."""
    random: Collection[str] = ()
    """File names compared by size only."""
    interleaved: Collection[str] = ()
    """JSON Lines files whose lines may come in any order."""
    ignore: Collection[str] = ()
    """Path patterns left out."""


def files(
    data_dir: pathlib.Path,
    frames_dir: pathlib.Path | None = None,
    rules: FileRules | None = None,
    only: Collection[str] | None = None,
) -> tuple[dict[str, Data], dict[str, pathlib.Path]]:
    """Return {relative path: description} and the images to compare by eye-distance (SSIM).

    A video's frames count as images named <video>#frame<k>. `rules` tells which files are left out or compared
    loosely; `only`: just these relative paths (the files a run changed).
    """
    frames_dir = frames_dir or data_dir.parent / "frames"
    rules = rules or FileRules()
    described: dict[str, Data] = {}
    images: dict[str, pathlib.Path] = {}
    paths = [data_dir / rel for rel in only] if only is not None else [p for p in data_dir.rglob("*") if p.is_file()]
    for path in sorted(paths):
        rel = path.relative_to(data_dir).as_posix()
        if path.name in rules.skip or ignored(rel, rules.ignore):
            continue
        ext = path.suffix.lower()
        if ext in VIDEOS:  # duration, size and a few frames (compared by SSIM like images)
            frames_dir.mkdir(parents=True, exist_ok=True)
            desc, frames = video(path, frames_dir / rel.replace("/", "_"))
            described[rel] = {"video": desc}
            for k, frame in enumerate(frames):
                described[f"{rel}#frame{k}"] = {"image": list(desc["size"])}
                images[f"{rel}#frame{k}"] = frame
            continue
        described[rel] = _describe(path, rules)
        if ext in IMAGES:
            images[rel] = path
    return described, images


def _describe(path: pathlib.Path, rules: FileRules) -> Data:
    """Return how a file is recorded: JSON, JSON Lines and text as they are, other files as `_describe_binary`."""
    ext = path.suffix.lower()
    if ext == ".json":
        try:
            return {"json": json.loads(path.read_text(encoding="utf-8"))}
        except ValueError:
            pass
    if ext == ".jsonl":
        lines: list[JSON] = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if path.name in rules.interleaved:  # written by parallel work, in either order
            lines.sort(key=lambda x: json.dumps(x, sort_keys=True))
        return {"jsonl": lines}
    if ext in TEXT:
        return {"text": path.read_text(encoding="utf-8", errors="replace")}
    return _describe_binary(path, rules)


def _describe_binary(path: pathlib.Path, rules: FileRules) -> Data:
    """Return how a file is recorded: a picture by its size, a sound by its length, other bytes by their hash."""
    ext = path.suffix.lower()
    if ext in IMAGES:
        img = Image.open(path)
        with img:
            return {"image": list(img.size)}
    if ext == ".wav":
        with wave.open(str(path)) as w:
            return {"audio_sec": round(w.getnframes() / w.getframerate(), 2)}
    if path.name in rules.random:
        return {"random_bytes": path.stat().st_size}
    return {"sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def shrink(src: pathlib.Path | bytes, dst: pathlib.Path) -> pathlib.Path:
    """Save an image (a file or its bytes) as a PNG no larger than KEEP_PX on its longest side."""
    img = Image.open(io.BytesIO(src) if isinstance(src, bytes) else src)
    with img:
        rgb = img.convert("RGB")
    rgb.thumbnail((KEEP_PX, KEEP_PX), Image.Resampling.LANCZOS)
    dst.parent.mkdir(parents=True, exist_ok=True)
    rgb.save(dst, "PNG", optimize=True)
    return dst


class _Filters(Protocol):
    """The part of scipy.ndimage `ssim` uses, typed here.

    Newer scipy-stubs leave one overload of gaussian_filter partly unknown and older ones don't, so neither a
    pyright ignore nor its absence passes on every Python: the module is seen through this instead.
    """

    def gaussian_filter(self, image: NDArray[np.float64], sigma: float, /) -> NDArray[np.float64]:
        """Return the image blurred with a Gaussian of standard deviation `sigma`."""
        ...


def _blur(m: NDArray[np.float64]) -> NDArray[np.float64]:
    return cast("_Filters", ndimage).gaussian_filter(m, 1.5)


def ssim(a: pathlib.Path, b: pathlib.Path) -> float:
    """Return the structural similarity of two images (grey, Gaussian window); 1.0 = the same picture."""
    ia, ib = Image.open(a), Image.open(b)
    with ia, ib:
        if ia.size != ib.size:
            return 0.0
        x = np.asarray(ia.convert("L"), dtype=np.float64)
        y = np.asarray(ib.convert("L"), dtype=np.float64)
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    mx, my = _blur(x), _blur(y)
    vx, vy, cov = _blur(x * x) - mx * mx, _blur(y * y) - my * my, _blur(x * y) - mx * my
    s = ((2 * mx * my + c1) * (2 * cov + c2)) / ((mx * mx + my * my + c1) * (vx + vy + c2))
    return float(s.mean())
