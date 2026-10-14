#!/usr/bin/env bash
# Launch one agent client as its own OS user in a systemd transient sandbox.
# Usage: sudo scripts/run-agent-isolated.sh USER STATE_DIR WORKTREE -- COMMAND [ARG...]
set -euo pipefail

if [ "$#" -lt 5 ] || [ "$4" != "--" ]; then
  echo "usage: sudo $0 USER STATE_DIR WORKTREE -- COMMAND [ARG...]" >&2
  exit 2
fi
if [ "$(id -u)" -ne 0 ]; then
  echo "run-agent-isolated.sh must run as root (it drops to the named agent user)" >&2
  exit 2
fi

agent_user=$1
state_dir=$2
worktree=$3
shift 4

case "$agent_user" in root|glassboard) echo "refusing privileged or Glassboard service user: $agent_user" >&2; exit 2;; esac
case "$state_dir:$worktree" in /*:/*) ;; *) echo "STATE_DIR and WORKTREE must be absolute paths" >&2; exit 2;; esac
id "$agent_user" >/dev/null
agent_uid=$(id -u "$agent_user")
agent_gid=$(id -g "$agent_user")
for path in "$state_dir" "$worktree"; do
  [ -d "$path" ] || { echo "not a directory: $path" >&2; exit 2; }
  [ "$(stat -c %u "$path")" = "$agent_uid" ] || { echo "$path is not owned by $agent_user" >&2; exit 2; }
done

exec systemd-run --quiet --wait --collect --pty \
  --uid="$agent_uid" --gid="$agent_gid" --working-directory="$worktree" \
  --setenv="HOME=$state_dir" --setenv="XDG_CONFIG_HOME=$state_dir/config" --setenv="XDG_STATE_HOME=$state_dir/state" \
  --property=NoNewPrivileges=yes --property=ProtectSystem=strict --property=ProtectHome=yes \
  --property=PrivateTmp=yes --property=PrivateDevices=yes --property=ProtectKernelTunables=yes \
  --property=ProtectKernelModules=yes --property=ProtectKernelLogs=yes --property=ProtectControlGroups=yes \
  --property=RestrictSUIDSGID=yes --property=LockPersonality=yes --property=RestrictNamespaces=yes \
  --property=CapabilityBoundingSet= --property="RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX" \
  --property="ReadWritePaths=$state_dir $worktree" -- "$@"

