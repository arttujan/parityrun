# Security Policy

## Supported Versions

parityrun is before 1.0: security fixes go into the newest release only, as a new patch or minor version on PyPI.
Update with `pip install -U parityrun` (or run the installer again).

| Version                  | Supported          |
| ------------------------ | ------------------ |
| newest release (0.6.x)   | :white_check_mark: |
| older releases           | :x:                |

## Reporting a Vulnerability

Report it privately, not in a public issue: on GitHub, open the repository's **Security** tab and choose
**Report a vulnerability**. Say what you found, how to reproduce it (a project file and a scenario help), the
version and the operating system.

What happens next:

- You get an answer within 7 days that the report has arrived, and then news at least every 14 days until it is
  settled.
- If it is a vulnerability, it is fixed in a new release, and a GitHub security advisory says what it was, which
  versions it affects and who found it (unless you'd rather not be named). Please keep it private until the
  release is out; the advisory says when it is.
- If it isn't, you get the reason, and the report is closed.

## What counts

parityrun runs code you give it, by design: a project file is Python, and impls.json, `build` and `cli` are
commands it starts. Opening a project you don't trust is running its code, as running its tests would be; that
alone isn't a vulnerability. These are:

- Anything outside your machine reaching what parityrun itself serves while it runs: the stub for LLM and outside
  services and the `capture` proxy listen on 127.0.0.1 only. (Where your app listens is up to its command.)
- Recordings (golden/) or reports (out/) keeping something they shouldn't, such as a request's headers, which the
  stub never records.
- `parityrun port` letting the AI tool change the parityrun folder or the original app without the loop putting
  it back or stopping, or giving Claude Code tools beyond its documented list.
- A scenario, recording or recorded answer (not a project file or impls.json) making parityrun run commands or
  write files outside the run's own folders.
- The installers (install.ps1, install.sh) or the published package doing anything but installing parityrun.
