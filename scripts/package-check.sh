#!/usr/bin/env bash
# Release check: build the wheel and sdist from this checkout, inspect them, install the wheel into a
# FRESH virtualenv outside the source tree (hash-locked server dependencies), then run the
# README-linked coordination demo against the installed package. Usage: scripts/package-check.sh [python3.11+]
set -euo pipefail
export PIP_DISABLE_PIP_VERSION_CHECK=1
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${1:-${PYTHON:-python3}}"
WORK=$(mktemp -d); trap 'rm -rf "$WORK"' EXIT
fails=0
pass() { echo "PASS $*"; }
fail() { echo "FAIL $*"; fails=$((fails+1)); }

# 1. build (setuptools from requirements/build.lock, no build isolation: offline-reproducible)
mk_venv() {
  if "$PY" -m venv "$1" 2>/dev/null && [ -x "$1/bin/pip" ]; then return 0; fi
  rm -rf "$1"; command -v uv >/dev/null || { echo "need python -m venv with pip, or uv"; exit 2; }
  uv venv -q --seed --python "$PY" "$1"
}
mk_venv "$WORK/build"
"$WORK/build/bin/pip" install -q --require-hashes -r "$ROOT/requirements/build.lock"
cp -r "$ROOT" "$WORK/src" && rm -rf "$WORK/src"/.venv* "$WORK/src"/build "$WORK/src"/*.egg-info "$WORK/src/src"/*.egg-info
( cd "$WORK/src" && "$WORK/build/bin/python" -m pip wheel -q --no-deps --no-build-isolation -w "$WORK/dist" . \
  && "$WORK/build/bin/python" -c "from setuptools import build_meta as b; b.build_sdist('$WORK/dist')" >/dev/null )
WHL=$(ls "$WORK"/dist/glassboard-*.whl); SDIST=$(ls "$WORK"/dist/glassboard-*.tar.gz)
VER=$(sed -n 's/^version = "\(.*\)"/\1/p' "$ROOT/pyproject.toml")
[[ "$WHL" == *"glassboard-$VER-py3-none-any.whl" ]] && pass "wheel built: $(basename "$WHL")" || fail "wheel name $WHL"
[[ "$SDIST" == *"glassboard-$VER.tar.gz" ]] && pass "sdist built: $(basename "$SDIST")" || fail "sdist name $SDIST"

# 2. contents: templates, static files, SQL migrations and the licence travel with the wheel
LIST=$("$WORK/build/bin/python" -c "import zipfile,sys; print('\n'.join(zipfile.ZipFile(sys.argv[1]).namelist()))" "$WHL")
for f in glassboard/sql/001_b0a.sql glassboard/sql/013_artifacts.sql glassboard/sql/016_purge_actor_history.sql glassboard/templates/base.html glassboard/templates/artifacts.html glassboard/static/app.css glassboard/static/poll.js glassboard/static/artifacts.js glassboard/static/favicon.svg glassboard/static/guide-timeline.png glassboard/static/guide-board.png \
         glassboard/cli.py glassboard/serve.py; do
  grep -qx "$f" <<<"$LIST" || fail "wheel misses $f"
done
grep -q "dist-info/licenses/LICENSE" <<<"$LIST" && pass "wheel carries its data files and LICENSE" || fail "LICENSE not in wheel"
grep -qE "(^|/)tests/|\.venv|__pycache__" <<<"$LIST" && fail "wheel carries test or cache files" || pass "wheel carries no tests or caches"

# 3. the CLI alone: stdlib only, no dependencies
mk_venv "$WORK/cli"
"$WORK/cli/bin/pip" install -q --no-deps "$WHL"
"$WORK/cli/bin/glassboard" --version | grep -q "$VER" && "$WORK/cli/bin/python" -c "
import sys, glassboard.cli, glassboard.tlsboot
bad = [m for m in sys.modules if m.split('.')[0] in ('fastapi','starlette','pydantic','jinja2','uvicorn')]
assert not bad, bad" && pass "CLI installs and runs with no dependencies" || fail "CLI-only install"

# 4. a server: hash-locked dependencies + the wheel, then the README-linked demo against it
mk_venv "$WORK/srv"
"$WORK/srv/bin/pip" install -q --require-hashes -r "$ROOT/requirements/server.lock"
"$WORK/srv/bin/pip" install -q --no-deps "$WHL"
"$WORK/srv/bin/pip" check >/dev/null && pass "server install: pip check clean" || fail "pip check"
mkdir "$WORK/run"
if out=$(cd "$WORK/run" && env -u GLASSBOARD_DB -u XDG_CONFIG_HOME -u XDG_STATE_HOME HOME="$WORK/run" \
        GLASSBOARD_DEMO_DIR="$WORK/demo" PATH="$WORK/srv/bin:$PATH" \
        bash "$ROOT/examples/coordination-demo.sh" 2>&1); then
  grep -q "\[task.accepted\] T1" <<<"$out" && pass "README-linked demo runs against the installed wheel" \
    || fail "demo output: $(tail -5 <<<"$out")"
else fail "demo failed: $(tail -15 <<<"$out")"; fi
python_src=$(cd "$WORK/run" && "$WORK/srv/bin/python" -c "import glassboard; print(glassboard.__file__)")
[[ "$python_src" == "$WORK/srv/"* ]] && pass "ran the installed package, not the checkout" || fail "imported $python_src"

[ $fails = 0 ] && echo "package-check: OK ($VER)" || echo "package-check: $fails FAIL"
exit $fails
