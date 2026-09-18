#!/usr/bin/env bash
# Download the pinned MediaMTX release for the Raspberry Pi (arm64) into this
# repo directory, so the systemd unit can run ./mediamtx. Run this ON THE PI.
#
# Usage: ./scripts/install-mediamtx.sh [version]
set -euo pipefail

VERSION="${1:-v1.16.1}"
ARCH="$(uname -m)"
case "$ARCH" in
  aarch64|arm64) MTX_ARCH="linux_arm64" ;;
  armv7l)        MTX_ARCH="linux_armv7" ;;
  x86_64|amd64)  MTX_ARCH="linux_amd64" ;;
  *) echo "unsupported arch: $ARCH" >&2; exit 1 ;;
esac

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARBALL="mediamtx_${VERSION}_${MTX_ARCH}.tar.gz"
URL="https://github.com/bluenviron/mediamtx/releases/download/${VERSION}/${TARBALL}"

echo "Downloading MediaMTX ${VERSION} (${MTX_ARCH})..."
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
curl -fsSL "$URL" -o "$TMP/$TARBALL"
tar -xzf "$TMP/$TARBALL" -C "$TMP"
install -m 0755 "$TMP/mediamtx" "$REPO_DIR/mediamtx"

echo "Installed: $REPO_DIR/mediamtx"
"$REPO_DIR/mediamtx" --version || true
echo "Setup completed."
