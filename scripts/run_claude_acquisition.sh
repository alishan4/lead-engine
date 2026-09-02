#!/usr/bin/env bash
# V3.5 -- manual/validation entrypoint for the unattended Claude acquisition
# worker's RESEARCH PHASE ONLY (no deterministic finalization, no
# READY_TO_SEND export, no dated daily-summary write). Use this for a
# controlled --max-prospects validation run (ideally against a
# LEAD_ENGINE_DATA_DIR sandbox, never production data).
#
# For an actual same-day catch-up PRODUCTION cycle -- the full chain
# (acquisition worker -> deterministic finalization -> READY_TO_SEND export
# -> reports -> dated run summary, so catchup.py's idempotency check has
# something to read next time) -- use run_daily.sh directly instead, exactly
# as the timer itself would, just invoked manually:
#   scripts/run_daily.sh --trigger-type SAME_DAY_CATCH_UP
# (run_daily.py calls scripts/acquisition_worker.py's run() function
# in-process either way -- one acquisition-worker implementation, reached
# through either entrypoint.)
#
# Usage:
#   scripts/run_claude_acquisition.sh                        # auto-detect trigger_type via catchup.py
#   scripts/run_claude_acquisition.sh SAME_DAY_CATCH_UP       # explicit trigger_type
#   scripts/run_claude_acquisition.sh "" --max-prospects 2    # controlled validation run (research phase only)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCK_DIR="$REPO_ROOT/data/runtime"
LOCK_FILE="$LOCK_DIR/run.lock"
mkdir -p "$LOCK_DIR"

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] RUN_ALREADY_ACTIVE -- run.lock held, exiting without touching state." >&2
    exit 1
fi

cd "$REPO_ROOT"

TRIGGER_TYPE="${1:-}"
shift || true

if [ -z "$TRIGGER_TYPE" ]; then
    DECISION_LINE="$(python3 scripts/catchup.py)"
    TRIGGER_TYPE="$(echo "$DECISION_LINE" | awk '{print $1}')"
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] catchup.py decision: $DECISION_LINE"
fi

case "$TRIGGER_TYPE" in
  RUN_ALREADY_ACTIVE|ALREADY_COMPLETED_TODAY|MISSED_ACQUISITION_WINDOW)
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $TRIGGER_TYPE -- not starting an acquisition cycle."
    exit 0
    ;;
esac

python3 scripts/acquisition_worker.py --trigger-type "$TRIGGER_TYPE" "$@"
