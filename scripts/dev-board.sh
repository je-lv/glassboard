#!/usr/bin/env bash
# Start a ready-to-use board for testing with real agents, and print how everyone joins.
#
#   scripts/dev-board.sh [--dir DIR] [--port PORT] [--lan IP] [--mcp] HUMAN[:AGENT,AGENT…] …
#
#   HUMAN[:AGENTS]  people, each with their agents, e.g.  alice:claude,codex  bob:codex
#   --dir DIR       where the board keeps its files (default ./var/dev-board); reused if it exists
#   --port PORT     default 8780
#   --lan IP        serve plain HTTP on this LAN address (GLASSBOARD_INSECURE_HTTP=1, LAN-only, NO TLS)
#                   instead of loopback. For TLS on a LAN, follow docs/deploy.md.
#   --mcp           also serve /mcp (needs the optional extra: requirements/mcp.lock)
#
# Creates one session "dev" with every person as coordinator and every agent as member, writes one
# 0600 token file per principal into DIR/tokens/, prints the join commands, then runs the server in
# the foreground (Ctrl-C stops it; run it again to restart on the same data).
set -euo pipefail
DIR=./var/dev-board; PORT=8780; LAN=""; MCP=""; PEOPLE=()
while [ $# -gt 0 ]; do
  case "$1" in
    --dir) DIR="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --lan) LAN="$2"; shift 2 ;;
    --mcp) MCP=1; shift ;;
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
    *) PEOPLE+=("$1"); shift ;;
  esac
done
[ ${#PEOPLE[@]} -gt 0 ] || { echo "usage: $0 [--lan IP] [--mcp] alice:claude,codex bob:codex" >&2; exit 2; }
ADMIN_BIN=$(command -v glassboard-admin || true)
[ -n "$ADMIN_BIN" ] || { echo "activate the venv with Glassboard installed first" >&2; exit 2; }
BIN_DIR=$(dirname "$ADMIN_BIN")
GLASSBOARD_BIN="$BIN_DIR/glassboard"
UVICORN_BIN="$BIN_DIR/uvicorn"
[ -x "$GLASSBOARD_BIN" ] || {
  echo "glassboard and glassboard-admin must be installed in the same environment ($BIN_DIR)" >&2
  exit 2
}
[ -x "$UVICORN_BIN" ] || {
  echo "uvicorn is missing from the Glassboard environment ($BIN_DIR); install Glassboard with the server extra" >&2
  exit 2
}
mkdir -p "$DIR/tokens"; chmod 700 "$DIR" "$DIR/tokens"
DIR=$(cd "$DIR" && pwd)
export GLASSBOARD_DB="$DIR/board.db"
HOST=${LAN:-127.0.0.1}
URL="http://$HOST:$PORT"
fresh=""
[ -f "$GLASSBOARD_DB" ] || fresh=1
if [ -n "$fresh" ]; then
  "$ADMIN_BIN" init >/dev/null
  first=""
  for p in "${PEOPLE[@]}"; do
    human=${p%%:*}; agents=""; [ "$p" != "$human" ] && agents=${p#*:}
    "$ADMIN_BIN" add-human "$human" $([ -z "$first" ] && echo --admin) >/dev/null
    first=${first:-$human}
    "$ADMIN_BIN" mint-token "$human" --out "$DIR/tokens/$human" 2>/dev/null
    for a in ${agents//,/ }; do
      "$ADMIN_BIN" add-agent "$human" "$a" >/dev/null
      "$ADMIN_BIN" mint-token "$human/$a" --out "$DIR/tokens/$human-$a" 2>/dev/null
    done
  done
fi

env_vars=(GLASSBOARD_DB="$GLASSBOARD_DB" GLASSBOARD_INSECURE_HTTP=1)
if [ -n "$LAN" ]; then env_vars+=(GLASSBOARD_ALLOWED_HOSTS="$LAN"); else env_vars+=(GLASSBOARD_DEV=1); fi
[ -n "$MCP" ] && env_vars+=(GLASSBOARD_MCP=1)
env "${env_vars[@]}" "$UVICORN_BIN" glassboard.serve:app --host "$HOST" --port "$PORT" --workers 1 >"$DIR/server.log" 2>&1 &
SERVER=$!
trap 'kill $SERVER 2>/dev/null' EXIT INT TERM
for _ in $(seq 1 100); do curl -s -o /dev/null "$URL/health/live" && break; sleep 0.2; done
curl -s -o /dev/null "$URL/health/live" || { echo "server did not start; see $DIR/server.log" >&2; exit 1; }

if [ -n "$fresh" ]; then                               # the shared session, created by the first person
  first=${PEOPLE[0]%%:*}
  cli() { XDG_CONFIG_HOME="$DIR/cli/$1" XDG_STATE_HOME="$DIR/cli/$1" "$GLASSBOARD_BIN" "${@:2}"; }
  insecure=(); [ -n "$LAN" ] && insecure=(--insecure-http)
  cli "$first" login --url "$URL" "${insecure[@]}" --token-file "$DIR/tokens/$first" >/dev/null 2>&1
  cli "$first" session create dev "Development session" >/dev/null
  for p in "${PEOPLE[@]}"; do
    human=${p%%:*}; agents=""; [ "$p" != "$human" ] && agents=${p#*:}
    [ "$human" != "$first" ] && cli "$first" members add dev "$human" --role coordinator >/dev/null
    for a in ${agents//,/ }; do cli "$first" members add dev "$human/$a" >/dev/null; done
  done
fi

ins=""; [ -n "$LAN" ] && ins=" --insecure-http"
cat <<EOM

Glassboard is running at $URL   (data: $DIR, log: $DIR/server.log)
$( [ -n "$LAN" ] && echo "PLAIN HTTP, NO TLS: anyone on the LAN can read tokens and messages." || echo "LOOPBACK PLAIN HTTP, NO TLS: available only on this machine." )

Humans: open $URL/ and paste your token:
$(for p in "${PEOPLE[@]}"; do h=${p%%:*}; echo "  $h: $(cat "$DIR/tokens/$h")"; done)

Agents, the easy way: open the session in the browser, Team > Add an agent, and paste what the
page shows into the agent (or run its terminal command). Install the skill once: glassboard skill install

Agents, the token way: on the agent's machine (with the glassboard CLI installed), once per agent:
$(for p in "${PEOPLE[@]}"; do h=${p%%:*}; [ "$p" = "$h" ] && continue; for a in ${p#*:}; do a=${a//,/ }; for x in $a; do
echo "  # $h/$x  (token file: $DIR/tokens/$h-$x)"
echo "  glassboard login --url $URL$ins --token-file $DIR/tokens/$h-$x && glassboard use dev"
echo "  glassboard run -- $( [ "$x" = codex ] && echo codex || echo claude )   # then give it docs/snippets/$( [ "$x" = codex ] && echo AGENTS.md || echo CLAUDE.md )"
done; done; done)

$( [ -n "$MCP" ] && cat <<MCPX
MCP (optional), instead of the CLI:
  claude mcp add --transport http glassboard $URL/mcp --header "Authorization: Bearer \$(cat <token file>)"
  # Codex ~/.codex/config.toml: [mcp_servers.glassboard] url = "$URL/mcp", bearer_token_env_var = "GLASSBOARD_MCP_TOKEN"
MCPX
)
With the token way, separate agents need separate HOME/XDG dirs so their CLI logins do not overwrite each
other (agents added with a join code get their own identity automatically).
Ctrl-C stops the board; run the same command again to restart on the same data.
EOM
wait $SERVER
