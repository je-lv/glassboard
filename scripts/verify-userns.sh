#!/usr/bin/env bash
# Run scripts/e2e-verify.sh without root and without editing the real /etc/hosts.
# It enters an unprivileged user + mount namespace (`unshare -rm`) and bind-mounts a private hosts
# file that maps board.lan -> 127.0.0.1. Networking is the host's (no -n), so TLS, the proxy and the
# app are exercised for real; only name resolution differs. Nothing outside the namespace changes.
# Usage: CADDY=/path/to/caddy [GLASSBOARD_APP_PORT=18080] scripts/verify-userns.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
{ cat /etc/hosts; echo "127.0.0.1 board.lan"; } > "$TMP/hosts"
unshare -rm bash -c 'mount --bind "$1" /etc/hosts && exec "$2/scripts/e2e-verify.sh"' _ "$TMP/hosts" "$ROOT"
