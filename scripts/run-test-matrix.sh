#!/usr/bin/env bash
# Local cross-version test matrix (Linux + uv-managed CPython 3.10-3.14).
#
# Usage:
#   scripts/run-test-matrix.sh                  # full suite, all versions, parallel
#   scripts/run-test-matrix.sh tests/unit -x    # extra pytest args are forwarded
#   MATRIX_JOBS=2 scripts/run-test-matrix.sh    # pytest-xdist workers per version
#
# Each version runs in its own process; logs land in .matrix/logs/<version>.log
# and the exit status in .matrix/logs/<version>.status, so the whole matrix
# finishes in about the time of one parallel run instead of five serial ones.
set -u
cd "$(dirname "$0")/.."   # repository root
ROOT=$PWD
LOGS=$ROOT/.matrix/logs
mkdir -p "$LOGS"
JOBS=${MATRIX_JOBS:-4}
TARGETS=("$@")
[ ${#TARGETS[@]} -eq 0 ] && TARGETS=(tests)

pids=(); versions=()
for version in 3.10 3.11 3.12 3.13 3.14; do
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
    "$python" -m pytest -q --no-cov -p no:cacheprovider -n "$JOBS" --dist loadfile \
      "${TARGETS[@]}" >"$LOGS/$version.log" 2>&1
    echo $? >"$LOGS/$version.status"
  ) &
  pids+=($!); versions+=("$version")
done

failed=0
for index in "${!pids[@]}"; do
  wait "${pids[$index]}"
  status=$(cat "$LOGS/${versions[$index]}.status" 2>/dev/null || echo "?")
  summary=$(grep -E "^[0-9]+ (passed|failed)|failed," "$LOGS/${versions[$index]}.log" | tail -1)
  printf '%-6s exit=%-3s %s\n' "${versions[$index]}" "$status" "$summary"
  [ "$status" = "0" ] || failed=1
done
exit "$failed"
