"""A stand-in for the `claude` command line, answered by the stub.

It sends its arguments and stdin to the stub, which records them like an LLM request and answers with a
`--output-format json` envelope.

A scenario uses it with an env value "{fake_claude}" (e.g. CLAUDE_BIN: "{fake_claude}"): the harness makes a real
executable for it in the run's folder (a .cmd file would cut a multi-line argument at its first newline). The stub's
address comes from PARITYRUN_STUB (set for every run) or CONFORMANCE_STUB.
"""

import os
import sys

import httpx


def main() -> int:
    """Pass the arguments and stdin to the stub and print its answer."""
    stub = os.environ.get("PARITYRUN_STUB") or os.environ["CONFORMANCE_STUB"]
    request = {"args": sys.argv[1:], "stdin": sys.stdin.buffer.read().decode("utf-8")}
    resp = httpx.post(stub.rstrip("/") + "/v1/claude", json=request, timeout=60)
    _ = resp.raise_for_status()
    _ = sys.stdout.buffer.write(resp.content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
