# parityrun

Golden-recording parity tests for HTTP services. You record what a reference implementation does for a set of
scenarios: every response, every LLM prompt, every call to an outside service, every database row and every file
it writes. Then you check another implementation against those recordings: a rewrite in another language, a port,
or simply the next version of the same code.

It was built to move a production service from Python to Rust without changing a single observable byte, and it
keeps checking that service on every change since.

- **Black box.** An implementation is a command. parityrun starts it with a fresh data folder and talks to it only
  over HTTP and its command line, so the language and framework don't matter.
- **LLM calls are part of the contract.** A built-in stub speaks the OpenAI chat completions API, the Anthropic
  Messages API and the `claude` command line. Each step queues the replies its calls need, and the requests
  themselves (model, messages, schema) are recorded and compared, so a changed prompt fails the check.
- **Outside services are stubbed, and recorded.** Point your service's API base at the stub and answer for any host
  with a small handler; every call (method, path, query, body) is compared.
- **Noise is normalised, not ignored.** Ids are numbered by first appearance, times, dates and unix times become
  placeholders, the run's own paths and ports become labels. Everything else must match exactly.
- **Pictures and videos are compared by eye.** Images by structural similarity (SSIM), videos by duration, size,
  sound and five frames. SQLite databases by schema and rows (row order is free).
- **No flaky goldens.** `record` runs every scenario twice and refuses to save one whose runs differ.

## Install

```
pip install git+https://github.com/arttujan/parityrun@v0.2.0
```

Python 3.10+. Videos need `ffmpeg` and `ffprobe` on PATH (or the `FFMPEG` / `FFPROBE` variables).

## Start with your app

In your app's folder (PHP, Laravel, Symfony, WordPress, Node.js, Django, Flask, FastAPI or Rust):

```
parityrun init        # finds how to start the app and every route in its code, writes parityrun/
parityrun capture     # opens the app in your browser: use it, every route you visit is ticked off
parityrun record      # records what the app does now into parityrun/golden/
```

`init` starts the app in a throwaway copy, reads its home page and its code, and writes `parityrun/`: how to start
the app (`impls.json`), the settings (`parityrun_project.py`, with notes on anything it couldn't decide, such as a
MySQL database), the routes found in the code (`golden/endpoints.json`) and a first scenario (`scenarios/smoke.yaml`:
the home page and the pages it links to). Every run uses a fresh copy of the app's folder, so the app's own files
are never changed and whatever it writes there is recorded.

`capture` starts the app behind a proxy at http://127.0.0.1:8000. Use the site as people do: open the pages, send the
forms. Each page shows how many of the app's routes have been visited, and http://127.0.0.1:8000/__parityrun lists
the ones still to go. Press Finish there: what you did becomes `scenarios/visit.yaml` (form tokens and new ids are
captured so it replays on a fresh app), and it is recorded twice to prove it replays. Run `capture` again until every
route is visited. `parityrun routes` shows the list in the terminal. Then check another version against the
recordings with `parityrun check --impl NAME`.

Routes are read from Laravel (`artisan route:list`), Symfony (`debug:router`), Slim, plain PHP pages and hand-made
PHP routers, Express-style JavaScript and TypeScript routers, Flask, FastAPI, Django, axum and actix.

## Try it

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
parityrun --project examples/guestbook/parityrun_project.py record              # needs php on PATH
parityrun --project examples/guestbook/parityrun_project.py check --impl js     # node
parityrun --project examples/guestbook/parityrun_project.py check --impl ts     # node 23.6+
parityrun --project examples/guestbook/parityrun_project.py check --impl rust   # cargo build --release in rust/ first
```

## A project

A project folder holds:

```
parityrun_project.py    PROJECT = parityrun.Project(...)
impls.json              the implementations: how to start each one
scenarios/<name>.yaml   what to do
fixtures/               uploads, long LLM replies, files copied into the data folder
golden/<name>/          the recordings (written by `record`, committed)
golden/endpoints.json   optional: every endpoint, for `coverage`
```

`impls.json`: a working directory (relative to `repo`), a command with `{port}`, `{repo}` and `{python}` filled in,
and optionally a `cli` command for command-line steps.

```json
{"python": {"cmd": ["{python}", "notes_service.py", "--port", "{port}"]},
 "rust":   {"cwd": "rust", "cmd": ["{repo}/target/release/notes", "--port", "{port}"], "cli": ["{repo}/target/release/notes"]}}
```

`parityrun_project.py`:

```python
import pathlib

from parityrun import Project, RunInfo

def env(run: RunInfo) -> dict:
    """What every implementation is started with: the contract."""
    return {"DATA_DIR": str(run.data_dir), "LLM_BASE_URL": f"{run.stub_url}/v1", "PAYMENTS_API": run.stub_url}

def outside(stub, method, host, rest, query, body, headers):
    """Answers for {stub}/<host>/<path>; None for anything else (a 404)."""
    if host == "api.payments.example" and rest == "/charges":
        return 200, {"id": f"ch_{stub.next_id()}", "status": "paid"}, {}
    return None

PROJECT = Project(
    root=pathlib.Path(__file__).parent,
    reference="python",
    quick=["notes", "billing"],
    env=env,
    ready="/health",
    shutdown="POST /shutdown",
    databases=["app.db"],
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
| `databases` | SQLite files in the data folder; the first is recorded as `db` |
| `snapshot(run)`, `setup(run)` | any other database: return `{"schema", "rows"}` to record as `db` (`parityrun.snapshot.dbapi(conn, "mysql" / "postgres" / "sqlite")` reads one); `setup` runs before each start, e.g. to empty it |
| `html` | record HTML answers as normalised lines: attribute order, entity spelling, indentation and comments don't count |
| `redirects` | record a 3xx answer's `Location` |
| `isolate` | run each implementation in a fresh copy of its folder; what it creates, changes or deletes there is recorded as `app/<path>` (`isolate_skip`: names not copied, default `.git`) |
| `ignore_files` | path patterns never recorded, e.g. `["storage/logs/*", "*.log"]` |
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

## Scenarios

```yaml
description: Add a note, list it, a refusal.
env: {FEATURE_X: "1"}                 # on top of env(run); {stub} {repo} {python} {data} {fake_claude} are filled in
files: {settings.json: settings.json} # fixtures copied into the data folder before the start
steps:
  - call: POST /notes
    id: made                          # later steps use {{made.id}}
    body: {text: "Buy oat milk"}
    llm: ["Errands"]                  # replies the LLM stub gives, in order; {$file: reply.json}; {$http: 429}
  - call: GET /notes/{{made.id}}
  - call: GET /jobs/{{made.job}}
    until: {path: status, in: [done, failed]}   # poll background work; the last answer is compared
    timeout: 120
  - cli: "export --format md"         # the implementation's command line, next to the running service
```

| Key | Meaning |
|---|---|
| `call: METHOD /path` | the request; `{{name[0].id}}` takes a value from an earlier step's JSON |
| `id` | names this step's JSON for later steps |
| `body` | JSON; a whole `"{{x}}"` keeps its type; `$merge: "{{x}}"` sends `x` back with keys changed |
| `raw: file` | sends `fixtures/<file>` as the body |
| `form` | a web form (urlencoded); `true` is a ticked checkbox (`on`), `false` leaves the field out |
| `upload` | `{field: fixture}`: files sent with `form` as multipart |
| `capture` | `{name: regex}` over the answer's text, or `{name: {header: location, regex: ...}}`: `{{name}}` in later steps; values of 8+ characters (CSRF tokens) are recorded as `<name>` |
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
parityrun record [scenario ...]            run the reference twice per scenario, save golden/<name>/
parityrun check [--quick|--slow] [--impl NAME] [scenario ...]
                                           compare; prints each failing comparison and the parity %, writes
                                           out/report-<impl>.json and keeps the run's pictures in out/<impl>/<name>/
parityrun coverage                         the endpoints in golden/endpoints.json no scenario calls
parityrun init [PATH] [--force] [--no-start]
                                           set parityrun up in an existing app's folder (writes parityrun/)
parityrun routes                           read the reference's routes from its code into golden/endpoints.json
parityrun capture [NAME] [--port 8000] [--impl NAME] [--no-browser]
                                           use the app in a browser; what you do becomes scenarios/NAME.yaml
```

`check` exits 0 only when every comparison passes. Review the diff of `golden/` before committing a new recording:
from then on a recording is only as right as the run that made it.

## Licence

MIT. Copyright (c) 2026 Arttu Jantunen.
