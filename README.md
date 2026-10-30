# parityrun

Prove that a rewrite of your app behaves exactly like the original. parityrun records what your app does (every
page, form answer, redirect, database row and file it writes) and then checks a new version against that recording:
a port from PHP to JavaScript, TypeScript or Rust, a framework upgrade, or simply the next release. With your own AI
tool, it can do the port too, turn by turn, until the new version matches.

## Install

```
pip install git+https://github.com/arttujan/parityrun@v0.4.0
```

Python 3.10 or newer, and whatever your app itself needs to run (for a PHP app: `php` on PATH).

## Use it

In your app's folder:

```
parityrun init        # finds how to start the app and every route in its code
parityrun capture     # opens the app in your browser: use it as people do, then press Finish
parityrun record      # records what the app does
```

`capture` shows on every page how many of the app's routes you have visited, and `http://127.0.0.1:8000/__parityrun`
lists the ones still to go. Run it again until the list is done. Your app's own files are never changed.

When the new version is ready, tell parityrun how to start it in `parityrun/impls.json`:

```json
"node": {"cwd": "../my-app-js", "cmd": ["node", "server.js"]}
```

(it gets its port in the `PORT` environment variable) and check it:

```
parityrun check --impl node      # every difference, and the parity %; exits 0 only at 100 %
parityrun coverage               # the routes and lines of code no scenario has run yet
```

## Let your AI tool port it

```
parityrun port --to typescript      # or javascript, rust
```

parityrun writes the new app's starting point beside yours (`../my-app-ts`), adds it to `impls.json` and gives your AI
tool the task in `PORTING.md`. Then it goes turn by turn: the tool works, parityrun checks, and the differences of one
scenario at a time become the next turn's task, until parity is 100 %. A scenario that stays red is reported, never
hidden. By default the tool is [Claude Code](https://claude.com/claude-code) (`claude` on PATH, your own account);
for another one, give its command: `--agent "yourtool --message-file {prompt_file}"`. The tool may not change your app
or the recordings: parityrun puts its own folder back after every turn and stops if your app changes.

## What "the same" means

Pages are compared as a browser reads them (indentation and attribute order don't count, text and links do), along
with status codes, redirects, database rows and written files; with the built-in stubs, also every prompt sent to an
LLM and every call to an outside service. Random ids, tokens and times are normalised. A check proves the new version identical on the recorded scenarios, which visit the routes and
(for PHP) run the share of the code that `parityrun coverage` shows; a path no scenario takes is not proven, so
capture until coverage is where you want it.

`init` recognises PHP (plain, Laravel, Symfony, WordPress), Node.js, Python (Django, Flask, FastAPI) and Rust apps.
Everything else (settings, scenario files, databases, LLM and outside-service stubs, examples):
[docs/reference.md](docs/reference.md).

## Licence

MIT. Copyright (c) 2026 Arttu Jantunen.
