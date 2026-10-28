"""A stand-in for the `claude` command line: sends its arguments and stdin to the stub, which records them like an LLM
request and answers with a `--output-format json` envelope.

A scenario uses it with an env value "{fake_claude}" (e.g. CLAUDE_BIN: "{fake_claude}"): the harness makes a real
executable for it in the run's folder (a .cmd file would cut a multi-line argument at its first newline). The stub's
address comes from PARITYRUN_STUB (set for every run) or CONFORMANCE_STUB."""

import json
import os
import sys
import urllib.request


def main() -> int:
    stub = os.environ.get("PARITYRUN_STUB") or os.environ["CONFORMANCE_STUB"]
    request = {"args": sys.argv[1:], "stdin": sys.stdin.buffer.read().decode("utf-8")}
    req = urllib.request.Request(stub.rstrip("/") + "/v1/claude", data=json.dumps(request).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        sys.stdout.buffer.write(resp.read())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
