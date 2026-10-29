"""A project: where its scenarios and recordings live, and what its service needs to run under the harness.

A project is a Python file that defines `PROJECT = Project(...)` (see the README). Everything with a default works
for a plain HTTP service; the rest describes the service: the environment it starts with, how to tell it is up and
how to stop it, the SQLite databases and files it leaves behind, and any outside services the stub answers for it.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import pathlib
import sys
from typing import Callable


@dataclasses.dataclass
class RunInfo:
    """What one run gives the service: its fresh data folder, the run's work folder, its port and the stub's URL."""
    data_dir: pathlib.Path
    work: pathlib.Path
    port: int
    stub_url: str
    repo: pathlib.Path


@dataclasses.dataclass
class Project:
    root: pathlib.Path
    """Holds scenarios/, golden/, fixtures/ and impls.json; check writes out/ here."""
    repo: pathlib.Path | None = None
    """What {repo} means in impls.json and scenario env (default: the folder above root)."""
    reference: str = "main"
    """The implementation in impls.json that `record` runs."""
    quick: list[str] = dataclasses.field(default_factory=list)
    """The scenarios `check --quick` runs (`--slow` runs the rest)."""
    env: Callable[[RunInfo], dict] | None = None
    """The environment every implementation gets on top of the harness's own (the contract)."""
    ready: str = "/"
    """GET this path until it answers 200: the service is up."""
    ready_timeout: float = 90
    shutdown: str | None = None
    """"POST /api/shutdown": asked at the end of a run; without it (or when it doesn't stop) the process is killed."""
    headers: dict = dataclasses.field(default_factory=dict)
    """Sent with every request (e.g. a CSRF header)."""
    events: str | None = None
    """The server-sent events path a step with `sse: n` listens to."""
    databases: list[str] = dataclasses.field(default_factory=list)
    """SQLite files in the data folder; the first is recorded (schema and rows) as `db`."""
    snapshot: Callable[[RunInfo], dict] | None = None
    """Any other database, read after the run: {"schema": ..., "rows": {table: [row, ...]}}, recorded as `db`
    instead of the SQLite file (parityrun.snapshot.dbapi reads MySQL, PostgreSQL or SQLite connections)."""
    setup: Callable[[RunInfo], None] | None = None
    """Called before each start, e.g. to empty an outside database so every run begins from the same state."""
    html: bool = False
    """Record HTML responses as normalised lines (`html`) instead of their exact text: attribute order, entity
    spelling, indentation and comments don't count (parityrun.htmlnorm)."""
    redirects: bool = False
    """Record the Location of every 3xx answer (`location`), normalised like everything else."""
    isolate: bool = False
    """Run each implementation in a fresh copy of its folder (impls.json `cwd`), so an app that writes into its own
    folder (uploads, a SQLite file, caches) needs no data-folder setting; what a run created, changed or deleted
    there is recorded as app/<path>. `databases` may then name a file inside the app (e.g. database/app.sqlite)."""
    isolate_skip: list[str] = dataclasses.field(default_factory=lambda: [".git", ".hg", ".svn"])
    """Folder and file names not copied for an isolated run."""
    coverage: bool = False
    """Measure which lines of a PHP implementation's code the scenarios run (PCOV or Xdebug): kept beside each
    recording as coverage.json, totalled by `record` and listed per file by `parityrun coverage`."""
    ignore_files: list[str] = dataclasses.field(default_factory=list)
    """Path patterns never recorded (fnmatch): logs, caches, sessions ("storage/logs/*", "*.log")."""
    random_files: set[str] = dataclasses.field(default_factory=set)
    """File names that are random by design: only their size is compared."""
    interleaved_files: set[str] = dataclasses.field(default_factory=set)
    """JSON Lines files written by parallel work: their lines are compared in any order."""
    measured_keys: set[str] = dataclasses.field(default_factory=set)
    """Numeric keys that are wall-clock measurements: recorded as "<measured>"."""
    measured_companions: set[str] = dataclasses.field(default_factory=set)
    """Numeric keys computed from a measurement beside them in the same object (e.g. a cost): "<measured>" too."""
    extra_roots: dict[str, str] = dataclasses.field(default_factory=dict)
    """{"site": "site"}: files under <work>/site are recorded as site/... (a folder the service writes outside data)."""
    stub_handler: Callable | None = None
    """Answers for outside services: handler(stub, method, host, rest, query, body, headers) -> (status, body, headers)
    or None. Calls to {stub}/<host>/<path> reach it (point the service's API base there)."""
    stub_queues: list[str] = dataclasses.field(default_factory=list)
    """Step keys whose (templated) list is added to stub.queues[key] before the step, for stub_handler to hand out."""
    post_process: Callable[[dict], None] | None = None
    """Changes the normalised recording in place before it is compared or saved (e.g. rows inserted in any order)."""
    labels: dict = dataclasses.field(default_factory=lambda: dict(LABELS))
    """The placeholders for this run's data folder, work folder, stub and service addresses."""
    image_ssim: float = 0.98
    """How alike two pictures must be (structural similarity) to count as the same."""
    work_prefix: str = "parityrun-"
    """The start of each run's temporary folder name (keep it when output wraps at a path's length)."""
    prog: str = "parityrun"
    """The command name the help text shows."""

    def __post_init__(self) -> None:
        self.root = pathlib.Path(self.root).resolve()
        self.repo = pathlib.Path(self.repo).resolve() if self.repo else self.root.parent
        self.labels = {**LABELS, **self.labels}

    @property
    def scenarios(self) -> pathlib.Path:
        return self.root / "scenarios"

    @property
    def golden(self) -> pathlib.Path:
        return self.root / "golden"

    @property
    def fixtures(self) -> pathlib.Path:
        return self.root / "fixtures"

    @property
    def out(self) -> pathlib.Path:
        return self.root / "out"


LABELS = {"data": "<data>", "work": "<work>", "stub": "<stub>", "service": "<service>"}


def load(path: str | pathlib.Path) -> Project:
    """The PROJECT a project file defines."""
    path = pathlib.Path(path).resolve()
    spec = importlib.util.spec_from_file_location(f"parityrun_project_{abs(hash(str(path)))}", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"Can't load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    project = getattr(module, "PROJECT", None)
    if not isinstance(project, Project):
        raise SystemExit(f"{path} defines no PROJECT = parityrun.Project(...)")
    return project
