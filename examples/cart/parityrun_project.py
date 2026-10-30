"""The cart example: a PHP library (the reference, served by parityrun's library shim) and its ports to JavaScript,
TypeScript and Rust, each with its own shim. Scenarios are calls (`fn`, `new`), not requests.

  parityrun --project examples/cart/parityrun_project.py record             (php on PATH)
  parityrun --project examples/cart/parityrun_project.py check --impl js    (node)
  ... check --impl ts      (node 23.6 or newer runs TypeScript as is)
  ... check --impl rust    (cargo: check builds it first)
"""

import pathlib

from parityrun import Project

PROJECT = Project(
    root=pathlib.Path(__file__).parent,
    repo=pathlib.Path(__file__).parent,
    reference="php",
    library=True,
    ready="/health",
)
