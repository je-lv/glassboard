#!/usr/bin/env bash
# Disposable local human-and-agent workflow. Run after installing Glassboard.
# Optional: GLASSBOARD_DEMO_DIR=/path/to/new/directory bash examples/coordination-demo.sh
set -euo pipefail
umask 077

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEMO_DIR="${GLASSBOARD_DEMO_DIR:-$ROOT/var/coordination-demo}"
if [ -e "$DEMO_DIR" ]; then
  echo "demo directory already exists: $DEMO_DIR (choose a new GLASSBOARD_DEMO_DIR)" >&2
  exit 2
fi
mkdir -p "$DEMO_DIR"
cd "$DEMO_DIR"
export GLASSBOARD_DB="$PWD/board.db" GLASSBOARD_DEV=1

# The operator creates the board, a human and a token.
glassboard-admin init
glassboard-admin add-human alice --admin
glassboard-admin mint-token alice --out alice.token

# The temporary server answers only on loopback and stops when this script exits.
if curl -fs -o /dev/null http://127.0.0.1:8000/health/live; then
  echo "port 8000 already serves a Glassboard instance; stop it before running this demo" >&2
  exit 2
fi
uvicorn glassboard.serve:app --host 127.0.0.1 --port 8000 --workers 1 > server.log 2>&1 &
SERVER=$!
trap 'kill "$SERVER" 2>/dev/null || true' EXIT
ready=0
for _ in {1..50}; do
  if curl -fs -o /dev/null http://127.0.0.1:8000/health/live; then ready=1; break; fi
  if ! kill -0 "$SERVER" 2>/dev/null; then break; fi
  sleep 0.2
done
if [ "$ready" -ne 1 ]; then
  echo "demo server did not become ready; see $DEMO_DIR/server.log" >&2
  exit 1
fi

# Keep participant configurations separate, as two real clients would be.
alice()  { XDG_CONFIG_HOME=$PWD/alice  XDG_STATE_HOME=$PWD/alice  glassboard "$@"; }
helper() { XDG_CONFIG_HOME=$PWD/helper XDG_STATE_HOME=$PWD/helper glassboard "$@"; }

alice login --url http://127.0.0.1:8000 --token-file alice.token
alice agent add helper
alice token new alice/helper --out helper.token
helper login --url http://127.0.0.1:8000 --token-file helper.token

alice session create faq "Write the project FAQ"
alice members add faq alice/helper
alice use faq
helper use faq

Q=$(alice post question "Which three questions should the FAQ answer first?" --to alice/helper | sed -n 's/^posted #\([0-9]*\).*/\1/p')
if [ -z "$Q" ]; then echo "could not read the posted question number" >&2; exit 1; fi
helper sync
helper post answer "Install, joining a session, recovering after a restore." --re "$Q"
helper sync --ack

alice task create "Draft the FAQ" --scope "docs/faq.md, three questions" --deliverable "a first draft" --assignee alice/helper
helper task claim T1
helper task note T1 "Two of three answers drafted."
helper task submit T1 "Draft ready for review."
alice task accept T1

alice board
alice history
printf '\nDemo files: %s\n' "$DEMO_DIR"
