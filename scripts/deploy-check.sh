#!/usr/bin/env bash
# Static checks of everything in deploy/ with the real tools, without root:
#   systemd-analyze verify (unit syntax), nft -c in a private user+network namespace (ruleset),
#   caddy validate (Caddyfile; needs CADDY=/path/to/caddy or caddy on PATH).
# Prints PASS/FAIL/SKIP per check; exit status = number of failures.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
fails=0
pass() { echo "PASS $*"; }
fail() { echo "FAIL $*"; fails=$((fails+1)); }
skip() { echo "SKIP $*"; }
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT

for script in "$ROOT/scripts/deploy-check.sh" "$ROOT/scripts/run-agent-isolated.sh"; do
  out=$(bash -n "$script" 2>&1) && pass "bash -n $(basename "$script")" || fail "$(basename "$script"): $out"
done

if command -v systemd-analyze >/dev/null; then
  for u in glassboard.service glassboard-backup.service glassboard-backup.timer; do
    # Paths/users of the target VM do not exist here: only syntax and directive errors count.
    out=$(systemd-analyze verify --man=no "$ROOT/deploy/$u" 2>&1 | grep -vE "does not exist|No such file|not executable|Failed to resolve user|Unit .* not found|Cannot add dependency|is not a valid user" )
    [ -z "$out" ] && pass "systemd-analyze verify $u" || fail "$u: $out"
  done
else skip "systemd-analyze not installed"; fi

if command -v nft >/dev/null && unshare -rn true 2>/dev/null; then
  out=$(unshare -rn nft -c -f "$ROOT/deploy/nftables-lan-only.conf" 2>&1) && pass "nft -c nftables-lan-only.conf" || fail "nft: $out"
else skip "nft or unprivileged namespaces not available"; fi

CADDY=${CADDY:-$(command -v caddy || true)}
if [ -n "$CADDY" ]; then
  out=$(XDG_DATA_HOME="$TMP/data" XDG_CONFIG_HOME="$TMP/cfg" HOME="$TMP" "$CADDY" validate --config "$ROOT/deploy/Caddyfile" --adapter caddyfile 2>&1) \
    && pass "caddy validate deploy/Caddyfile" || fail "caddy: $(tail -3 <<<"$out")"
else skip "caddy not found (set CADDY=)"; fi

[ $fails = 0 ] && echo "deploy-check: OK" || echo "deploy-check: $fails FAIL"
exit $fails
