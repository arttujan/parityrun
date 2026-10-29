"""`parityrun init` (telling the stack, reading the home page, writing parityrun/) and isolated runs."""

import json
import pathlib
import textwrap

import httpx
import pytest

from parityrun import Project, RunInfo, cli, load, runner
from parityrun import init as setup


def _files(root: pathlib.Path, files: dict) -> pathlib.Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def test_a_plain_php_site_runs_from_its_own_folder(tmp_path):
    stack = setup.detect(_files(tmp_path, {"index.php": "<?php echo 1;", "about.php": ""}))
    assert (stack.name, stack.label, stack.tool) == ("php", "PHP", "php")
    assert stack.cmd == ["php", "-S", "127.0.0.1:{port}", "-t", "."] and stack.databases == []


def test_a_php_app_with_a_public_folder_serves_that(tmp_path):
    stack = setup.detect(_files(tmp_path, {"composer.json": "{}", "public/index.php": "", "src/App.php": ""}))
    assert stack.cmd[-2:] == ["-t", "public"]
    assert any("composer install" in note for note in stack.notes)  # vendor/ is missing


def test_laravel_with_sqlite_records_its_database_and_skips_its_logs(tmp_path):
    stack = setup.detect(_files(tmp_path, {
        "artisan": "", "composer.json": "{}", "vendor/autoload.php": "", "public/index.php": "",
        ".env": "APP_NAME=Shop\nDB_CONNECTION=sqlite\nDB_DATABASE=database/database.sqlite\n"}))
    assert stack.label == "PHP (Laravel)" and stack.databases == ["database/database.sqlite"]
    assert "storage/logs/*" in stack.ignore_files and stack.notes == []


def test_a_mysql_app_gets_a_note_instead_of_a_database(tmp_path):
    stack = setup.detect(_files(tmp_path, {"artisan": "", "public/index.php": "", "vendor/x": "",
                                           ".env": 'DB_CONNECTION="mysql"\nDB_HOST=127.0.0.1\n'}))
    assert stack.databases == [] and any("MySQL" in note for note in stack.notes)


def test_sqlite_files_in_the_app_are_found_but_not_in_vendor(tmp_path):
    stack = setup.detect(_files(tmp_path, {"index.php": "", "vendor/pkg/test.sqlite": "", "data/app.sqlite": "",
                                           "data/cache.db": ""}))
    assert stack.databases == ["data/app.sqlite"] and any("data/cache.db" in note for note in stack.notes)


def test_symfony_and_wordpress_are_told_apart(tmp_path):
    symfony = setup.detect(_files(tmp_path / "s", {"bin/console": "", "symfony.lock": "{}", "public/index.php": ""}))
    wordpress = setup.detect(_files(tmp_path / "w", {"index.php": "", "wp-config.php": ""}))
    assert symfony.label == "PHP (Symfony)" and "var/log/*" in symfony.ignore_files
    assert wordpress.label == "PHP (WordPress)" and any("MySQL" in note for note in wordpress.notes)


@pytest.mark.parametrize(("package", "cmd"), [
    ({"scripts": {"start": "node server.js"}}, ["node", "server.js"]),
    ({"main": "src/app.js"}, ["node", "src/app.js"]),
    ({"scripts": {"start": "next start"}}, ["npm", "start"]),
    ({}, ["node", "index.js"]),
])
def test_node_apps_start_the_way_their_package_says(tmp_path, package, cmd):
    stack = setup.detect(_files(tmp_path, {"package.json": json.dumps(package)}))
    assert (stack.name, stack.cmd, stack.env) == ("node", cmd, {"PORT": "{port}"})


def test_python_and_rust_apps(tmp_path):
    django = setup.detect(_files(tmp_path / "d", {"manage.py": ""}))
    flask = setup.detect(_files(tmp_path / "f", {"app.py": "from flask import Flask\napp = Flask(__name__)\n"}))
    rust = setup.detect(_files(tmp_path / "r", {"Cargo.toml": "[package]\n"}))
    assert django.cmd == ["{python}", "manage.py", "runserver", "127.0.0.1:{port}", "--noreload"]
    assert flask.cmd == ["{python}", "-m", "flask", "--app", "app", "run", "--port", "{port}"]
    assert (rust.cmd, rust.env, rust.ready_timeout) == (["cargo", "run", "--release", "--quiet"], {"PORT": "{port}"},
                                                        900)


def test_an_unknown_folder_says_what_it_looked_for(tmp_path):
    with pytest.raises(SystemExit, match="Couldn't tell how to start"):
        setup.detect(_files(tmp_path, {"README.md": "hi"}))


def test_the_home_pages_own_links_and_tokens_are_found():
    html = """<a href="/about">a</a><a href="about2">b</a><a href="https://other.example/x">c</a>
      <a href="/logout">d</a><a href="/style.css">e</a><a href="#top">f</a><a href="/about">again</a>
      <a href="mailto:x@example.com">g</a><a href="/?page=2">h</a><a href="/posts/7/delete">i</a>
      <form><input type="hidden" name="_token" value="abc"><input type="hidden" name="page" value="1">
      <input name="csrf" value="visible"></form>"""
    links, tokens = setup.page_parts(html, "http://127.0.0.1:5000/")
    assert links == ["/about", "/about2", "/?page=2"] and tokens == ["_token"]


def test_a_token_is_captured_whatever_the_attribute_order():
    rule = {"t": setup.token_regex("_token")}
    for page in ('<input type="hidden" name="_token" value="a1">', "<input value='a1' name='_token' type=hidden>"):
        r = httpx.Response(200, text=page, headers={"Content-Type": "text/html"})
        assert runner._capture(r, rule, "text/html", "s") == {"t": "a1"}


def test_init_writes_a_project_that_loads(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(setup.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    app = _files(tmp_path / "site", {"index.php": "<?php echo 1;", "data/app.sqlite": ""})
    assert setup.init(app, check_start=False) == 0
    assert "Found: PHP app" in capsys.readouterr().out
    folder = app / "parityrun"
    assert json.loads((folder / "impls.json").read_text(encoding="utf-8")) == {
        "php": {"cmd": ["php", "-S", "127.0.0.1:{port}", "-t", "."]}}
    project = load(folder / "parityrun_project.py")
    assert (project.reference, project.repo, project.ready, project.quick) == ("php", app.resolve(), "/", ["smoke"])
    assert project.isolate and project.html and project.redirects and project.databases == ["data/app.sqlite"]
    assert project.coverage  # a PHP app's lines are measured
    run = RunInfo(data_dir=tmp_path, work=tmp_path, port=4321, stub_url="http://127.0.0.1:1", repo=app)
    assert project.env(run) == {"PORT": "4321"}  # any implementation added later gets its port too
    assert runner.load(project, "smoke")["steps"] == [{"call": "GET /", "follow": True}]
    assert (folder / ".gitignore").read_text(encoding="utf-8") == "out/\n"
    assert setup.init(app, check_start=False) == 1  # never over what is there without --force
    assert setup.init(app, force=True, check_start=False) == 0


def test_init_says_when_the_tool_is_missing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(setup.shutil, "which", lambda tool: None)
    assert setup.init(_files(tmp_path / "site", {"index.php": ""})) == 1
    assert "php isn't installed" in capsys.readouterr().out


def test_the_project_init_wrote_is_found_from_the_apps_folder(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(setup.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    app = _files(tmp_path / "site", {"index.php": ""})
    setup.init(app, check_start=False)
    assert json.loads((app / "parityrun" / "golden" / "endpoints.json").read_text(encoding="utf-8")) == ["GET /"]
    (app / "parityrun" / "golden" / "endpoints.json").write_text('["GET /", "GET /about"]', encoding="utf-8")
    monkeypatch.chdir(app)
    assert cli.main(["coverage"]) == 0
    assert "Endpoint coverage: 1/2 (50 %)" in capsys.readouterr().out


SERVICE = textwrap.dedent("""\
    import pathlib, sqlite3, sys
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    HERE = pathlib.Path.cwd()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            if self.path == "/visit":  # writes into its own folder, like a legacy app
                (HERE / "counter.txt").write_text("1")
                (HERE / "logs").mkdir(exist_ok=True)
                (HERE / "logs" / "x.log").write_text("noise")
                (HERE / "old.txt").unlink()
                (HERE / "same.txt").write_text("same")
                conn = sqlite3.connect(HERE / "app.db")
                conn.execute("CREATE TABLE IF NOT EXISTS visits (n INTEGER)")
                conn.execute("INSERT INTO visits VALUES (1)")
                conn.commit()
                conn.close()
            body = b"<p>ok</p>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
    """)


def test_an_isolated_run_records_what_the_app_changed_in_its_own_folder_and_leaves_it_alone(tmp_path):
    app = _files(tmp_path / "app", {"server.py": SERVICE, "old.txt": "bye", "same.txt": "same", "keep.txt": "k",
                                    ".git/HEAD": "ref"})
    proj = _files(tmp_path / "app" / "parityrun", {
        "impls.json": json.dumps({"py": {"cmd": ["{python}", "server.py", "{port}"]}}),
        "scenarios/visit.yaml": "steps:\n  - call: GET /visit\n"})
    project = Project(root=proj, reference="py", isolate=True, databases=["app.db"], ignore_files=["logs/*"])
    recording = runner.run(runner.Implementation(project, "py"), "visit", tmp_path / "out")
    assert recording["files"] == {"app/counter.txt": {"text": "1"}, "app/old.txt": {"deleted": True}}
    assert recording["db"]["rows"] == {"visits": [{"n": 1}]}
    assert sorted(p.name for p in app.iterdir()) == [".git", "keep.txt", "old.txt", "parityrun", "same.txt",
                                                      "server.py"]  # the app's own folder is as it was


def test_the_copy_leaves_out_version_control_and_the_parityrun_folder(tmp_path):
    app = _files(tmp_path / "app", {"a.txt": "a", ".git/HEAD": "x", "parityrun/impls.json": json.dumps(
        {"py": {"cmd": ["x"]}})})
    impl = runner.Implementation(Project(root=app / "parityrun", reference="py"), "py")
    baseline = runner.copy_app(impl, tmp_path / "copy")
    assert set(baseline) == {"a.txt"} and not (tmp_path / "copy" / ".git").exists()
    (tmp_path / "copy" / "a.txt").write_text("a", encoding="utf-8")  # rewritten with the same bytes
    (tmp_path / "copy" / "b.txt").write_text("b", encoding="utf-8")
    assert runner.app_changes(impl, tmp_path / "copy", baseline) == ({"b.txt"}, set())


def test_isolation_settings_are_off_by_default(tmp_path):
    project = Project(root=tmp_path)
    assert (project.isolate, project.isolate_skip, project.ignore_files) == (False, [".git", ".hg", ".svn"], [])
