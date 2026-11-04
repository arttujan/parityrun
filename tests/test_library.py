"""Libraries: `fn` and `new` steps through a shim, their recording and comparison, the API read from PHP code.

Also API coverage, and the shims parityrun ships (the examples' shims must stay the templates with the tables filled
in).

Nothing here needs PHP: the shim these tests run is a tiny Python one speaking the same protocol.
"""

import json
import pathlib
import re

import pytest
import yaml

from parityrun import Project, RunInfo, api, cli, init, load, runner

CART = pathlib.Path(__file__).resolve().parents[1] / "examples" / "cart"

FAKE_SHIM = r"""
import http.server, json, os

objects = {}

def call(request):
    args = request.get("args", [])
    values = list(args.values()) if isinstance(args, dict) else args
    if "new" in request:
        value = {"$object": request["new"], "n": values[0]}
    elif "." in request["fn"]:
        name, method = request["fn"].split(".")
        if name not in objects:
            return 400, {"error": f"no object called {name}"}
        value = objects[name][method]
    elif request["fn"] == "add":
        value = sum(values)
    elif request["fn"] == "boom":
        return 200, {"error": {"class": "ValueError", "message": "no"}}
    elif request["fn"] == "say":
        return 200, {"value": None, "output": "hi\n"}
    if request.get("as"):
        objects[request["as"]] = value
    return 200, {"value": value}

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def answer(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.answer(200, "ok")

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.answer(*call(request))

http.server.HTTPServer(("127.0.0.1", int(os.environ["PORT"])), Handler).serve_forever()
"""

STEPS = """steps:
- fn: add
  args: [2, 3]
  id: sum
- fn: add
  args: ["{{sum.value}}", 10]
- new: Box
  args: [7]
  as: box
- fn: box.n
- fn: boom
- fn: say
- fn: add
  args: {a: 1, b: 2}
"""


def _library(tmp_path: pathlib.Path, steps: str = STEPS) -> Project:
    _ = (tmp_path / "shim.py").write_text(FAKE_SHIM, encoding="utf-8")
    (tmp_path / "scenarios").mkdir()
    _ = (tmp_path / "scenarios" / "calls.yaml").write_text(steps, encoding="utf-8")
    _ = (tmp_path / "impls.json").write_text(
        json.dumps({"py": {"cmd": ["{python}", str(tmp_path / "shim.py")], "env": {"PORT": "{port}"}}}),
        encoding="utf-8",
    )
    return Project(root=tmp_path, reference="py", library=True, ready="/health")


def test_calls_are_recorded_with_what_they_return_throw_and_print(tmp_path: pathlib.Path) -> None:
    project = _library(tmp_path)
    recording = runner.run(runner.Implementation(project, "py"), "calls", tmp_path / "out")
    assert recording["steps"] == [
        {"call": "fn add", "result": {"value": 5}},
        {"call": "fn add", "result": {"value": 15}},  # a whole-value template keeps the number a number
        {"call": "new Box", "result": {"value": {"$object": "Box", "n": 7}}},
        {"call": "fn box.n", "result": {"value": 7}},  # a method on the object `as` kept
        {"call": "fn boom", "result": {"error": {"class": "ValueError", "message": "no"}}},
        {"call": "fn say", "result": {"value": None, "output": "hi\n"}},
        {"call": "fn add", "result": {"value": 3}},
    ]  # named arguments
    other = json.loads(json.dumps(recording))
    other["steps"][4]["result"]["error"]["message"] = "nope"
    failing = [c["name"] for c in runner.checks(recording, other, tmp_path, tmp_path) if not c["ok"]]
    assert failing == ["step 4 fn boom result"]


def test_a_call_the_shim_cant_make_stops_the_run_with_its_answer(tmp_path: pathlib.Path) -> None:
    project = _library(tmp_path, "steps:\n- fn: nobody.n\n")
    with pytest.raises(RuntimeError, match=r"calls step 0 \(fn nobody.n\): the shim answered 400: .*no object called"):
        _ = runner.run(runner.Implementation(project, "py"), "calls", tmp_path / "out")


PHP_API = r"""<?php
namespace Shop;

use Other\Thing;

const BRACES = '{ not a block }';

function price(int $cents, string $currency = "EUR", ...$rest): string
{
    $f = function ($x) { return "}"; };  // a closure is no API, and a brace in a string is no brace
    $o = new class extends Thing { public function hidden(): void { } };  // nor is an anonymous class
    return "{$cents}";
}

/* function commented_out() {} */
interface Priced { public function price(): int; }
trait Tagged { public function tags(): array { return []; } }
abstract class Base { public function id(): int { return 1; } }

final class Cart extends Base implements Priced
{
    private array $lines = [];

    public function __construct(private string $owner, array $lines = [])
    {
    }

    public static function empty(): self { return new self('nobody'); }
    public function price(): int { return 0; }
    function add(string $sku, int $qty = 1): void { }
    protected function guard(): void { }
    private static function secret(): void { }
    public function __toString(): string { return ''; }
}

class Hidden
{
    private function __construct() { }
    public static function make(): self { return new self(); }
}

enum Size: string
{
    case S = 's';
    public function label(): string { return 'small'; }
}
"""


def test_the_api_is_read_from_php_code() -> None:
    found = api.php(PHP_API, "src/Shop.php")
    parts = {p.key: p for p in found}
    assert [p.key for p in found] == [
        "new Shop\\Cart",  # not Hidden (a private constructor), Base (abstract), the interface, trait or enum
        "fn Shop\\price",
        "fn Shop\\Base->id",
        "fn Shop\\Cart::empty",
        "fn Shop\\Cart->price",
        "fn Shop\\Cart->add",
        "fn Shop\\Hidden::make",
        "fn Shop\\Size->label",
    ]  # no private, protected or magic methods, no closures
    assert parts["fn Shop\\price"].params == '(int $cents, string $currency = "EUR", ...$rest)'
    assert parts["fn Shop\\price"].required == 1
    assert parts["fn Shop\\price"].source == "src/Shop.php:8"
    assert parts["new Shop\\Cart"].params == "(private string $owner, array $lines = [])"
    assert parts["fn Shop\\Cart->add"].required == 1


def test_braced_namespaces_and_global_functions_are_read_too() -> None:
    keys = [p.key for p in api.php("<?php\nfunction top() {}\nnamespace A { function f() {} class K {} }\n", "x.php")]
    assert keys == ["new A\\K", "fn top", "fn A\\f"]


def test_a_step_names_the_part_it_calls() -> None:
    objects: dict[str, str] = {}
    assert api.key({"new": "Shop\\Cart", "as": "cart"}, objects) == "new Shop\\Cart"
    assert api.key({"fn": "cart.add"}, objects) == "fn Shop\\Cart->add"
    assert api.key({"fn": "Shop\\Cart::empty"}, objects) == "fn Shop\\Cart::empty"
    assert api.key({"fn": "other.add"}, objects) == "fn other.add"  # an object no `new` made: unknown class


def test_routes_lists_a_librarys_api_and_coverage_counts_the_calls(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = load(CART / "parityrun_project.py")
    project.root = tmp_path / "cart"  # the example's scenarios, its golden/ written elsewhere
    (project.root / "scenarios").mkdir(parents=True)
    for scenario in (CART / "scenarios").glob("*.yaml"):
        _ = (project.root / "scenarios" / scenario.name).write_bytes(scenario.read_bytes())
    _ = (project.root / "impls.json").write_bytes((CART / "impls.json").read_bytes())
    assert cli.main(["routes"], project=project) == 0
    said = capsys.readouterr().out
    assert "  x fn Cart\\vat(int $cents, float $rate = 0.24)" in said
    assert "src/functions.php:19" in said
    assert "11 functions, methods and classes, 11 called by the scenarios" in said
    assert json.loads((project.golden / "endpoints.json").read_text(encoding="utf-8"))[0] == "new Cart\\Cart"
    (project.scenarios / "cart.yaml").unlink()
    assert cli.main(["coverage"], project=project) == 0
    said = capsys.readouterr().out
    assert "API coverage: 5/11 (45 %)" in said
    assert "not covered: fn Cart\\Cart->add" in said
    assert cli.main(["capture"], project=project) == 1
    assert "A library has no pages to visit" in capsys.readouterr().out


def test_the_shims_are_shipped_and_found_through_the_placeholder() -> None:
    assert {p.name for p in runner.SHIMS.iterdir()} >= {
        "library.php",
        "library.js",
        "library.ts",
        "library.rs",
        "json.rs",
    }
    assert f"{runner.SHIMS.as_posix()}/library.php" == (runner.SHIMS / "library.php").as_posix()


def _after(text: str, marker: str) -> str:
    assert marker in text, marker
    return text[text.index(marker) :]


@pytest.mark.parametrize(
    ("example", "template", "protocol"),
    [
        ("js/shim.js", "library.js", "function named("),
        ("ts/shim.ts", "library.ts", "function named("),
        ("rust/src/main.rs", "library.rs", "fn answer("),
        ("rust/src/json.rs", "json.rs", "//!"),
    ],
)
def test_the_examples_shims_are_the_templates_with_the_tables_filled_in(
    example: str, template: str, protocol: str
) -> None:
    """What a port gets from `parityrun port` is what the examples prove end to end."""
    mine = (CART / example).read_text(encoding="utf-8")
    shipped = (runner.SHIMS / template).read_text(encoding="utf-8")
    assert _after(mine, protocol) == _after(shipped, protocol)
    assert _protocol_lines(mine) == _protocol_lines(shipped)


def _protocol_lines(text: str) -> list[str]:
    """Return the header comment's lines that describe the protocol."""
    return [line for line in text.splitlines()[:8] if re.match(r"//\s+(POST|GET|\{|->)", line)]


def _files(root: pathlib.Path, files: dict[str, str]) -> pathlib.Path:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        _ = (root / rel).write_text(text, encoding="utf-8")
    return root


MONEY = {
    "src/Money.php": "<?php\nnamespace Pay;\nclass Money { public function __construct(int $cents) {} }\n",
    "src/functions.php": "<?php\nnamespace Pay;\nfunction zero(): int { return 0; }\n"
    "function cents(string $text): int { return 0; }\n",
}


def test_init_tells_a_php_library_from_a_site(tmp_path: pathlib.Path) -> None:
    lib = init.detect(_files(tmp_path / "lib", {**MONEY, "composer.json": '{"type": "library"}'}))
    assert (lib.label, lib.library, lib.cmd) == ("PHP library", True, ["php", "{shims}/library.php"])
    assert lib.env["PARITYRUN_LIB"] == "vendor/autoload.php,src/functions.php"  # functions aren't autoloaded
    assert any("composer install" in note for note in lib.notes)
    plain = init.detect(_files(tmp_path / "plain", MONEY))  # no composer.json, no page
    assert plain.library
    assert plain.env["PARITYRUN_LIB"] == "src/Money.php,src/functions.php"
    assert any("name order" in note for note in plain.notes)
    site = init.detect(_files(tmp_path / "site", {**MONEY, "index.php": "<?php echo 1;"}))
    assert not site.library
    assert site.cmd[:2] == ["php", "-S"]


def test_init_writes_a_library_project_with_its_api_and_first_calls(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def which(tool: str) -> str:
        return f"/usr/bin/{tool}"

    monkeypatch.setattr(init.shutil, "which", which)
    app = _files(tmp_path / "lib", MONEY)
    assert init.init(app, check_start=False) == 0
    said = capsys.readouterr().out
    assert "Found: PHP library" in said
    assert "3 functions, methods and classes found" in said
    assert "edit scenarios/calls.yaml" in said
    folder = app / "parityrun"
    impls = json.loads((folder / "impls.json").read_text(encoding="utf-8"))
    assert impls == {
        "php": {"cmd": ["php", "{shims}/library.php"], "env": {"PARITYRUN_LIB": "src/Money.php,src/functions.php"}}
    }
    project = load(folder / "parityrun_project.py")
    assert project.library
    assert project.ready == "/health"
    assert project.coverage
    assert project.isolate
    sample = RunInfo(data_dir=tmp_path, work=tmp_path, port=5, stub_url="", repo=app)
    assert project.env
    assert project.env(sample) == {"PORT": "5"}  # the files to load are the reference's own, not the contract
    calls = (folder / "scenarios" / "calls.yaml").read_text(encoding="utf-8")
    assert "#   fn Pay\\cents(string $text)\n" in calls
    assert yaml.safe_load(calls) == {"steps": [{"fn": "Pay\\zero"}]}  # the calls that need no argument
    assert json.loads((folder / "golden" / "endpoints.json").read_text(encoding="utf-8")) == [
        "new Pay\\Money",
        "fn Pay\\zero",
        "fn Pay\\cents",
    ]
