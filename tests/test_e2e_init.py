"""End to end, per stack: `parityrun init` in an existing app's folder, then record and check with nothing written by
hand but one form step. The apps write into their own folders, as legacy apps do; their folders must stay untouched.

Each test skips when its tool is missing: php for the PHP site, node for the Node.js app.
"""

import json
import pathlib
import shutil

import pytest
import yaml

from parityrun import cli

LEGACY_PHP = {
    "index.php": """<?php
// A legacy page: a form with a random token, notes appended to a file in its own folder.
if ($_SERVER['REQUEST_METHOD'] === 'POST' && ($_POST['_token'] ?? '') !== '') {
    if (!is_dir(__DIR__ . '/data')) {
        mkdir(__DIR__ . '/data');
    }
    file_put_contents(__DIR__ . '/data/notes.txt', ($_POST['note'] ?? '') . "\\n", FILE_APPEND);
    echo "<p>Saved</p>";
    return;
}
$token = bin2hex(random_bytes(16));
?>
<!DOCTYPE html>
<html><body>
  <form method="post" action="/"><input type="hidden" name="_token" value="<?= $token ?>">
    <input name="note"><button>Save</button></form>
  <a href="/about.php">About</a> <a href="/logout.php">Sign out</a> <a href="https://example.com/">Elsewhere</a>
</body></html>
""",
    "about.php": "<?php echo '<p>About us</p>';\n",
    "logout.php": "<?php file_put_contents(__DIR__ . '/signed-out.txt', 'bye');\n",
}

NODE_APP = {
    "package.json": json.dumps({"name": "hits", "private": True, "scripts": {"start": "node server.js"}}),
    "server.js": """const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");

http.createServer((req, res) => {
  if (req.method === "POST" && req.url === "/hit") {
    fs.mkdirSync(path.join(__dirname, "data"), { recursive: true });
    fs.appendFileSync(path.join(__dirname, "data", "hits.txt"), "hit\\n");
    res.writeHead(303, { Location: "/docs" });
    return res.end();
  }
  const page = req.url === "/" ? '<a href="/docs">Docs</a> <a href="/admin/delete-all">Wipe</a>' : "<p>Docs</p>";
  res.writeHead(req.url === "/" || req.url === "/docs" ? 200 : 404, { "Content-Type": "text/html" });
  res.end(page);
}).listen(Number(process.env.PORT), "127.0.0.1");
""",
}


def _app(folder: pathlib.Path, files: dict) -> pathlib.Path:
    for rel, text in files.items():
        (folder / rel).parent.mkdir(parents=True, exist_ok=True)
        (folder / rel).write_text(text, encoding="utf-8")
    return folder


def _add_step(app: pathlib.Path, step: dict) -> list[str]:
    path = app / "parityrun" / "scenarios" / "smoke.yaml"
    scenario = yaml.safe_load(path.read_text(encoding="utf-8"))
    scenario["steps"].append(step)
    path.write_text(yaml.safe_dump(scenario, sort_keys=False), encoding="utf-8")
    return [s["call"] for s in scenario["steps"]]


def _recording(app: pathlib.Path) -> dict:
    return json.loads((app / "parityrun" / "golden" / "smoke" / "recording.json").read_text(encoding="utf-8"))


def test_init_sets_up_a_legacy_php_site_that_then_records_and_checks(tmp_path, capsys):
    if not shutil.which("php"):
        pytest.skip("php isn't on PATH")
    app = _app(tmp_path / "site", LEGACY_PHP)
    assert cli.main(["init", str(app)]) == 0
    said = capsys.readouterr().out
    assert "Found: PHP app" in said and "form tokens captured: _token" in said
    assert _add_step(app, {"call": "POST /", "form": {"_token": "{{_token}}", "note": "hello"}}) == [
        "GET /", "GET /about.php", "POST /"]  # init didn't follow the sign-out link
    project = str(app / "parityrun" / "parityrun_project.py")
    assert cli.main(["--project", project, "record"]) == 0
    recording = _recording(app)
    steps = recording["steps"]
    assert '<input name="_token" type="hidden" value="<_token>">' in steps[0]["html"]
    assert steps[1]["html"] == ["<p>", "About us", "</p>"] and steps[2]["html"] == ["<p>", "Saved", "</p>"]
    assert recording["files"] == {"app/data/notes.txt": {"text": "hello\n"}}
    assert cli.main(["--project", project, "check"]) == 0
    assert sorted(p.name for p in app.iterdir()) == ["about.php", "index.php", "logout.php", "parityrun"]


def test_init_sets_up_a_node_app_that_then_records_and_checks(tmp_path, capsys, monkeypatch):
    if not shutil.which("node"):
        pytest.skip("node isn't on PATH")
    app = _app(tmp_path / "hits", NODE_APP)
    assert cli.main(["init", str(app)]) == 0
    assert "Found: Node.js app" in capsys.readouterr().out
    assert _add_step(app, {"call": "POST /hit", "follow": True}) == ["GET /", "GET /docs", "POST /hit"]
    monkeypatch.chdir(app)  # from the app's folder, plain `parityrun record` finds parityrun/
    assert cli.main(["record"]) == 0
    recording = _recording(app)
    assert recording["steps"][2]["redirects"] == [[303, "/docs"]]
    assert recording["files"] == {"app/data/hits.txt": {"text": "hit\n"}}
    assert cli.main(["check"]) == 0
    assert sorted(p.name for p in app.iterdir()) == ["package.json", "parityrun", "server.js"]
