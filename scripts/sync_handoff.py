#!/usr/bin/env python3
"""
V3.6 -- builds the two shared queues (EMAIL_READY / CONTACT_FORM_READY)
from data/outreach/ready_to_send.jsonl + each lead's local artifacts, and
syncs them to the configured backend (config/handoff.yaml).

Always writes to the local backend first (data/handoff/ -- zero
credentials, always available) as the durable merge basis and safety net,
then ALSO pushes to the configured remote backend if it differs from
"local". A remote failure (SharedHandoffAuthRequired or anything else) is
recorded, never raised past this script -- the local queue is already
written and remains authoritative; this mirrors section 16/14's fail-closed
requirement exactly.

One bad lead's row-building failure is recorded (lead_id, error, timestamp)
and never blocks the rest -- see handoff_row_failures in the returned dict.

This script NEVER sends anything and NEVER touches Gmail.

Usage:
  python3 scripts/sync_handoff.py
"""
import sys

from _lib import PROSPECTS, LEADS, OUTREACH, read_jsonl, load_json, load_yaml, now_iso
from handoff_backend import build_backend, LocalFileBackend, SharedHandoffAuthRequired
from handoff_lib import is_eligible_for_export, build_lead_engine_fields, merge_row, queue_for_channel


def get_prospect(pid, discovered=None):
    discovered = discovered if discovered is not None else read_jsonl(PROSPECTS / "discovered.jsonl")
    return next((r for r in discovered if r["id"] == pid), None)


def build_rows(logfn=lambda m: None):
    """Returns (email_rows, form_rows, row_failures, skipped_count)."""
    cfg = load_yaml("handoff.yaml")
    limits_cfg = load_yaml("limits.yaml")
    ready_rows = read_jsonl(OUTREACH / "ready_to_send.jsonl")
    discovered = read_jsonl(PROSPECTS / "discovered.jsonl")

    local_backend = LocalFileBackend(cfg)
    existing = local_backend.all_rows()

    email_rows, form_rows, failures = [], [], []
    skipped = 0
    for r in ready_rows:
        pid = r.get("prospect_id")
        try:
            prospect = get_prospect(pid, discovered)
            if not prospect:
                skipped += 1
                continue
            ldir = LEADS / pid
            contact = load_json(ldir / "contact_record.json")
            asset = load_json(ldir / "staged_asset.json")
            wedge = load_json(ldir / "primary_wedge.json")
            dossier = load_json(ldir / "intelligence_dossier.json")
            window = load_json(ldir / "send_window.json")
            draft = load_json(ldir / "email_draft.json")

            eligible, reason = is_eligible_for_export(prospect, contact, window, draft, dossier, limits_cfg)
            if not eligible:
                skipped += 1
                logfn(f"  - {pid}: not exported to shared queue -- {reason}")
                continue

            fields = build_lead_engine_fields(prospect, r, contact, asset, wedge, dossier, limits_cfg)
            merged = merge_row(existing.get(pid), fields)
            queue = queue_for_channel(fields["preferred_channel"])
            (email_rows if queue == "EMAIL_READY" else form_rows).append(merged)
        except Exception as e:
            failures.append({"lead_id": pid, "error": str(e)[:400], "timestamp": now_iso()})
            logfn(f"  ! {pid}: row-build failed -- {e}")
            continue

    return email_rows, form_rows, failures, skipped


def sync(logfn=print):
    cfg = load_yaml("handoff.yaml")
    email_rows, form_rows, row_failures, skipped = build_rows(logfn)

    result = {
        "handoff_sync_status": "SYNCED",
        "email_ready_rows": len(email_rows),
        "contact_form_ready_rows": len(form_rows),
        "row_failures": row_failures,
        "skipped_ineligible": skipped,
        "backend": cfg.get("backend", "local"),
        "synced_at": now_iso(),
    }

    # Always write local first -- the durable safety net and merge basis.
    local_backend = LocalFileBackend(cfg)
    local_backend.export_ready(email_rows, form_rows)
    logfn(f"Local handoff queue updated: {len(email_rows)} EMAIL_READY, {len(form_rows)} CONTACT_FORM_READY "
          f"({skipped} lead(s) not yet eligible, {len(row_failures)} row-build failure(s)).")

    remote_name = cfg.get("backend", "local")
    if remote_name != "local":
        try:
            remote_backend = build_backend(cfg)
            remote_backend.export_ready(email_rows, form_rows)
            logfn(f"Remote handoff backend ({remote_name}) synced successfully.")
        except SharedHandoffAuthRequired as e:
            result["handoff_sync_status"] = "SHARED_HANDOFF_AUTH_REQUIRED"
            result["remote_sync_error"] = str(e)
            logfn(f"SHARED_HANDOFF_AUTH_REQUIRED -- {e} Local queue remains authoritative.")
        except Exception as e:
            result["handoff_sync_status"] = "HANDOFF_SYNC_FAILED"
            result["remote_sync_error"] = str(e)[:400]
            logfn(f"HANDOFF_SYNC_FAILED -- remote backend error: {e}. Local queue remains authoritative, nothing lost.")

    return result


def main():
    result = sync()
    print(f"sync_handoff: {result['handoff_sync_status']} -- "
          f"{result['email_ready_rows']} EMAIL_READY, {result['contact_form_ready_rows']} CONTACT_FORM_READY, "
          f"{len(result['row_failures'])} row failure(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
