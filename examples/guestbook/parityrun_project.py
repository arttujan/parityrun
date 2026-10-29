"""The guestbook example: a PHP site (the reference) and its ports to JavaScript, TypeScript and Rust.

  parityrun --project examples/guestbook/parityrun_project.py record             (php on PATH)
  parityrun --project examples/guestbook/parityrun_project.py check --impl js    (node)
  ... check --impl ts      (node 23.6 or newer runs TypeScript as is)
  ... check --impl rust    (first: cargo build --release in examples/guestbook/rust)
"""

import pathlib

from parityrun import Project, RunInfo


def env(run: RunInfo) -> dict:
    return {"GUESTBOOK_DATA": str(run.data_dir)}


PROJECT = Project(
    root=pathlib.Path(__file__).parent,
    repo=pathlib.Path(__file__).parent,
    reference="php",
    quick=["sign", "validation", "delete"],
    env=env,
    ready="/health",
    html=True,
    redirects=True,
)
