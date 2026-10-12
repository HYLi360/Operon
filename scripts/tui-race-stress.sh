#!/usr/bin/env bash
# tui-race-stress.sh [rounds] — run the TUI test suite under timing jitter.
#
# The project requires (AGENTS.md §5.7) three randomized-order runs of
# tests/unit/test_tui*.py after TUI changes; this wrapper is the way to do it.
# Random order comes from pytest-randomly (a test/dev dependency since it was
# introduced for exactly this); the jitter harness is scripts/tui_jitter.py,
# loaded here with the knobs the 2026-10 stress experiments found effective
# (see the plugin's docstring for what each knob widens).  All knobs can be
# overridden from the environment; OPERON_TUI_SEED defaults to the round
# number so each round samples different interleavings.
#
# Exit status is nonzero if any round failed.  A failure is a defect to
# register (defects.yml), not a flake to re-run.

set -u
cd "$(dirname "$0")/.."

ROUNDS="${1:-3}"
PY="${PYTHON:-.venv/bin/python}"
LOG_DIR="${TUI_RACE_LOG_DIR:-others/tui-race/runs}"
mkdir -p "$LOG_DIR"

export PYTHONPATH="$PWD/scripts${PYTHONPATH:+:$PYTHONPATH}"
export OPERON_TUI_JITTER_MS="${OPERON_TUI_JITTER_MS:-0.5}"
export OPERON_TUI_MOUNT_JITTER_MS="${OPERON_TUI_MOUNT_JITTER_MS:-30}"
export OPERON_TUI_FWD_JITTER_MS="${OPERON_TUI_FWD_JITTER_MS:-2}"
export OPERON_TUI_WORKER_JITTER_MS="${OPERON_TUI_WORKER_JITTER_MS:-50}"

rc_all=0
for round in $(seq 1 "$ROUNDS"); do
    log="$LOG_DIR/tui-race-round${round}.log"
    echo "=== round $round/$ROUNDS (jitter seed ${OPERON_TUI_SEED:-$round}) ==="
    OPERON_TUI_SEED="${OPERON_TUI_SEED:-$round}" \
        "$PY" -m pytest tests/unit/test_tui*.py -q --no-cov -p no:cacheprovider \
        -p tui_jitter >"$log" 2>&1
    rc=$?
    echo "round $round rc=$rc : $(tail -1 "$log")"
    [ "$rc" -eq 0 ] || rc_all=1
done
exit "$rc_all"
