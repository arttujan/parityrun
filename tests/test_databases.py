"""Databases other than a SQLite file: the Project.snapshot and Project.setup hooks and the DB-API reader.

The MySQL and PostgreSQL tests need a server you can throw a table at; they skip unless one is named:
  PARITYRUN_TEST_MYSQL=mysql://user:password@127.0.0.1:3306/scratch      (pip install pymysql)
  PARITYRUN_TEST_POSTGRES=postgresql://user:password@127.0.0.1:5432/scratch   (pip install "psycopg[binary]")
"""

import contextlib
import datetime as dt
import decimal
import hashlib
import os
import pathlib
import shutil
import sqlite3
import urllib.parse
import uuid

import pytest

from parityrun import load, runner, snapshot

EXAMPLE = pathlib.Path(__file__).resolve().parents[1] / "examples" / "notes"
MYSQL = os.environ.get("PARITYRUN_TEST_MYSQL")
POSTGRES = os.environ.get("PARITYRUN_TEST_POSTGRES")
RAW_HASH = hashlib.sha256(b"\x00\x01").hexdigest()


def test_values_become_plain_json():
    assert snapshot._plain(decimal.Decimal("12.50")) == "12.50"
    assert snapshot._plain(dt.datetime(2026, 1, 2, 3, 4, 5)) == "2026-01-02T03:04:05"
    assert snapshot._plain(dt.date(2026, 1, 2)) == "2026-01-02"
    assert snapshot._plain(b"\x00\x01") == {"sha256": RAW_HASH}
    assert snapshot._plain(memoryview(b"\x00\x01")) == {"sha256": RAW_HASH}
    assert snapshot._plain(uuid.UUID(int=1)) == "00000000-0000-0000-0000-000000000001"
    assert snapshot._plain('{"k": [1]}') == {"k": [1]} and snapshot._plain("plain") == "plain"
    assert snapshot._plain([decimal.Decimal("1.0"), None]) == ["1.0", None]
    assert (snapshot._plain(True), snapshot._plain(3), snapshot._plain(None)) == (True, 3, None)


def test_a_sqlite_connection_reads_like_the_sqlite_file(tmp_path):
    path = tmp_path / "app.db"
    with contextlib.closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, meta TEXT)")
        conn.execute("CREATE TABLE migrations (n INTEGER)")
        conn.execute("CREATE INDEX t_meta ON t (meta)")
        conn.execute("""INSERT INTO t VALUES (1, '{"a": 1}')""")
        conn.commit()
        assert snapshot.dbapi(conn, "sqlite") == snapshot.database(path)
        assert conn.row_factory is None  # the caller's connection is left as it was
        skipped = snapshot.dbapi(conn, "sqlite", skip={"migrations"})
    assert set(skipped["rows"]) == {"t"} and skipped["rows"]["t"] == [{"id": 1, "meta": {"a": 1}}]
    assert "migrations" not in skipped["schema"] and "index t_meta" in skipped["schema"]
    with pytest.raises(ValueError, match="dialect 'oracle'"):
        snapshot.dbapi(None, "oracle")


def test_a_project_snapshot_hook_is_recorded_as_the_database_and_setup_runs_before_each_start(tmp_path):
    root = tmp_path / "notes"
    shutil.copytree(EXAMPLE, root, ignore=shutil.ignore_patterns("golden", "out"))
    project = load(root / "parityrun_project.py")
    impl = runner.Implementation(project, "python")
    builtin = runner.run(impl, "notes", tmp_path / "a")
    started = []
    project.setup = lambda run: started.append(run.data_dir.exists())

    def read(run):
        with contextlib.closing(sqlite3.connect(run.data_dir / "notes.db")) as conn:
            return snapshot.dbapi(conn, "sqlite")

    project.snapshot = read
    hooked = runner.run(impl, "notes", tmp_path / "b")
    assert started == [True]
    assert hooked == builtin and hooked["db"]["rows"]["notes"][0]["id"] == "<id:1>"


def _table(conn, create: str, insert: str) -> None:
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS parityrun_t")
    cur.execute(create)
    cur.execute(insert)
    conn.commit()


@pytest.mark.skipif(not MYSQL, reason="set PARITYRUN_TEST_MYSQL=mysql://user:password@host:port/database")
def test_mysql_tables_and_rows_are_read():
    pymysql = pytest.importorskip("pymysql")
    url = urllib.parse.urlsplit(MYSQL)
    conn = pymysql.connect(host=url.hostname, port=url.port or 3306, user=urllib.parse.unquote(url.username or ""),
                           password=urllib.parse.unquote(url.password or ""), database=url.path.lstrip("/"))
    try:
        _table(conn, "CREATE TABLE parityrun_t (id INT PRIMARY KEY, name VARCHAR(20) NOT NULL, price DECIMAL(6,2), "
                     "made DATETIME, meta JSON, raw BLOB)",
               """INSERT INTO parityrun_t VALUES (1, 'a', 12.50, '2026-01-02 03:04:05', '{"k": 1}', x'0001')""")
        got = snapshot.dbapi(conn, "mysql")
        columns = got["schema"]["parityrun_t"]
        assert [(c[0], c[2], c[4]) for c in columns] == [
            ("id", "NO", "PRI"), ("name", "NO", ""), ("price", "YES", ""), ("made", "YES", ""), ("meta", "YES", ""),
            ("raw", "YES", "")]
        assert columns[1][1] == "varchar(20)" and columns[2][1] == "decimal(6,2)"
        assert got["rows"]["parityrun_t"] == [{"id": 1, "name": "a", "price": "12.50", "made": "2026-01-02T03:04:05",
                                               "meta": {"k": 1}, "raw": {"sha256": RAW_HASH}}]
    finally:
        conn.cursor().execute("DROP TABLE IF EXISTS parityrun_t")
        conn.commit()
        conn.close()


@pytest.mark.skipif(not POSTGRES, reason="set PARITYRUN_TEST_POSTGRES=postgresql://user:password@host:port/database")
def test_postgres_tables_and_rows_are_read():
    psycopg = pytest.importorskip("psycopg")
    conn = psycopg.connect(POSTGRES)
    try:
        _table(conn, "CREATE TABLE parityrun_t (id INT PRIMARY KEY, name VARCHAR(20) NOT NULL, price NUMERIC(6,2), "
                     "made TIMESTAMP, meta JSONB, tags TEXT[], raw BYTEA)",
               r"""INSERT INTO parityrun_t VALUES (1, 'a', 12.50, '2026-01-02 03:04:05', '{"k": 1}', """
               r"""ARRAY['x', 'y'], '\x0001')""")
        got = snapshot.dbapi(conn, "postgres")
        assert got["schema"]["parityrun_t"] == [
            ["id", "integer", "NO", None, ""], ["name", "character varying", "NO", None, ""],
            ["price", "numeric", "YES", None, ""], ["made", "timestamp without time zone", "YES", None, ""],
            ["meta", "jsonb", "YES", None, ""], ["tags", "ARRAY", "YES", None, ""], ["raw", "bytea", "YES", None, ""]]
        assert got["rows"]["parityrun_t"] == [{"id": 1, "name": "a", "price": "12.50", "made": "2026-01-02T03:04:05",
                                               "meta": {"k": 1}, "tags": ["x", "y"], "raw": {"sha256": RAW_HASH}}]
    finally:
        conn.rollback()
        conn.cursor().execute("DROP TABLE IF EXISTS parityrun_t")
        conn.commit()
        conn.close()
