#!/usr/bin/env bash
# Create (or update) the project virtualenv and install the daemon plus dev
# tools into it. Safe to re-run: an existing .venv is reused.
#
# Usage: ./scripts/install.sh            (or: make install)
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$REPO_DIR/.venv"
PYTHON="${PYTHON:-python3}"

if [[ ! -x "$VENV/bin/python" ]]; then
  echo "Creating virtualenv at $VENV"
  "$PYTHON" -m venv "$VENV"
else
  echo "Reusing virtualenv at $VENV"
fi

"$VENV/bin/python" -m pip install --upgrade pip
# requirements.txt carries the --extra-index-url for the Lattice SDK packages
# (served from Anduril's schema registry, not PyPI) and the exact pins.
"$VENV/bin/python" -m pip install -r "$REPO_DIR/requirements.txt"
# Editable install of the daemon (console script lattice-cam) plus
# the dev tools: pytest, ruff, mypy.
"$VENV/bin/python" -m pip install -e "$REPO_DIR[dev]"

cat <<MSG

Installed into $VENV

Next steps:
  1. cp .env.example .env            # then fill in LATTICE_ENDPOINT, the token, camera position...
  2. ./scripts/install-mediamtx.sh    # downloads the MediaMTX binary into the repo (on the Pi)
  3. Edit the placeholders (project path, User=/Group=, account name) in
       deploy/systemd/lattice-cam.service
       deploy/systemd/mediamtx-srt.service
       deploy/sudoers.d/lattice-cam
     then: make install-units        # copies units + sudoers rule (uses sudo)
  4. sudo systemctl enable --now lattice-cam
     (do NOT enable mediamtx-srt; the daemon starts it on a Lattice Start task)

Checks: make check   (ruff, mypy, pytest)
MSG
