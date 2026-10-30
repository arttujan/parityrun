"""A stand-in for an AI tool, for tests/test_port.py: it "ports" the guestbook by copying the example's own port in the
language of the folder it works in, listening on PORT as PORTING.md says. The first turn's copy has a porting bug (a
validation message reworded); a later turn, which names the failing scenario, gets it right.

  python port_agent.py PROMPT_FILE        (in the port's folder; the prompt also arrives on stdin)

Exit codes other than 0 tell the test what the loop didn't give it."""

import os
import pathlib
import sys

GUESTBOOK = pathlib.Path(__file__).resolve().parents[1] / "examples" / "guestbook"
SOURCES = {  # the port's file: the example's file, and how it reads its port -> how the port must
    "server.js": ("js/server.js", "Number(process.argv[2] || 8080)", "Number(process.env.PORT)"),
    "server.ts": ("ts/server.ts", "Number(process.argv[2] || 8080)", "Number(process.env.PORT)"),
    "src/main.rs": ("rust/src/main.rs", 'std::env::args().nth(1).unwrap_or_else(|| "8080".into())',
                    'std::env::var("PORT").expect("PORT")'),
}


def main() -> int:
    task = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
    if sys.stdin.buffer.read().decode("utf-8") != task:
        return 3
    if not pathlib.Path(os.environ.get("PARITYRUN_PROJECT", "")).is_file():
        return 4  # the agent's own `parityrun check` would find no project
    here = pathlib.Path.cwd()
    if not (here / "PORTING.md").exists():
        return 5
    rel = next(r for r in SOURCES if (here / r).exists())
    source, old, new = SOURCES[rel]
    code = (GUESTBOOK / source).read_text(encoding="utf-8")
    if old not in code:
        return 6
    code = code.replace(old, new)
    if task.startswith("Turn 1 "):  # a hurried first try
        code = code.replace("Please write your name.", "Please enter your name.")
    (here / rel).write_text(code, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
