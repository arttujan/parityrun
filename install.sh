#!/bin/sh
# Installs parityrun on macOS and Linux, with nothing else needed first. In a terminal:
#
#   curl -LsSf https://raw.githubusercontent.com/arttujan/parityrun/main/install.sh | sh
#
# uv (https://docs.astral.sh/uv/) installs parityrun as a command of its own, with its own Python when none fits.
# Run it again to update to the version below. PARITYRUN_SOURCE installs from elsewhere: a folder, git+https://...
set -eu
version="0.6.0"
source="${PARITYRUN_SOURCE:-parityrun==$version}"
if ! command -v uv >/dev/null 2>&1; then
    echo "Installing uv, which installs parityrun ..."
    if command -v curl >/dev/null 2>&1; then
        curl -LsSf https://astral.sh/uv/install.sh | sh
    else
        wget -qO- https://astral.sh/uv/install.sh | sh
    fi
    PATH="$HOME/.local/bin:$PATH"
fi
uv tool install --force "$source"
uv tool update-shell >/dev/null 2>&1 || true
PATH="$(uv tool dir --bin):$PATH"
parityrun --version
echo
echo "Done. In your app's folder, run:  parityrun"
echo "(If the command isn't found, open a new terminal first.)"
