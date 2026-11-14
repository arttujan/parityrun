<!-- The title becomes the commit message on main: Conventional Commits, e.g. "fix(capture): keep repeated fields". -->

## What and why

<!-- What changes, and the problem it solves. "Fixes #123" closes the issue when this is merged. -->

## How it is tested

<!-- The test that shows it, and what you ran (pytest, pre-commit run --all-files). -->

## Checklist

- [ ] A test fails without this change and passes with it
- [ ] `pytest` and `pre-commit run --all-files` pass
- [ ] Recordings made before still check the same (tests/test_regression.py untouched), or the title has a `!` and
      this says what breaks
- [ ] What the wiki should say about it, if anything, is written here
