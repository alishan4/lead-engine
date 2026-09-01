#!/usr/bin/env bash
# Lead Engine daily orchestration entrypoint. Wraps run_daily.py with a
# single-run lock (flock) so an overlapping systemd trigger (e.g. a
# previous run still finishing, or a manual + timer collision) can never
# run two orchestrations at once against the same data files.
#
# Usage:
#   scripts/run_daily.sh            # production run
#   scripts/run_daily.sh --dry-run  # validation run, writes to a DRY-RUN- summary file
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCK_DIR="$REPO_ROOT/data/runtime"
LOCK_FILE="$LOCK_DIR/run.lock"
mkdir -p "$LOCK_DIR"

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Lead Engine daily run already in progress (lock held on $LOCK_FILE) -- exiting." >&2
    exit 1
fi

cd "$REPO_ROOT"
python3 scripts/run_daily.py "$@"
