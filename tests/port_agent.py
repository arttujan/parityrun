"""A stand-in for an AI tool, for tests/test_port.py: it "ports" the guestbook (a site) or the cart (a library).

It copies the example's own port in the language of the folder it works in, as PORTING.md says it must run. The first
turn's copy has a porting bug; a later turn, which names the failing scenario, gets it right.

  python port_agent.py PROMPT_FILE        (in the port's folder; the prompt also arrives on stdin)

Exit codes other than 0 tell the test what the loop didn't give it.
"""

import os
import pathlib
import re
import sys

EXAMPLES = pathlib.Path(__file__).resolve().parents[1] / "examples"
PORT = ("Number(process.argv[2] || 8080)", "Number(process.env.PORT)")  # the guestbook reads its port from argv
SITE = {  # the port's file: the example's file, what to change in it -> to what; then the first turn's bug
    "server.js": ("guestbook/js/server.js", [PORT]),
    "server.ts": ("guestbook/ts/server.ts", [PORT]),
    "src/main.rs": (
        "guestbook/rust/src/main.rs",
        [('std::env::args().nth(1).unwrap_or_else(|| "8080".into())', 'std::env::var("PORT").expect("PORT")')],
    ),
}
SITE_BUG = ("Please write your name.", "Please enter your name.")
NODE_ROUND = ("Math.sign(x) * Math.round(Math.abs(x)) || 0", "Math.round(x) || 0")  # -0.5 -> 0, not PHP's -1
LIBRARY = {  # the skeleton's shim tells a library's port; each of its files, from the cart example
    "shim.js": {"index.js": "cart/js/index.js", "shim.js": "cart/js/shim.js"},
    "shim.ts": {"index.ts": "cart/ts/index.ts", "shim.ts": "cart/ts/shim.ts"},
    "src/json.rs": {
        "src/lib.rs": "cart/rust/src/lib.rs",
        "src/main.rs": "cart/rust/src/main.rs",
        "src/json.rs": "cart/rust/src/json.rs",
    },
}
LIBRARY_BUGS = [NODE_ROUND, ("(cents as f64 * rate).round() as i64", "(cents as f64 * rate + 0.5).floor() as i64")]


def _write(here: pathlib.Path, rel: str, code: str, bugs: list[tuple[str, str]], *, first: bool) -> None:
    for old, new in bugs if first else []:  # a hurried first try
        code = code.replace(old, new)
    _ = (here / rel).write_text(code, encoding="utf-8")


def main() -> int:
    """Port the folder it runs in; 0 when it did."""
    task = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
    if sys.stdin.buffer.read().decode("utf-8") != task:
        return 3
    if not pathlib.Path(os.environ.get("PARITYRUN_PROJECT", "")).is_file():
        return 4  # the agent's own `parityrun check` would find no project
    here = pathlib.Path.cwd()
    if not (here / "PORTING.md").exists():
        return 5
    first = task.startswith("Turn 1 ")
    library = next((files for shim, files in LIBRARY.items() if (here / shim).exists()), None)
    if library:
        crate = (
            re.search(r"use (\w+) as lib;", (here / "src/main.rs").read_text(encoding="utf-8"))
            if (here / "src/main.rs").exists()
            else None
        )
        for rel, source in library.items():
            code = (EXAMPLES / source).read_text(encoding="utf-8")
            if crate:  # the example's crate is `cart`; the skeleton's is named after its folder
                code = code.replace("use cart as lib;", f"use {crate.group(1)} as lib;")
            _write(here, rel, code, LIBRARY_BUGS, first=first)
        return 0
    rel = next(r for r in SITE if (here / r).exists())
    source, changes = SITE[rel]
    code = (EXAMPLES / source).read_text(encoding="utf-8")
    for old, new in changes:
        if old not in code:
            return 6
        code = code.replace(old, new)
    _write(here, rel, code, [SITE_BUG], first=first)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
