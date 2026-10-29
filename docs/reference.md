# parityrun reference

Everything the [README](../README.md) leaves out: what is compared, the project files, every setting, every
scenario key and every command.

- [What is compared](#what-is-compared)
- [Starting from an existing app](#starting-from-an-existing-app): `init`, `capture`, `routes`, code coverage
- [A project](#a-project): the files, `impls.json`, `parityrun_project.py` and its settings
- [Databases](#databases)
- [Scenarios](#scenarios)
- [Commands](#commands)
- [Examples](#examples)

## What is compared

You record what a reference implementation does for a set of scenarios: every response, every LLM prompt, every
call to an outside service, every database row and every file it writes. Then you check another implementation
against those recordings: a rewrite in another language, a port, or the next version of the same code.

- **Black box.** An implementation is a command. parityrun starts it with a fresh data folder (or a fresh copy of
  its own folder) and talks to it only over HTTP and its command line, so the language and framework don't matter.
- **Web pages as a browser reads them.** With `html=True`, HTML is compared tag by tag: attribute order, entity
  spelling, indentation and comments don't count; text, tags and attribute values do. Redirects, forms, uploads and
  cookies work as in a browser.
- **LLM calls are part of the contract.** A built-in stub speaks the OpenAI chat completions API, the Anthropic
  Messages API and the `claude` command line. Each step queues the replies its calls need, and the requests
  themselves (model, messages, schema) are recorded and compared, so a changed prompt fails the check.
- **Outside services are stubbed, and recorded.** Point the app's API base at the stub and answer for any host with
  a small handler; every call (method, path, query, body) is compared.
- **Noise is normalised, not ignored.** Ids are numbered by first appearance, times, dates and unix times become
  placeholders, the run's own paths and ports become labels, captured tokens become `<name>`. Everything else must
  match exactly.
- **Databases by schema and rows** (row order is free): SQLite files, or MySQL and PostgreSQL through a hook.
- **Pictures and videos are compared by eye.** Images by structural similarity (SSIM), videos by duration, size,
  sound and five frames.
- **No flaky goldens.** `record` runs every scenario twice and refuses to save one whose runs differ.

## Starting from an existing app

```
parityrun init        # finds how to start the app and every route in its code, writes parityrun/
parityrun capture     # opens the app in your browser: use it, every route you visit is ticked off
parityrun record      # records what the app does now into parityrun/golden/
parityrun coverage    # the routes and (PHP) the lines of code no scenario ran yet
```

**`init`** tells the stack (PHP, Laravel, Symfony, WordPress, Node.js, Django, Flask, FastAPI, Rust), starts the app
once in a throwaway copy, reads its home page and its code, and writes `parityrun/`: how to start the app
(`impls.json`), the settings (`parityrun_project.py`, with notes on anything it couldn't decide, such as a MySQL
database), the routes found in the code (`golden/endpoints.json`) and a first scenario (`scenarios/smoke.yaml`: the
home page and the pages it links to, hidden form tokens captured, nothing that signs out or deletes). Every run uses
a fresh copy of the app's folder (`isolate`), so the app's own files are never changed and whatever it writes there
is recorded. From the app's folder, the other commands find `parityrun/` by themselves.

**`capture`** starts the app behind a proxy at http://127.0.0.1:8000. Use the site as people do: open the pages, send
the forms. Each page shows how many of the app's routes have been visited, and http://127.0.0.1:8000/__parityrun
lists them all with the ones still to go. Press Finish there (or Ctrl+C in the terminal): what you did becomes
`scenarios/visit.yaml`, one step per page or form. Form tokens and new ids are captured from the answer that gave them
(by the form field, attribute, JSON key or text around them), so the scenario replays on a fresh app; uploaded files
are saved under `fixtures/captured/`; stylesheets, scripts and pictures the browser fetched are left out. The
scenario is then recorded twice to prove it replays. Run `capture` again until every route is visited.

**`routes`** reads the routes from the code into `golden/endpoints.json` and shows which ones the scenarios visit.
Laravel and Symfony are asked for their own list (`artisan route:list`, `debug:router`, in a throwaway copy); else
the code is read: Laravel route files (prefix groups, resources), Symfony attributes, Slim, plain PHP pages,
hand-made PHP, JavaScript, TypeScript and Rust routers (`$path === '/x'`, `preg_match('#^/x/(\d+)$#', ...)`),
Express-style routers, Flask, FastAPI, Django, axum and actix. Every route is written `METHOD /path/{param}`.

**Code coverage** (PHP, `coverage=True`, which `init` sets): `record` measures which lines of the app's code each
scenario ran, with the [PCOV](https://github.com/krakjoe/pcov) extension (or Xdebug), keeps it as
`golden/<name>/coverage.json` and prints the total; `parityrun coverage` lists every file with the lines no scenario
ran. Files no request loaded count too, with an estimate of their lines. PCOV is found in PHP's `ext/` folder even
when `php.ini` doesn't load it; on Windows, copy `php_pcov.dll` from
[the PECL builds](https://downloads.php.net/~windows/pecl/releases/pcov/) there. Without it, recording works as
before and says so once.

What parity means, then: identical on the recorded scenarios, which visit that many routes and run that share of the
code. A path no scenario takes is not proven.

## A project

A project folder (`parityrun/` after `init`) holds:

```
parityrun_project.py    PROJECT = parityrun.Project(...)
impls.json              the implementations: how to start each one
scenarios/<name>.yaml   what to do
fixtures/               uploads, long LLM replies, files copied into the data folder
golden/<name>/          the recordings (written by `record`, committed), with coverage.json when measured
golden/endpoints.json   every route, for `coverage` and `capture`
```

`impls.json`: a working directory (relative to `repo`), a command with `{port}`, `{repo}` and `{python}` filled in,
and optionally a `cli` command for command-line steps. A program named without a path is looked up on PATH.

```json
{"php":  {"cmd": ["php", "-S", "127.0.0.1:{port}", "-t", "public"]},
 "node": {"cwd": "../port-js", "cmd": ["node", "server.js"]},
 "rust": {"cwd": "rust", "cmd": ["{repo}/target/release/notes", "--port", "{port}"], "cli": ["{repo}/target/release/notes"]}}
```

`parityrun_project.py`:

```python
import pathlib

from parityrun import Project, RunInfo

def env(run: RunInfo) -> dict:
    """What every implementation is started with: the contract."""
    return {"PORT": str(run.port), "DATA_DIR": str(run.data_dir), "LLM_BASE_URL": f"{run.stub_url}/v1",
            "PAYMENTS_API": run.stub_url}

def outside(stub, method, host, rest, query, body, headers):
    """Answers for {stub}/<host>/<path>; None for anything else (a 404)."""
    if host == "api.payments.example" and rest == "/charges":
        return 200, {"id": f"ch_{stub.next_id()}", "status": "paid"}, {}
    return None

PROJECT = Project(
    root=pathlib.Path(__file__).parent,
    reference="php",
    quick=["smoke", "billing"],
    env=env,
    ready="/health",
    html=True,
    redirects=True,
    isolate=True,
    databases=["database/app.sqlite"],
    stub_handler=outside,
)
```

| Setting | Meaning |
|---|---|
| `root`, `repo` | the project folder; what `{repo}` means (default: the folder above `root`) |
| `reference` | the implementation `record` runs |
| `quick` | the scenarios `check --quick` runs; `--slow` runs the rest |
| `env(run)` | the environment on top of the caller's: `run.data_dir`, `run.work`, `run.port`, `run.stub_url`, `run.repo` |
| `ready`, `ready_timeout` | GET this path until it answers 200 (default `/`, 90 s) |
| `shutdown` | e.g. `"POST /shutdown"`; without it, or if it doesn't stop in 20 s, the process is killed |
| `headers` | sent with every request (e.g. a CSRF header) |
| `events` | a server-sent events path a step's `sse: n` listens to |
| `html` | record HTML answers as normalised lines: attribute order, entity spelling, indentation and comments don't count |
| `redirects` | record a 3xx answer's `Location` |
| `isolate` | run each implementation in a fresh copy of its folder; what it creates, changes or deletes there is recorded as `app/<path>` (`isolate_skip`: names not copied, default `.git`) |
| `ignore_files` | path patterns never recorded, e.g. `["storage/logs/*", "*.log"]` |
| `coverage` | measure which lines of a PHP implementation the scenarios run (PCOV or Xdebug) |
| `databases` | SQLite files in the data folder (or, isolated, in the app's folder); the first is recorded as `db` |
| `snapshot(run)`, `setup(run)` | any other database (see Databases); `setup` runs before each start |
| `random_files` | file names random by design: only their size is compared |
| `interleaved_files` | JSON Lines files written by parallel work: line order is free |
| `measured_keys`, `measured_companions` | wall-clock numbers (e.g. `seconds`), and numbers computed from them beside them (e.g. `cost_usd`): `<measured>` |
| `extra_roots` | `{"site": "site"}`: files under `<work>/site` are recorded as `site/...` |
| `stub_handler`, `stub_queues` | answers for outside services; step keys whose lists feed `stub.queues[key]` (e.g. chat updates) |
| `post_process(recording)` | a last change to the normalised recording (e.g. ids of rows parallel work inserts in any order) |
| `labels` | the placeholders for the run's data, work, stub and service addresses |
| `image_ssim` | how alike pictures must be (default 0.98) |
| `work_prefix`, `prog` | the temporary folders' name start; the command name in help text |

A project with its own entry point can skip `--project`: `parityrun.cli.main(project=PROJECT)`.

## Databases

A SQLite file is recorded by naming it in `databases`. For MySQL, MariaDB or PostgreSQL, give each run the same
starting data in `setup` and return the tables from `snapshot`; `parityrun.snapshot.dbapi` reads any DB-API
connection (columns as `[name, type, nullable, default, key]`, every row, decimals and times as text, bytes by hash):

```python
import contextlib, subprocess
import pymysql
from parityrun.snapshot import dbapi

def connect():
    return pymysql.connect(host="127.0.0.1", user="shop", password="shop", database="shop_test")

def setup(run):
    subprocess.run("mysql -ushop -pshop shop_test < fixtures/start.sql", shell=True, check=True)

def snapshot(run):
    with contextlib.closing(connect()) as conn:
        return dbapi(conn, "mysql", skip={"migrations"})
```

## Scenarios

```yaml
description: Sign in, add a note, list it, a refusal.
env: {FEATURE_X: "1"}                 # on top of env(run); {stub} {repo} {python} {data} {fake_claude} are filled in
files: {settings.json: settings.json} # fixtures copied into the data folder before the start
steps:
  - call: GET /login
    capture: {csrf: 'name="_token" value="([^"]+)"'}
  - call: POST /login
    form: {_token: "{{csrf}}", email: ann@example.com, password: secret}
    follow: true
  - call: POST /notes
    id: made                          # later steps use {{made.id}}
    body: {text: "Buy oat milk"}
    llm: ["Errands"]                  # replies the LLM stub gives, in order; {$file: reply.json}; {$http: 429}
  - call: GET /jobs/{{made.job}}
    until: {path: status, in: [done, failed]}   # poll background work; the last answer is compared
    timeout: 120
  - cli: "export --format md"         # the implementation's command line, next to the running service
```

| Key | Meaning |
|---|---|
| `call: METHOD /path` | the request; `{{name[0].id}}` takes a value from an earlier step's JSON or a capture |
| `id` | names this step's JSON for later steps |
| `body` | JSON; a whole `"{{x}}"` keeps its type; `$merge: "{{x}}"` sends `x` back with keys changed |
| `form` | a web form (urlencoded); `true` is a ticked checkbox (`on`), `false` leaves the field out |
| `upload` | `{field: fixture}`: files sent with `form` as multipart |
| `raw: file` | sends `fixtures/<file>` as the body |
| `capture` | `{name: regex}` over the answer's text, or `{name: {header: location, regex: ...}}`: `{{name}}` in later steps; values of 8+ characters (tokens) are recorded as `<name>` |
| `follow` | follow redirects to the page they end on (recorded as `redirects`) |
| `headers` | request headers (a 206 answer is compared by its range and length) |
| `llm` | replies for the LLM stub; they wait until asked for, so a background task may take them later |
| `until`, `timeout`, `every` | repeat until `{path, equals / in / not / length}` holds |
| `sse: n` | also listen to the project's events and compare the first n |
| `ignore` | dotted paths (`*` = any key) that depend on the machine, not the code |
| `unordered` | compare a list response without its order |
| `cli` | run the command line instead of a request: exit code, stdout and stderr are compared |

Quote text with a comma inside `{...}`: YAML splits it otherwise, and the loader refuses such files.

`{fake_claude}` in a scenario's `env` becomes a real executable that stands in for the `claude` command line
(`CLAUDE_BIN: "{fake_claude}"`): its arguments and stdin are recorded like any LLM request, and it answers with a
`--output-format json` envelope.

## Commands

```
parityrun init [PATH] [--force] [--no-start]
                                           set parityrun up in an existing app's folder (writes parityrun/)
parityrun capture [NAME] [--port 8000] [--impl NAME] [--no-browser]
                                           use the app in a browser; what you do becomes scenarios/NAME.yaml
parityrun routes                           read the reference's routes from its code into golden/endpoints.json
parityrun record [scenario ...]            run the reference twice per scenario, save golden/<name>/
parityrun check [--quick|--slow] [--impl NAME] [scenario ...]
                                           compare; prints each failing comparison and the parity %, writes
                                           out/report-<impl>.json and keeps the run's pictures in out/<impl>/<name>/
parityrun coverage                         the routes no scenario calls, and the PHP lines no scenario ran
```

Every command takes `--project FILE` first; without it, `parityrun_project.py` here or `parityrun/parityrun_project.py`.
`check` exits 0 only when every comparison passes. Review the diff of `golden/` before committing a new recording:
from then on a recording is only as right as the run that made it.

## Examples

`examples/notes` is a tiny notes service that titles each note with an LLM:

```
parityrun --project examples/notes/parityrun_project.py record      # golden/notes/recording.json
parityrun --project examples/notes/parityrun_project.py check       # 100 % parity against itself
```

Change the system prompt in `notes_service.py` and run `check` again: the `prompts` comparison fails and shows the
difference.

`examples/guestbook` is a PHP site (forms, a CSRF cookie, redirects, HTML pages) with ports to JavaScript, TypeScript
and Rust that match it to 100 %:

```
parityrun --project examples/guestbook/parityrun_project.py record              # php
parityrun --project examples/guestbook/parityrun_project.py check --impl js     # node
parityrun --project examples/guestbook/parityrun_project.py check --impl ts     # node 23.6+
parityrun --project examples/guestbook/parityrun_project.py check --impl rust   # cargo build --release in rust/ first
parityrun --project examples/guestbook/parityrun_project.py capture             # browse it yourself
```
