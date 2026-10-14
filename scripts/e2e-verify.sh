#!/usr/bin/env bash
# End-to-end check on ONE machine: uvicorn behind Caddy (internal CA) on board.lan:8443,
# participant CLI over verified TLS. Needs: python venv with .[server], caddy binary, root (for /etc/hosts)
# or an existing board.lan → 127.0.0.1 mapping. Prints PASS/FAIL lines; exits non-zero on any FAIL.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CADDY="${CADDY:-caddy}"
WORK="$(mktemp -d)"
export HOME="$WORK/home" XDG_CONFIG_HOME="$WORK/home/.config" XDG_STATE_HOME="$WORK/home/.state" XDG_DATA_HOME="$WORK/caddy-data"
mkdir -p "$HOME"
export GLASSBOARD_DB="$WORK/board.db" GLASSBOARD_ALLOWED_HOSTS="board.lan"
# App port is configurable because 8080 is often taken on dev machines (e.g. by an HTTP proxy).
export GLASSBOARD_APP_PORT="${GLASSBOARD_APP_PORT:-8080}"
APP="$GLASSBOARD_APP_PORT"
PLAIN=$((APP + 1))
fails=0
command -v glassboard >/dev/null && command -v glassboard-admin >/dev/null && command -v uvicorn >/dev/null || { echo "FAIL precondition: install the package first (see docs/deploy.md)"; exit 1; }
pass() { echo "PASS $*"; }
fail() { echo "FAIL $*"; fails=$((fails+1)); }
cleanup() { kill ${APP_PID:-} ${CADDY_PID:-} ${CADDY2_PID:-} 2>/dev/null; wait 2>/dev/null; }
trap cleanup EXIT

grep -qE '^127\.0\.0\.1\s+board\.lan' /etc/hosts || { [ "$(id -u)" = 0 ] && echo "127.0.0.1 board.lan" >> /etc/hosts; }
getent hosts board.lan >/dev/null && pass "board.lan resolves" || { fail "board.lan does not resolve"; exit 1; }

glassboard-admin init >/dev/null
glassboard-admin add-human jose >/dev/null
glassboard-admin add-agent jose claude >/dev/null
glassboard-admin mint-token jose/claude --out "$WORK/claude.token" 2>/dev/null
[ -s "$WORK/claude.token" ] || { echo "FAIL precondition: token not minted"; exit 1; }

for port in "$APP" "$PLAIN"; do
  ss -ltn 2>/dev/null | grep -qE "127\\.0\\.0\\.1:$port\\b|\\*:$port\\b|:$port\\s" \
    && { echo "FAIL precondition: port $port already in use; set GLASSBOARD_APP_PORT so it and the next port are free"; exit 1; }
done
uvicorn glassboard.serve:app --host 127.0.0.1 --port "$APP" --workers 1 --proxy-headers --forwarded-allow-ips 127.0.0.1 --no-server-header --log-level warning &
APP_PID=$!
# Test-only copy: default_sni makes Caddy present the board.lan cert to ANY SNI, so check 3 exercises
# the CLI's own hostname verification instead of Caddy refusing an unknown SNI.
sed 's/^\tadmin off$/\tadmin off\n\tdefault_sni board.lan/' "$ROOT/deploy/Caddyfile" > "$WORK/Caddyfile"
"$CADDY" run --config "$WORK/Caddyfile" --adapter caddyfile >"$WORK/caddy.log" 2>&1 &
CADDY_PID=$!
for i in $(seq 1 50); do curl -s -o /dev/null http://127.0.0.1:$APP/health/live -H 'Host: board.lan' && break; sleep 0.2; done
CA="$XDG_DATA_HOME/caddy/pki/authorities/local/root.crt"
for i in $(seq 1 50); do [ -f "$CA" ] && curl -s --cacert "$CA" https://board.lan:8443/health/live >/dev/null && break; sleep 0.2; done
[ -f "$CA" ] && pass "caddy internal CA root generated" || { fail "no CA root"; exit 1; }

# 1. CLI login over verified TLS with the internal root
out=$(glassboard login --url https://board.lan:8443 --ca-file "$CA" --token-file "$WORK/claude.token" 2>&1) \
  && [[ "$out" == "@jose/claude (agent, owner jose)"* ]] && pass "login+whoami over TLS: $out" || fail "login: $out"
mode=$(stat -c %a "$XDG_CONFIG_HOME/glassboard/credentials"); [ "$mode" = 600 ] && pass "credentials file 0600" || fail "credentials mode $mode"
glassboard whoami --json | grep -q '"handle": "jose/claude"' && pass "whoami --json" || fail "whoami --json"

# 2. Verification is really on: without the CA the CLI must fail with a TLS error (exit 4)
glassboard login --url https://board.lan:8443 --token-file "$WORK/claude.token" >/dev/null 2>"$WORK/e1"; rc=$?
[ $rc = 4 ] && grep -q tls_error "$WORK/e1" && pass "untrusted CA rejected (exit 4)" || fail "untrusted CA accepted? rc=$rc $(cat "$WORK/e1")"

# 3. Hostname verification: same server, same CA, wrong name
glassboard login --url https://127.0.0.1:8443 --ca-file "$CA" --token-file "$WORK/claude.token" >/dev/null 2>"$WORK/e2"; rc=$?
[ $rc = 4 ] && grep -qiE "hostname|IP address mismatch|CERTIFICATE_VERIFY_FAILED" "$WORK/e2" && pass "client hostname verification rejects board.lan cert at 127.0.0.1 (exit 4): $(head -c 140 "$WORK/e2")" || fail "hostname mismatch rc=$rc $(cat "$WORK/e2")"

# 4. Plain http to a non-loopback URL is refused by the CLI
glassboard login --url http://board.lan:8080 --token-file "$WORK/claude.token" >/dev/null 2>&1; rc=$?
[ $rc = 2 ] && pass "plain http refused (exit 2)" || fail "plain http rc=$rc"

# 5. Bad token through the proxy → 401, exit 3
printf 'gbt_aaaaaaaaaa_%s\n' "$(head -c 43 /dev/zero | tr '\0' A)" > "$WORK/bad.token"
glassboard login --url https://board.lan:8443 --ca-file "$CA" --token-file "$WORK/bad.token" >/dev/null 2>"$WORK/e3"; rc=$?
[ $rc = 3 ] && grep -q unauthenticated "$WORK/e3" && pass "bad token → 401 (exit 3)" || fail "bad token rc=$rc"

# 6. DNS-rebinding defence: valid TLS to board.lan but a foreign Host header must not reach a 200
code=$(curl -s -o "$WORK/h6" -w '%{http_code}' --cacert "$CA" -H 'Host: evil.example' https://board.lan:8443/api/v1/discovery)
[ "$code" = 421 ] && pass "foreign Host header refused by proxy (HTTP $code)" || fail "foreign Host served: $(cat "$WORK/h6")"
# 6b. Bypassing Caddy and hitting the app port directly with a foreign Host → app TrustedHost 400
code=$(curl -s -o /dev/null -w '%{http_code}' -H 'Host: evil.example' http://127.0.0.1:$APP/api/v1/discovery)
[ "$code" = 400 ] && pass "app rejects foreign Host directly (HTTP 400)" || fail "app direct foreign host HTTP $code"

# 7. Validation errors through the proxy never echo input
code=$(curl -s -o "$WORK/h7" -w '%{http_code}' --cacert "$CA" -H "Authorization: Bearer $(cat "$WORK/claude.token")" "https://board.lan:8443/api/v1/whoami?format=PWN%3Cscript%3E")
# Require a real 422 first: an empty body from a failed connection must not count as "no echo".
[ "$code" = 422 ] && ! grep -q PWN "$WORK/h7" && pass "422 through proxy has no echo" || fail "422 check: HTTP $code $(cat "$WORK/h7")"

# 8. Token never in logs
grep -q "$(cut -d_ -f3 "$WORK/claude.token")" "$WORK/caddy.log" && fail "token secret in caddy log" || pass "token secret absent from caddy log"

# 9. Runtime env passthrough via the wrapper (generic child process)
probe=$(glassboard run -- bash -c 'glassboard env-probe --json' 2>/dev/null)
echo "$probe" | grep -q '"runtime.source": "env"' && pass "run wrapper → child sees GLASSBOARD_RUNTIME" || fail "wrapper env: $probe"
probe=$(glassboard run -- bash -c 'true; env -i PATH="$PATH" HOME="$HOME" XDG_STATE_HOME="$XDG_STATE_HOME" glassboard env-probe --json' 2>/dev/null)
echo "$probe" | grep -q '"runtime.source": "ancestor:' && pass "scrubbed env → ancestor fallback finds runtime" || fail "fallback: $probe"

# 10. LAN-only guard: a second proxy instance whose allow-list excludes loopback must
#     refuse our (loopback) source address with 403 and never reach the app.
GLASSBOARD_TLS_PORT=8444 GLASSBOARD_ALLOWED_CIDRS="10.0.0.0/8" "$CADDY" run --config "$WORK/Caddyfile" --adapter caddyfile >"$WORK/caddy2.log" 2>&1 &
CADDY2_PID=$!
for i in $(seq 1 50); do curl -s -o /dev/null --cacert "$CA" https://board.lan:8444/ && break; sleep 0.2; done
code=$(curl -s -o "$WORK/h10" -w '%{http_code}' --cacert "$CA" https://board.lan:8444/api/v1/discovery)
[ "$code" = 403 ] && grep -q "LAN only" "$WORK/h10" && pass "source outside allowed CIDRs refused (HTTP 403)" || fail "LAN guard: HTTP $code $(cat "$WORK/h10")"
code=$(curl -s -o /dev/null -w '%{http_code}' --cacert "$CA" https://board.lan:8443/api/v1/discovery)
[ "$code" = 200 ] && pass "source inside default private_ranges allowed (HTTP 200)" || fail "LAN guard default: HTTP $code"

# Sessions over TLS with the CLI: a human creates a session and adds an agent; the agent sees
#  it; the human mints a token for an owned agent into a 0600 file.
glassboard-admin mint-token jose --out "$WORK/jose.token" 2>/dev/null
J=(env XDG_CONFIG_HOME="$WORK/jose-cfg" XDG_STATE_HOME="$WORK/jose-state")
"${J[@]}" glassboard login --url https://board.lan:8443 --ca-file "$CA" --token-file "$WORK/jose.token" >/dev/null 2>&1
out=$("${J[@]}" glassboard session create docs "Write the docs" 2>&1)
[[ "$out" == "session docs created · you are coordinator"* ]] && pass "human creates a session over TLS" || fail "session create: $out"
out=$("${J[@]}" glassboard members add docs jose/claude 2>&1)
[[ "$out" == "@jose/claude added · member"* ]] && pass "coordinator adds an agent: $out" || fail "members add: $out"
out=$(glassboard sessions 2>&1)
echo "$out" | grep -q "^docs · member · active" && pass "agent lists its session" || fail "sessions: $out"
"${J[@]}" glassboard token new jose/claude --out "$WORK/claude2.token" 2>/dev/null
[ "$(stat -c %a "$WORK/claude2.token" 2>/dev/null)" = 600 ] && grep -q '^gbt_' "$WORK/claude2.token" && pass "token for an owned agent written 0600" || fail "token new"

# Idempotent replay through the proxy: same key twice → identical body + Glassboard-Replayed.
REC=$("${J[@]}" glassboard sessions --json | python -c 'import json,sys;print(json.load(sys.stdin)["sessions"][0]["recovery_id"])')
for n in 1 2; do
  curl -s -o "$WORK/r5_$n" -D "$WORK/r5h_$n" --cacert "$CA" -X POST -H "Content-Type: application/json" \
    -H "Authorization: Bearer $(cat "$WORK/jose.token")" -H "Idempotency-Key: verify-replay-0001" \
    -H "X-Glassboard-Recovery: $REC" -d '{"handle":"jose/claude","role":"observer"}' https://board.lan:8443/api/v1/sessions/docs/members
done
cmp -s "$WORK/r5_1" "$WORK/r5_2" && grep -qi '^glassboard-replayed: true' "$WORK/r5h_2" && ! grep -qi '^glassboard-replayed' "$WORK/r5h_1" \
  && pass "replay through proxy is byte-identical with Glassboard-Replayed" || fail "replay: $(cat "$WORK/r5_1") / $(cat "$WORK/r5_2")"

# Messaging over TLS: a human asks the agent a question; the agent attaches its inbox and
#  receives it; there is no GET /sync; the export holds no consumer secret.
api() { local tok="$1" method="$2" path="$3" shift_body="${4:-}"; shift 4 2>/dev/null
  curl -s -o "$WORK/api_body" -w '%{http_code}' --cacert "$CA" -X "$method" -H "Authorization: Bearer $(cat "$tok")" \
    -H "Content-Type: application/json" -H "X-Glassboard-Recovery: $REC" -H "Idempotency-Key: v$(date +%s%N)" \
    ${shift_body:+-d "$shift_body"} "https://board.lan:8443/api/v1$path"; }
"${J[@]}" glassboard members add docs jose/claude --role member >/dev/null 2>&1
code=$(api "$WORK/jose.token" POST /sessions/docs/messages '{"kind":"question","body":"Is TLS verified?","to":["jose/claude"]}')
[ "$code" = 201 ] && pass "human posts a question over TLS" || fail "post: HTTP $code $(cat "$WORK/api_body")"
code=$(api "$WORK/jose.token" POST /sessions/docs/messages/preflight '{"kind":"note","body":"Unsent checked draft."}')
python -c 'import json,sys; j=json.load(open(sys.argv[1])); assert j["status"]=="ready" and not j["redacted"]' "$WORK/api_body" 2>/dev/null \
  && [ "$code" = 200 ] && pass "preflight records a ready draft over TLS" || fail "preflight: HTTP $code $(cat "$WORK/api_body")"
code=$(api "$WORK/claude.token" POST /sessions/docs/sync '{}')
python -c 'import json,sys,re; j=json.load(open(sys.argv[1])); assert re.fullmatch(r"C\d+-[a-z2-7]{10}", j["consumer"]); assert any(e["body"]=="Is TLS verified?" for e in j["inbox"]); assert not any(e["type"]=="message.preflight" for e in j["inbox"]); assert j["owed_total"]==1' "$WORK/api_body" 2>/dev/null \
  && [ "$code" = 200 ] && pass "agent attaches and receives the question (owed 1)" || fail "sync: HTTP $code $(head -c 200 "$WORK/api_body")"
CONSUMER_SECRET=$(python -c 'import json,sys; print(json.load(open(sys.argv[1]))["consumer"].split("-")[1])' "$WORK/api_body" 2>/dev/null)
code=$(curl -s -o /dev/null -w '%{http_code}' --cacert "$CA" -H "Authorization: Bearer $(cat "$WORK/claude.token")" https://board.lan:8443/api/v1/sessions/docs/sync)
[ "$code" = 405 ] && pass "GET /sync is 405" || fail "GET sync: HTTP $code"
code=$(curl -s -o "$WORK/export.md" -w '%{http_code}' --cacert "$CA" -H "Authorization: Bearer $(cat "$WORK/jose.token")" "https://board.lan:8443/api/v1/sessions/docs/export?format=md")
[ "$code" = 200 ] && [ -n "$CONSUMER_SECRET" ] && ! grep -q "$CONSUMER_SECRET" "$WORK/export.md" && grep -q "^#.* \[question\]" "$WORK/export.md" \
  && pass "export over TLS has the question and no consumer secret" || fail "export: HTTP $code"

# The CLI agent loop over TLS. The curl runtime of the messaging check holds jose/claude's inbox, so the CLI
#  must see consumer_conflict and take over explicitly; then it receives a message and acks.
glassboard use docs >/dev/null 2>&1
out=$(glassboard sync 2>&1); rc=$?
[ $rc = 5 ] && echo "$out" | grep -q consumer_conflict && out=$(glassboard sync --takeover "verify harness" 2>&1) \
  && echo "$out" | grep -q "consumer C" && pass "CLI sees consumer_conflict, then takes over explicitly" || fail "takeover: rc=$rc $out"
"${J[@]}" glassboard use docs >/dev/null 2>&1
out=$("${J[@]}" glassboard post note "CLI over TLS works" --to jose/claude 2>&1)
[[ "$out" == "posted #"* ]] && pass "human posts with the CLI: $out" || fail "post: $out"
out=$(glassboard sync --ack 2>&1 && glassboard sync 2>&1)
echo "$out" | grep -q "  | CLI over TLS works" && glassboard sync --ack >/dev/null 2>&1 && glassboard sync 2>&1 | grep -q "^inbox: 0 new" \
  && pass "CLI receives the message, acks, and the inbox empties" || fail "sync/ack: $out"

# Dashboard over TLS: login nonce, hardened session cookie, CSP, CSRF.
JAR="$WORK/cookies"
curl -s -c "$JAR" -b "$JAR" -D "$WORK/h9a" -o "$WORK/login.html" --cacert "$CA" https://board.lan:8443/login
NONCE=$(grep -o 'name="csrf" value="[^"]*"' "$WORK/login.html" | sed 's/.*value="//;s/"$//')
grep -qi "^content-security-policy: default-src 'self'" "$WORK/h9a" && [ -n "$NONCE" ] && pass "login page has CSP and a nonce" || fail "login page"
code=$(curl -s -c "$JAR" -b "$JAR" -D "$WORK/h9b" -o /dev/null -w '%{http_code}' --cacert "$CA" \
  --data-urlencode "csrf=$NONCE" --data-urlencode "token=$(cat "$WORK/jose.token")" https://board.lan:8443/login)
grep -i "^set-cookie: __Host-gb_session=" "$WORK/h9b" | grep -qi "secure" && grep -i "^set-cookie: __Host-gb_session=" "$WORK/h9b" | grep -qi "httponly" \
  && [ "$code" = 303 ] && pass "login sets a Secure HttpOnly __Host- cookie" || fail "login: HTTP $code"
code=$(curl -s -c "$JAR" -b "$JAR" -D "$WORK/h9c" -o "$WORK/s.html" -w '%{http_code}' --cacert "$CA" https://board.lan:8443/s/docs)
[ "$code" = 200 ] && grep -q "CLI over TLS works" "$WORK/s.html" && grep -qi "^content-security-policy:" "$WORK/h9c" \
  && pass "session page over TLS shows the timeline with CSP" || fail "session page: HTTP $code"
code=$(curl -s -b "$JAR" -o "$WORK/trace.jsonl" -w '%{http_code}' --cacert "$CA" 'https://board.lan:8443/s/docs/record.jsonl?only=trace')
python -c 'import json,sys; rows=[json.loads(x) for x in open(sys.argv[1])]; assert rows[0]["subset"]=="trace"; assert len(rows)==2 and rows[1]["type"]=="message.preflight" and "Unsent checked draft." in rows[1]["body"]' "$WORK/trace.jsonl" 2>/dev/null \
  && [ "$code" = 200 ] && pass "browser forensic JSONL contains the unsent draft" || fail "forensic trace: HTTP $code"
code=$(curl -s -b "$JAR" -o /dev/null -w '%{http_code}' --cacert "$CA" --data "kind=note&body=no+csrf" https://board.lan:8443/s/docs/post)
[ "$code" = 403 ] && pass "form post without CSRF token → 403" || fail "csrf: HTTP $code"

# A task lifecycle over TLS with the CLI: human creates, agent claims (fence never printed),
# notes and submits exact source/test evidence; the named reviewer binds a verdict to that
# submission and commit; the human accepts.
COMMIT=0123456789abcdef0123456789abcdef01234567
"${J[@]}" glassboard task create "Verify harness task" --scope "Check the loop." --deliverable "evidence" --reviewer jose >/dev/null 2>&1
o1=$(glassboard task claim T1 2>&1); o2=$(glassboard task note T1 "step one" 2>&1)
o3=$(glassboard task submit T1 "done" --commit "$COMMIT" --test 'pass|verify harness|passed' 2>&1)
SUBMISSION=$(sed -n 's/.* · #\([0-9][0-9]*\).*/\1/p' <<<"$o3")
o4=$("${J[@]}" glassboard task review T1 approve --submission "$SUBMISSION" --commit "$COMMIT" --notes "Exact commit checked." 2>&1)
o5=$("${J[@]}" glassboard task accept T1 2>&1)
[[ "$o5" == "T1 done"* ]] && [ -n "$SUBMISSION" ] && echo "$o4" | grep -q " · #" \
  && echo "$o1" | grep -q "kept for this runtime" && ! echo "$o1$o2$o3" | grep -qE 'T1g[0-9]+-[a-z2-7]{10}' \
  && pass "exact-submission review and acceptance over TLS; no fence secret printed" || fail "tasks: $o1 | $o2 | $o3 | $o4 | $o5"

# Immutable artifact upload and verified download through TLS.
printf 'TLS artifact evidence\n' > "$WORK/artifact.txt"
a1=$(glassboard artifact upload "$WORK/artifact.txt" --task T1 2>&1)
ARTIFACT=$(sed -n 's/^artifact \([^ ]*\) uploaded.*/\1/p' <<<"$a1")
a2=$("${J[@]}" glassboard artifact get "$ARTIFACT" --out "$WORK/artifact-downloaded.txt" 2>&1)
[ -n "$ARTIFACT" ] && cmp -s "$WORK/artifact.txt" "$WORK/artifact-downloaded.txt" && [[ "$a2" == *"sha256"* ]] \
  && pass "immutable artifact upload and integrity-checked download over TLS" || fail "artifact: $a1 | $a2"

# Pause over TLS: the agent's claim is refused with session_paused; resume restores it.
"${J[@]}" glassboard task create "Paused task" --scope "s" --deliverable "d" >/dev/null 2>&1
o1=$("${J[@]}" glassboard session pause "verify harness" 2>&1); o2=$(glassboard task claim T2 2>&1); rc=$?
o3=$("${J[@]}" glassboard session resume "done" 2>&1); o4=$(glassboard task claim T2 2>&1)
[[ "$o1" == *"active → paused"* ]] && [ $rc = 5 ] && echo "$o2" | grep -q session_paused && [[ "$o4" == "T2 in_progress"* ]] \
  && pass "pause refuses claims over TLS; resume restores them" || fail "pause: $o1 | $o2 | $o3 | $o4"

# Long-poll through the proxy: a waiting sync returns promptly when a message arrives.
glassboard sync >/dev/null 2>&1; glassboard sync --ack >/dev/null 2>&1      # drain: a fresh boundary, then ack it
( start_ms=$(date +%s%3N); glassboard sync --wait 15 > "$WORK/lp.out" 2>&1; echo "$(( $(date +%s%3N) - start_ms ))" > "$WORK/lp.time" ) &
LP=$!; sleep 1.5
"${J[@]}" glassboard post note "long-poll wake-up" --to jose/claude >/dev/null 2>&1
wait $LP
took_ms=$(cat "$WORK/lp.time" 2>/dev/null || echo 99000)
grep -q "  | long-poll wake-up" "$WORK/lp.out" && [ "$took_ms" -lt 5000 ] \
  && pass "long-poll through Caddy returned $((took_ms / 1000)).$(((took_ms % 1000) / 100))s after start, with the message posted at 1.5s" || fail "long-poll: took ${took_ms}ms $(head -c 200 "$WORK/lp.out")"

# Backup while serving, stop, restore, restart; the CLI meets the restore over TLS.
glassboard-admin backup "$WORK/b16.db" --no-encrypt >/dev/null 2>&1 && [ -s "$WORK/b16.db" ] && [ -s "$WORK/b16.db.journal" ] \
  && pass "online backup while the board runs (+ journal copy)" || fail "backup"
"${J[@]}" glassboard post note "after the backup: will be lost" >/dev/null 2>&1
kill $APP_PID 2>/dev/null; wait $APP_PID 2>/dev/null
glassboard-admin restore "$WORK/b16.db" > "$WORK/restore.out" 2>&1; rrc=$?
uvicorn glassboard.serve:app --host 127.0.0.1 --port "$APP" --workers 1 --proxy-headers --forwarded-allow-ips 127.0.0.1 --no-server-header --log-level warning &
APP_PID=$!
for i in $(seq 1 50); do curl -s -o /dev/null http://127.0.0.1:$APP/health/live -H 'Host: board.lan' && break; sleep 0.2; done
o1=$(glassboard sync 2>&1); rc1=$?
o2=$(glassboard reconcile 2>&1); o3=$("${J[@]}" glassboard reconcile 2>&1)
o4=$("${J[@]}" glassboard session resume "reconciled after restore" 2>&1)
[ $rrc = 0 ] && [ $rc1 = 7 ] && echo "$o1" | grep -q recovery_mismatch && echo "$o2" | grep -q "adopted recovery" \
  && [[ "$o4" == *"paused → active"* ]] && ! "${J[@]}" glassboard history 2>/dev/null | grep -q "will be lost" \
  && pass "restore → CLI exit 7 (recovery_mismatch) → reconcile → resume; post-backup message gone" \
  || fail "restore: rrc=$rrc rc1=$rc1 | $o1 | $o2 | $o4 | $(head -c 300 "$WORK/restore.out")"
"${J[@]}" glassboard artifact get "$ARTIFACT" --out "$WORK/artifact-restored.txt" >/dev/null 2>&1 \
  && cmp -s "$WORK/artifact.txt" "$WORK/artifact-restored.txt" \
  && pass "immutable artifact survives online backup and restore with identical bytes" \
  || fail "artifact missing or changed after restore"

# 11. Agent protocol is public, served as Markdown, under the new name
code=$(curl -s -o "$WORK/h11" -w '%{http_code} %{content_type}' --cacert "$CA" https://board.lan:8443/api/v1/protocol)
[[ "$code" == "200 text/markdown"* ]] && cmp -s "$WORK/h11" "$ROOT/docs/protocol.md" && pass "/protocol through proxy ($code)" || fail "/protocol: $code $(head -c 80 "$WORK/h11")"

# 12. A chunked body over the limit is refused even without Content-Length
head -c 70000 /dev/zero | tr '\0' x > "$WORK/big"
code=$(curl -s -o "$WORK/h12" -w '%{http_code}' --cacert "$CA" -H "Transfer-Encoding: chunked" -H "Content-Type: application/json" --data-binary @"$WORK/big" https://board.lan:8443/api/v1/whoami)
[ "$code" = 413 ] && grep -q payload_too_large "$WORK/h12" && pass "chunked 70 KB body → 413" || fail "chunked body: HTTP $code $(head -c 120 "$WORK/h12")"

# 12b. (Optional) Only when the optional MCP extra is installed and GLASSBOARD_MCP=1 was
#      exported: board_whoami through Caddy/TLS; a foreign Origin is refused. Without it, /mcp is 404.
RPC='{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"board_whoami","arguments":{}}}'
mcp_post() { curl -s -o "$WORK/hm" -w '%{http_code}' --cacert "$CA" -H "Authorization: Bearer $(cat "$WORK/claude.token")" \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" "$@" --data "$RPC" https://board.lan:8443/mcp; }
if [ "${GLASSBOARD_MCP:-}" = 1 ]; then
  c1=$(mcp_post); b1=$(cat "$WORK/hm"); c2=$(mcp_post -H "Origin: https://evil.example"); b2=$(cat "$WORK/hm")
  [ "$c1" = 200 ] && [[ "$b1" == *'@jose/claude (agent, owner jose)'* ]] && [ "$c2" = 403 ] && [[ "$b2" != *evil* ]] \
    && pass "MCP board_whoami over TLS; foreign Origin → 403" || fail "MCP: $c1 $(head -c 160 <<<"$b1") | $c2"
else
  c1=$(mcp_post); [ "$c1" = 404 ] && pass "/mcp absent unless the MCP extra and GLASSBOARD_MCP=1 are both set" || fail "/mcp present without opt-in: HTTP $c1"
fi

# 12c. Easier TLS: setup-tls finds Caddy's root, publishes /ca.crt, prints the fingerprint; a fresh
#      client pins it with --fetch-ca. A wrong fingerprint saves nothing.
st=$(glassboard-admin setup-tls --host board.lan --port 8443 2>&1); FP=$(sed -n 's/^board CA fingerprint: //p' <<<"$st")
FRESH="$WORK/fresh"; mkdir -p "$FRESH"
fr() { HOME="$FRESH" XDG_CONFIG_HOME="$FRESH/cfg" XDG_STATE_HOME="$FRESH/st" glassboard "$@"; }
bad=$(fr login --url https://board.lan:8443 --fetch-ca --ca-fingerprint "SHA256:$(printf 'AB%.0s' $(seq 32))" --token-file "$WORK/claude.token" 2>&1); brc=$?
good=$(fr login --url https://board.lan:8443 --fetch-ca --ca-fingerprint "$FP" --token-file "$WORK/claude.token" 2>&1); grc=$?
[ -n "$FP" ] && [ $brc = 4 ] && [[ "$bad" == *ca_fingerprint_mismatch* ]] && [ $grc = 0 ] && [[ "$good" == *"@jose/claude (agent, owner jose)"* ]] \
  && cmp -s "$FRESH/cfg/glassboard/ca.crt" "$CA" && fr whoami >/dev/null \
  && pass "setup-tls + login --fetch-ca pins the board CA through Caddy (wrong fingerprint refused)" \
  || fail "fetch-ca: fp=$FP brc=$brc grc=$grc | $(head -c 200 <<<"$st$bad$good")"

# 12d. Plain-HTTP mode: a second app instance with GLASSBOARD_INSECURE_HTTP=1, reached by name (not
#      literal loopback): the CLI refuses it without --insecure-http and warns with it.
GLASSBOARD_INSECURE_HTTP=1 uvicorn glassboard.serve:app --host 127.0.0.1 --port "$PLAIN" --workers 1 --log-level warning 2>"$WORK/plain.log" &
PLAIN_PID=$!
for i in $(seq 1 50); do curl -s -o /dev/null -H 'Host: board.lan' http://127.0.0.1:$PLAIN/health/live && break; sleep 0.2; done
PH="$WORK/plainhome"; mkdir -p "$PH"
ph() { HOME="$PH" XDG_CONFIG_HOME="$PH/cfg" XDG_STATE_HOME="$PH/st" glassboard "$@"; }
p1=$(ph login --url http://board.lan:$PLAIN --token-file "$WORK/claude.token" 2>&1); prc1=$?
p2=$(ph login --url http://board.lan:$PLAIN --insecure-http --token-file "$WORK/claude.token" 2>&1); prc2=$?
disc=$(curl -s -H 'Host: board.lan' http://127.0.0.1:$PLAIN/api/v1/discovery)
kill $PLAIN_PID 2>/dev/null; wait $PLAIN_PID 2>/dev/null
[ $prc1 = 2 ] && [ $prc2 = 0 ] && [[ "$p2" == *"plain HTTP, no TLS"* ]] && [[ "$p2" == *"@jose/claude"* ]] \
  && [[ "$disc" == *'"transport":"plain-http"'* ]] && grep -q "NO TLS" "$WORK/plain.log" \
  && pass "plain-HTTP mode: refused without --insecure-http, warns with it, discovery and server log warn" \
  || fail "plain: $prc1 $prc2 | $(head -c 200 <<<"$p1$p2$disc")"

# 13. 10 auth failures/min/IP → 429, even for a valid token. Runs last: it locks this address out.
for i in $(seq 1 12); do
  code=$(curl -s -o /dev/null -w '%{http_code}' --cacert "$CA" -H "Authorization: Bearer gbt_aaaaaaaaaa_$(head -c 43 /dev/zero | tr '\0' A)" https://board.lan:8443/api/v1/whoami)
  [ "$code" = 429 ] && break
done
code=$(curl -s -o "$WORK/h13" -D "$WORK/h13h" -w '%{http_code}' --cacert "$CA" -H "Authorization: Bearer $(cat "$WORK/claude.token")" https://board.lan:8443/api/v1/whoami)
[ "$code" = 429 ] && grep -qi '^retry-after:' "$WORK/h13h" && grep -q rate_limited "$WORK/h13" && pass "auth failures → 429 with Retry-After (valid token refused while limited)" || fail "rate limit: HTTP $code $(head -c 120 "$WORK/h13")"

# 14. A client cannot pick its own limiter key: while this address is locked out,
#     a forged X-Forwarded-For must not get through (Caddy must not forward client-supplied XFF).
code=$(curl -s -o /dev/null -w '%{http_code}' --cacert "$CA" -H "X-Forwarded-For: 10.9.9.9" -H "Authorization: Bearer $(cat "$WORK/claude.token")" https://board.lan:8443/api/v1/whoami)
[ "$code" = 429 ] && pass "forged X-Forwarded-For does not escape the limit (HTTP 429)" || fail "forged X-Forwarded-For escaped the limit: HTTP $code"

echo "versions: $(python -c 'import fastapi,uvicorn,pydantic,starlette;print("fastapi",fastapi.__version__,"uvicorn",uvicorn.__version__,"pydantic",pydantic.__version__,"starlette",starlette.__version__)') caddy $("$CADDY" version | cut -d' ' -f1) python $(python -c 'import platform;print(platform.python_version())') openssl $(python -c 'import ssl;print(ssl.OPENSSL_VERSION)')"
[ $fails = 0 ] && echo "verify: ALL PASS" || echo "verify: $fails FAIL"
exit $fails
