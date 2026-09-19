#!/usr/bin/env bash

# Local cross-version test matrix (Linux + uv-managed CPython 3.10-3.15).
#
# Usage:
#   scripts/run-test-matrix.sh                        # full suite, all versions
#   scripts/run-test-matrix.sh tests/unit -x          # extra pytest args are forwarded
#   MATRIX_JOBS=4 scripts/run-test-matrix.sh          # pytest-xdist workers per version
#   MATRIX_CONCURRENCY=2 scripts/run-test-matrix.sh   # versions running at once
#
# Parallelism: `MATRIX_CONCURRENCY` versions run at a time and each one gets
# `MATRIX_JOBS` xdist workers, so the matrix holds the product of the two.  The
# default splits the machine's cores over the concurrent versions and leaves one
# core free: on a 24-thread workstation that is 3 versions x 7 workers, and the
# six versions go out as two waves.
#
# Waves do not change the wall clock much — the matrix is six times one suite, so
# it costs what the cores allow either way (about five minutes for the full suite
# on a 24-thread box, not the two minutes a smaller suite once suggested) — they
# change how much of the machine the run holds at once.
#
# Each version runs in its own process; logs land in .matrix/logs/<version>.log
# and the exit status in .matrix/logs/<version>.status.  One run at a time owns
# that directory: it is wiped before anything starts, so a run in flight is never
# read against the previous run's leftovers, and a second run refuses to start
# rather than wipe the files the first one is still reporting from.

set -u
cd "$(dirname "$0")/.."   # repository root
ROOT=$PWD
LOGS=$ROOT/.matrix/logs
OWNER=$ROOT/.matrix/logs.owner
# One run owns the log directory: a second run would wipe the files the first
# one is still reporting from (its statuses then look like results).  A lock
# left behind by a run that was killed is taken over after six hours.
if [ -f "$OWNER" ]; then
  owner=$(cat "$OWNER" 2>/dev/null)
  if [ -n "$owner" ] && kill -0 "$owner" 2>/dev/null \
     && [ -z "$(find "$OWNER" -mmin +360 2>/dev/null)" ]; then
    echo "refusing to start: matrix run $owner is still using $LOGS" >&2
    exit 2
  fi
  echo "taking over a stale lock $OWNER (pid ${owner:-unknown})"
fi
rm -rf "$LOGS"
mkdir -p "$LOGS"
echo $$ >"$OWNER"
trap 'rm -f "$OWNER"' EXIT
TARGETS=("$@")
[ ${#TARGETS[@]} -eq 0 ] && TARGETS=(tests)

VERSIONS=(3.10 3.11 3.12 3.13 3.14 3.15)

cores=$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 4)
concurrency=${MATRIX_CONCURRENCY:-3}
[ "$concurrency" -gt ${#VERSIONS[@]} ] && concurrency=${#VERSIONS[@]}
[ "$concurrency" -lt 1 ] && concurrency=1
default_jobs=$(( (cores - 1) / concurrency ))
[ "$default_jobs" -lt 1 ] && default_jobs=1
jobs=${MATRIX_JOBS:-$default_jobs}

echo "matrix: ${#VERSIONS[@]} versions, ${concurrency} at a time, ${jobs} workers each" \
     "($((concurrency * jobs)) of ${cores} cores)"

failed=0
start=0
while [ "$start" -lt ${#VERSIONS[@]} ]; do
  wave=("${VERSIONS[@]:$start:$concurrency}")
  pids=(); running=()
  for version in "${wave[@]}"; do
    case "$version" in
      3.14) python=$ROOT/.venv/bin/python ;;
      *)    python=$ROOT/.matrix/venv${version/./}/bin/python ;;
    esac
    if [ ! -x "$python" ]; then
      echo "skip $version: $python not found (run scripts/setup-test-matrix.sh)"
      continue
    fi
    echo "start $version -> $LOGS/$version.log"
    (
      echo "== $version | $(date +%H:%M:%S) | ${jobs} workers | ${TARGETS[*]}" \
           >"$LOGS/$version.log"
      "$python" -m pytest -q --no-cov -p no:cacheprovider -n "$jobs" \
        "${TARGETS[@]}" >>"$LOGS/$version.log" 2>&1
      echo $? >"$LOGS/$version.status"
    ) &
    pids+=($!); running+=("$version")
  done
  for index in "${!pids[@]}"; do
    wait "${pids[$index]}"
    version=${running[$index]}
    status=$(cat "$LOGS/$version.status" 2>/dev/null || echo "?")
    summary=$(grep -E "^[0-9]+ (passed|failed)|failed," "$LOGS/$version.log" | tail -1)
    printf '%-6s exit=%-3s %s\n' "$version" "$status" "$summary"
    [ "$status" = "0" ] || failed=1
  done
  start=$(( start + concurrency ))
done
exit "$failed"
