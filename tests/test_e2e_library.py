"""End to end, per language, for libraries: the cart example's PHP library recorded through parityrun's PHP shim, and
its JavaScript, TypeScript and Rust ports (each with its shim, made from parityrun's templates) checked against it
to 100 % parity; a port with a typical porting bug fails on exactly the call that shows it.

Each test skips when its toolchain is missing: php (8.2+) for the reference, node for js, node 23.6+ for ts, cargo
for rust; the coverage test also needs PCOV or Xdebug.
"""

import json
import pathlib
import re
import shutil
import subprocess

import pytest

from parityrun import cli, coverage, load

CART = pathlib.Path(__file__).resolve().parents[1] / "examples" / "cart"
PORTS = ["js", "ts", "rust"]


def _needs(impl: str) -> None:
    tool = {"php": "php", "js": "node", "ts": "node", "rust": "cargo"}[impl]
    if not shutil.which(tool):
        pytest.skip(f"{tool} isn't on PATH: the {impl} implementation can't run")
    if impl == "ts":
        version = subprocess.run(["node", "--version"], capture_output=True, text=True).stdout
        if tuple(int(x) for x in re.findall(r"\d+", version)[:2]) < (23, 6):
            pytest.skip(f"node {version.strip()} can't run TypeScript as is (needs 23.6 or newer)")


def _project(folder: pathlib.Path, impls: dict | None = None):
    """The example's settings and scenarios in `folder` (goldens and reports go there); the code runs in place."""
    root = folder / "cart"
    shutil.copytree(CART, root, ignore=shutil.ignore_patterns("golden", "out", "php", "js", "ts", "rust"))
    if impls:
        spec = json.loads((root / "impls.json").read_text(encoding="utf-8"))
        (root / "impls.json").write_text(json.dumps({**spec, **impls}), encoding="utf-8")
    project = load(root / "parityrun_project.py")
    project.repo = CART
    return project


def _failures(project, impl: str) -> dict:
    code = cli.main(["check", "--impl", impl], project=project)
    report = json.loads((project.out / f"report-{impl}.json").read_text(encoding="utf-8"))
    failed = {name: [c["name"] for c in checks if not c["ok"]] for name, checks in report.items()}
    failed = {name: names for name, names in failed.items() if names}
    assert (code == 0) == (not failed)
    return failed


def _results(project, name: str) -> list:
    recording = json.loads((project.golden / name / "recording.json").read_text(encoding="utf-8"))
    return [step["result"] for step in recording["steps"]]


@pytest.fixture(scope="module")
def php_library(tmp_path_factory):
    """The PHP library recorded once (twice per scenario: record refuses an unstable run)."""
    _needs("php")
    project = _project(tmp_path_factory.mktemp("php"))
    assert cli.main(["record"], project=project) == 0
    return project


def test_the_php_library_records_what_it_returns_prints_and_throws(php_library):
    values = [r.get("value") for r in _results(php_library, "functions")]
    assert values == ["1 234,50 €", "-0,01 €", "0,05 €", "hello-world", "sa-s-l-br-d-2", 300, -1, 1, 100]

    cart = _results(php_library, "cart")
    assert cart[0] == {"value": {"$object": "Cart\\Cart", "currency": "EUR"}}
    assert [r["value"] for r in cart[1:4]] == [3, 4, 5]
    assert cart[4] == {"error": {"class": "InvalidArgumentException", "message": "Quantity must be positive, got 0"}}
    assert cart[5] == {"value": 345}
    assert cart[6] == {"value": [{"sku": "apple", "qty": 5, "price": 45}, {"sku": "pear", "qty": 1, "price": 120}]}
    assert cart[7] == {"value": None, "output": f"{'apple':<10} {5:>3} x 0,45 €\n{'pear':<10} {1:>3} x 1,20 €\n"
                                                 "Total: 3,45 €\n"}
    assert cart[8] == {"error": {"class": "OutOfBoundsException", "message": "No kiwi in the cart"}}
    assert cart[9:] == [{"value": None}, {"value": 5}]

    saved = _results(php_library, "json")
    assert saved[0] == {"value": {"$object": "Cart\\Cart", "currency": "SEK"}}
    assert saved[1]["value"][0] == {"sku": "123", "qty": 2, "price": 1999}  # json_decode made "123" an int key
    assert saved[2] == {"value": 4348}
    assert saved[4] == {"error": {"class": "JsonException", "message": "Syntax error"}}
    assert saved[5] == {"value": {"$object": "Cart\\Cart", "currency": "USD"}}


@pytest.mark.parametrize("port", PORTS)
def test_each_port_matches_the_php_library(php_library, port):
    _needs(port)
    assert _failures(php_library, port) == {}


def test_a_port_that_rounds_like_javascript_fails_where_php_differs(php_library, tmp_path):
    """Math.round takes -0.5 up to -0; PHP's round() takes it away from zero, to -1."""
    _needs("js")
    shutil.copytree(CART / "js", tmp_path / "js")
    source = (tmp_path / "js" / "index.js").read_text(encoding="utf-8")
    broken = source.replace("Math.sign(x) * Math.round(Math.abs(x)) || 0", "Math.round(x) || 0")
    assert broken != source
    (tmp_path / "js" / "index.js").write_text(broken, encoding="utf-8")
    spec = json.loads((php_library.root / "impls.json").read_text(encoding="utf-8"))
    spec["js-round"] = {"cwd": str(tmp_path / "js"), "cmd": ["node", "shim.js"], "env": {"PORT": "{port}"}}
    (php_library.root / "impls.json").write_text(json.dumps(spec), encoding="utf-8")
    assert _failures(php_library, "js-round") == {"functions": ["step 6 fn Cart\\vat result"]}


def test_the_php_shim_measures_the_lines_the_calls_ran(tmp_path):
    _needs("php")
    if coverage.detect(shutil.which("php"))[1] is None:
        pytest.skip("PHP has neither PCOV nor Xdebug")
    project = _project(tmp_path)
    project.coverage = True
    assert cli.main(["record", "functions"], project=project) == 0
    files = json.loads((project.golden / "functions" / "coverage.json").read_text(encoding="utf-8"))["files"]
    assert files["src/functions.php"]["run"]  # measured call by call, though the shim never exits by itself
    lines = (CART / "php" / "src" / "Cart.php").read_text(encoding="utf-8").splitlines()
    throw = next(n for n, line in enumerate(lines, 1) if "InvalidArgumentException" in line)
    assert throw not in files.get("src/Cart.php", {}).get("run", [])  # loaded, but no call ran Cart's code


def test_init_sets_up_a_php_library_that_then_records_and_checks(tmp_path, capsys):
    """Only the library's own files: init finds the shim's settings and the API; one scenario's calls written."""
    _needs("php")
    app = tmp_path / "cart"
    shutil.copytree(CART / "php", app)
    assert cli.main(["init", str(app)]) == 0
    said = capsys.readouterr().out
    assert "Found: PHP library" in said and "The library loads" in said
    calls = app / "parityrun" / "scenarios" / "calls.yaml"
    calls.write_text(calls.read_text(encoding="utf-8").replace("steps: []\n", (CART / "scenarios" / "cart.yaml")
                                                               .read_text(encoding="utf-8").split("\n", 1)[1]),
                     encoding="utf-8")
    project = str(app / "parityrun" / "parityrun_project.py")
    assert cli.main(["--project", project, "record"]) == 0
    recording = json.loads((app / "parityrun" / "golden" / "calls" / "recording.json").read_text(encoding="utf-8"))
    assert recording["steps"][5] == {"call": "fn cart.total", "result": {"value": 345}}
    assert cli.main(["--project", project, "check"]) == 0
    assert cli.main(["--project", project, "routes"]) == 0
    assert "11 functions, methods and classes, 7 called by the scenarios" in capsys.readouterr().out
    assert sorted(p.name for p in app.iterdir()) == ["parityrun", "src"]  # the library's folder is left as it was
