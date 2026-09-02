#!/usr/bin/env python3
"""
V3.6 -- monthly-workbook-compatible CSV exports. A reporting MIRROR only:
nothing here is a transactional path, and nothing here ever mutates an
.xlsx file directly (see OPERATING-RULES.md Sec.2 -- the monthly tracker is
never authoritative over Lead Engine or Gmail state). Safe to regenerate
from scratch on every run; never hand-edited.

Writes (config/handoff.yaml: tracker, under the same gitignored
data/handoff/ directory as the shared queue):
  leads_master.csv    -- one row per prospect ever discovered
  outreach_log.csv     -- one row per shared-queue lead (both queues)
  follow_up_queue.csv  -- shared-queue rows currently follow_up_state == FOLLOW_UP_DUE
  daily_pipeline.csv   -- one row per daily run summary on disk

Usage:
  python3 scripts/export_tracker_csv.py
"""
import csv
import sys

from pathlib import Path

from _lib import PROSPECTS, DATA, read_jsonl, load_yaml
from handoff_backend import LocalFileBackend
from handoff_lib import COLUMNS

LEADS_MASTER_COLUMNS = (
    "id", "business_name", "niche", "city", "state", "status",
    "fit_confirmed_score", "fit_potential_score", "gap_confirmed_score", "gap_potential_score",
    "qualification_tier", "primary_wedge_type", "why_now", "discovered_at", "last_audited_at",
)

DAILY_PIPELINE_COLUMNS = (
    "run_id", "date", "trigger_type", "claude_auth_status", "acquisition_run_completed",
    "pending_leads_processed", "fresh_candidates_discovered", "businesses_verified",
    "qualified", "high_priority", "rejected", "needs_enrichment", "ready_to_send",
    "worker_timeout", "handoff_sync_status",
)


def write_csv(path, columns, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c) for c in columns})


def export_leads_master(out_dir):
    prospects = read_jsonl(PROSPECTS / "discovered.jsonl")
    write_csv(out_dir / "leads_master.csv", LEADS_MASTER_COLUMNS, prospects)
    return len(prospects)


def export_outreach_log(out_dir, backend):
    rows = list(backend.all_rows().values())
    write_csv(out_dir / "outreach_log.csv", COLUMNS, rows)
    return len(rows)


def export_follow_up_queue(out_dir, backend):
    rows = [r for r in backend.all_rows().values() if r.get("follow_up_state") == "FOLLOW_UP_DUE"]
    write_csv(out_dir / "follow_up_queue.csv", COLUMNS, rows)
    return len(rows)


def export_daily_pipeline(out_dir):
    runs_dir = DATA / "runtime" / "daily_runs"
    rows = []
    if runs_dir.exists():
        for f in sorted(runs_dir.glob("*.json")):
            if f.name.startswith("DRY-RUN-"):
                continue  # a validation run is never counted as production pipeline history
            from _lib import load_json
            d = load_json(f) or {}
            d = dict(d)
            d["date"] = f.stem
            rows.append(d)
    write_csv(out_dir / "daily_pipeline.csv", DAILY_PIPELINE_COLUMNS, rows)
    return len(rows)


def export_all(logfn=print):
    cfg = load_yaml("handoff.yaml")
    out_dir = DATA / Path(cfg["local_backend"]["dir"]).name  # see handoff_backend.LocalFileBackend -- same DATA-relative resolution
    backend = LocalFileBackend(cfg)

    counts = {
        "leads_master_rows": export_leads_master(out_dir),
        "outreach_log_rows": export_outreach_log(out_dir, backend),
        "follow_up_queue_rows": export_follow_up_queue(out_dir, backend),
        "daily_pipeline_rows": export_daily_pipeline(out_dir),
    }
    logfn(f"export_tracker_csv: {counts}")
    return counts


def main():
    export_all()
    return 0


if __name__ == "__main__":
    sys.exit(main())
