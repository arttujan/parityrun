# parityrun

Prove that a rewrite of your app behaves exactly like the original. parityrun records what your app does (every
page, form answer, redirect, database row and file it writes) and then checks a new version against that recording:
a port from PHP to JavaScript, TypeScript or Rust, a framework upgrade, or simply the next release.

## Install

```
pip install git+https://github.com/arttujan/parityrun@v0.3.0
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
