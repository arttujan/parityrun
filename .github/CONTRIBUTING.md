# Contributing to parityrun

Thanks for helping. Bug reports, ideas and pull requests are all welcome. Questions go to
[Discussions](https://github.com/arttujan/parityrun/discussions), bugs and ideas to
[Issues](https://github.com/arttujan/parityrun/issues/new/choose). A security problem goes privately, as
[SECURITY.md](SECURITY.md) says, never in a public issue.

## How a change gets in

1. Fork the repository and make a branch in your fork.
2. Make the change, with a test that shows it (see below), and open a pull request to `main`.
3. CI runs the lint and the tests on Linux and Windows, and CodeQL looks at the code. A first-time contributor's
   CI waits for the maintainer to start it.
4. The maintainer reviews it. When CI is green and the review approves, the pull request is squashed into one commit
   on `main`, with the pull request's title as its message.

Nobody pushes to `main` directly, and only the maintainer makes releases.

## Set up

Python 3.10 or newer:

```
git clone https://github.com/<you>/parityrun
cd parityrun
python -m venv .venv
.venv/Scripts/activate            # Windows; on macOS and Linux: source .venv/bin/activate
pip install -e ".[dev]"
pre-commit install
```

`pre-commit install` checks every commit as CI will: ruff (every rule), ruff's formatter and pyright (strict), and
that the commit message follows [Conventional Commits](https://www.conventionalcommits.org/). Run them all with
`pre-commit run --all-files`.

## Tests

```
pytest
```

The end-to-end tests run real apps, so they need their tools on PATH, and skip (saying why) without them: `php`
(8.2 or newer; with PCOV or Xdebug for the coverage tests), `node` (23.6 or newer, for TypeScript) and `cargo`. CI
has them all. The MySQL, PostgreSQL and real AI tool tests run only when asked; their files say how.

## What a change has to keep

- **Recordings already made stay valid.** tests/test_regression.py freezes what parityrun records and how it saves
  it. If it fails, the change altered default behaviour: make the new behaviour opt-in (a `Project` setting or a
  scenario key) instead of changing the test.
- **The examples keep passing.** Every port in examples/ must match its reference at 100 % parity.
- **Strict types and lint, no blanket exceptions.** If a rule really doesn't fit a line, use a `# noqa: RULE` (or
  `# pyright: ignore[rule]`) on that line with the reason beside it.
- **New behaviour comes with a test** that fails without it.

## Pull request titles

The title becomes the commit message on `main`, so it follows Conventional Commits, and CI checks it:

```
feat: record WebSocket messages
fix(capture): keep a form's repeated fields in order
docs: explain isolated runs
```

Types: `feat`, `fix`, `docs`, `refactor`, `perf`, `test`, `build`, `ci`, `chore`, `style`, `revert`. A change that
breaks existing projects or recordings gets a `!` (`feat!: ...`) and says what breaks in the description.

## Documentation

The documentation is the [wiki](https://github.com/arttujan/parityrun/wiki). If your change needs it updated, say
what in the pull request; the maintainer updates the wiki when it is merged.

## License

parityrun is MIT licensed. What you contribute is under the same license.
