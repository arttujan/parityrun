"""End to end, per language: the guestbook PHP site recorded, and its JavaScript, TypeScript and Rust ports checked
against it to 100 % parity; a port with a typical porting bug fails on exactly the checks that show the bug.

Each test skips (and says why) when its toolchain is missing: php (8.1+) for the reference, node for js, node 23.6+
for ts (type stripping), cargo for rust. The tests that use the JavaScript port as the reference need only node.
"""

import json
import pathlib
import re
import shutil
import subprocess

import httpx
import pytest
import yaml

from parityrun import cli, load
from parityrun.capture import Session

GUESTBOOK = pathlib.Path(__file__).resolve().parents[1] / "examples" / "guestbook"
PORTS = ["js", "ts", "rust"]


def _needs(impl: str) -> None:
    tool = {"php": "php", "js": "node", "ts": "node", "rust": "cargo"}[impl]
    if not shutil.which(tool):
        pytest.skip(f"{tool} isn't on PATH: the {impl} implementation can't run")
    if impl == "ts":
        version = subprocess.run(["node", "--version"], capture_output=True, text=True).stdout
        major, minor = (int(x) for x in re.findall(r"\d+", version)[:2])
        if (major, minor) < (23, 6):
            pytest.skip(f"node {version.strip()} can't run TypeScript as is (needs 23.6 or newer)")
    if impl == "rust":
        subprocess.run(["cargo", "build", "--release", "--quiet"], cwd=GUESTBOOK / "rust", check=True, timeout=600)


def _project(folder: pathlib.Path, reference: str, impls: dict | None = None, scenarios: bool = True):
    """The example's settings and scenarios in `folder` (goldens and reports go there); the code runs in place."""
    root = folder / "guestbook"
    skip = ["golden", "out", "php", "js", "ts", "rust"] + ([] if scenarios else ["scenarios"])
    shutil.copytree(GUESTBOOK, root, ignore=shutil.ignore_patterns(*skip))
    if impls:
        spec = json.loads((root / "impls.json").read_text(encoding="utf-8"))
        (root / "impls.json").write_text(json.dumps({**spec, **impls}), encoding="utf-8")
    project = load(root / "parityrun_project.py")
    project.repo = GUESTBOOK
    project.reference = reference
    return project


def _failures(project, impl: str) -> dict:
    """{scenario: [failing check names]} for `check --impl impl`, nothing when parity is 100 %."""
    code = cli.main(["check", "--impl", impl], project=project)
    report = json.loads((project.out / f"report-{impl}.json").read_text(encoding="utf-8"))
    failed = {name: [c["name"] for c in checks if not c["ok"]] for name, checks in report.items()}
    failed = {name: names for name, names in failed.items() if names}
    assert (code == 0) == (not failed)
    return failed


def _recording(project, name: str) -> dict:
    return json.loads((project.golden / name / "recording.json").read_text(encoding="utf-8"))


def _assert_recorded_like_the_site_behaves(project) -> None:
    """What any correct implementation records: the web features of v0.2 seen through a real site."""
    sign = _recording(project, "sign")
    steps = sign["steps"]
    assert steps[0]["status"] == 200 and '<input name="csrf" type="hidden" value="<csrf>">' in steps[0]["html"]
    assert (steps[1]["status"], steps[1]["location"], steps[1]["html"]) == (303, "/entries/<id:1>", [])
    assert steps[2]["status"] == 200 and steps[2]["redirects"] == [[303, "/entries/<id:2>"]]
    assert "location" not in steps[2] and "Tom" in steps[2]["html"]
    assert steps[3]["html"].index('<a href="/entries/<id:2>">') < steps[3]["html"].index('<a href="/entries/<id:1>">')
    page = steps[4]["html"]
    at = page.index('Hello &lt;world&gt; &amp; "friends"')
    assert page[at:at + 3] == ['Hello &lt;world&gt; &amp; "friends"', "<br>", "Second line"]
    assert "Signed <time> UTC" in page and "<title>" in page and "Åsa O'Brien · Guestbook" in page
    assert sign["files"] == {"entries.json": {"json": [
        {"id": "<id:1>", "name": "Åsa O'Brien", "message": 'Hello <world> & "friends"\nSecond line', "at": "<time>"},
        {"id": "<id:2>", "name": "Tom", "message": "Nice site!", "at": "<time>"}]}}

    steps = _recording(project, "validation")["steps"]
    assert [s["status"] for s in steps] == [200, 422, 422, 303, 403, 403, 303, 200]
    assert "Please write your name." in steps[1]["html"] and "Please write a message." in steps[1]["html"]
    assert "Your name can be at most 40 characters." in steps[2]["html"]
    assert "Your message can be at most 200 characters." in steps[2]["html"]
    assert " " in steps[7]["html"]  # the no-break-space name was accepted and is listed

    delete = _recording(project, "delete")
    steps = delete["steps"]
    assert steps[1]["location"] == "/entries/<entry>"  # a deleted entry's id lives on only as its capture label
    assert [s["status"] for s in steps] == [200, 303, 403, 200, 404, 404, 404, 404]
    assert steps[3]["redirects"] == [[303, "/"]] and "0 entries" in steps[3]["html"]
    assert delete["files"] == {"entries.json": {"json": []}}


@pytest.fixture(scope="module")
def php_site(tmp_path_factory):
    """The PHP site recorded once (twice per scenario: record refuses an unstable run)."""
    _needs("php")
    project = _project(tmp_path_factory.mktemp("php"), "php")
    assert cli.main(["record"], project=project) == 0
    return project


def test_the_php_site_records_what_it_does(php_site):
    _assert_recorded_like_the_site_behaves(php_site)


@pytest.mark.parametrize("port", PORTS)
def test_each_port_matches_the_php_site(php_site, port):
    _needs(port)
    assert _failures(php_site, port) == {}


@pytest.fixture(scope="module")
def js_site(tmp_path_factory):
    """The JavaScript port as the reference: the same checks without PHP installed."""
    _needs("js")
    project = _project(tmp_path_factory.mktemp("js"), "js")
    assert cli.main(["record"], project=project) == 0
    return project


def test_the_javascript_port_records_like_the_site(js_site):
    _assert_recorded_like_the_site_behaves(js_site)


@pytest.mark.parametrize("port", ["ts", "rust"])
def test_the_other_ports_match_the_javascript_port(js_site, port):
    _needs(port)
    assert _failures(js_site, port) == {}


@pytest.mark.parametrize(("reference", "ports"), [("php", ["js", "ts", "rust"]), ("js", ["ts", "rust"])])
def test_browsing_the_site_through_capture_becomes_a_scenario_every_port_passes(tmp_path, reference, ports):
    """Routes read from the code, ticked off while a browser uses the site; the session replays on each port."""
    _needs(reference)
    project = _project(tmp_path, reference, scenarios=False)
    session = Session(project, port=0)
    assert session.keys == ["GET /", "GET /entries/{param}", "POST /entries/{param}/delete", "GET /health",
                            "POST /sign"]  # read from the reference's code
    session.start()
    try:
        browser = httpx.Client(base_url=session.url, follow_redirects=True)
        token = re.search(r'name="csrf" value="([0-9a-f]+)"', browser.get("/").text).group(1)
        entry = browser.post("/sign", data={"csrf": token, "name": "Ann", "message": "Hello\nthere"})
        assert entry.status_code == 200 and "Ann" in entry.text
        browser.get("/")
        assert "0 entries" in browser.post(f"{entry.url.path}/delete", data={"csrf": token}).text
        assert browser.get("/nowhere").status_code == 404
        assert "4 of 5 routes visited; 1 to go" in browser.get("/__parityrun").text
    finally:
        session.stop()
    steps = yaml.safe_load(session.save("browse").read_text(encoding="utf-8"))["steps"]
    assert [s["call"] for s in steps] == ["GET /", "POST /sign", "GET /entries/{{entry}}", "GET /",
                                          "POST /entries/{{entry}}/delete", "GET /", "GET /nowhere"]
    assert steps[1]["form"] == {"csrf": "{{csrf}}", "name": "Ann", "message": "Hello\nthere"}
    assert cli.main(["record", "browse"], project=project) == 0
    for port in ports:
        _needs(port)
        assert cli.main(["check", "--impl", port, "browse"], project=project) == 0, port


def test_a_port_that_trims_like_javascript_fails_where_php_differs(js_site, tmp_path):
    """String.trim() also strips a no-break space; PHP's trim() keeps it, so that name is refused by mistake."""
    source = (GUESTBOOK / "js" / "server.js").read_text(encoding="utf-8")
    broken = source.replace('phpTrim(rq.field("name"))', 'rq.field("name").trim()')
    assert broken != source
    (tmp_path / "server.js").write_text(broken, encoding="utf-8")
    spec = json.loads((js_site.root / "impls.json").read_text(encoding="utf-8"))
    spec["js-trim"] = {"cwd": "js", "cmd": ["node", str(tmp_path / "server.js"), "{port}"]}
    (js_site.root / "impls.json").write_text(json.dumps(spec), encoding="utf-8")
    assert _failures(js_site, "js-trim") == {"validation": [
        "step 6 POST /sign status", "step 6 POST /sign location", "step 6 POST /sign html",
        "step 7 GET / html", "file entries.json"]}
