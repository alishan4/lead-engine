#!/usr/bin/env python3
"""
V3.6 -- imports external (ChatGPT/Gmail-side) result events from
data/outreach/outreach_results.jsonl (the local fallback inbox -- see
schemas/outreach_result_event.schema.json) and applies them idempotently to
the shared handoff queue via handoff_lib.apply_event.

Idempotent: every event is deduped against data/handoff/imported_events_log.jsonl
(keyed by handoff_lib.event_dedup_key, which specifically collapses the same
Gmail message/thread id + event_type regardless of exact timestamp
formatting -- "never duplicate the same Gmail message/thread event"). A
stale/out-of-order event (older than one already applied for that field) is
recorded as skipped, never applied, and never treated as an error.

V3.6.1: every event -- valid or not -- is processed inside its own isolated
try/except covering validation (handoff_lib.validate_event), dedup-key
computation, and application. A malformed/invalid event (missing lead_id,
missing event_type, an unrecognized event_type, a truncated non-dict row,
or any other unexpected shape) is recorded in `import_failures` and never
aborts or blocks processing of any other event in the batch.

A SUPPRESSED event also registers in the existing V3.3 suppression registry
(scripts/outreach_lib.py) so every OTHER stage of the pipeline (not just the
shared queue view) respects it on the next run -- this is the one place an
external event reaches back into Lead Engine's own state, and only ever
ADDS a suppression, never removes one.

This script NEVER sends anything and NEVER touches Gmail -- it only reads
a local JSONL file (or, for a configured remote backend, that backend's own
`import_results()`) and updates local JSON state.

Usage:
  python3 scripts/import_outreach_results.py
"""
import sys

from _lib import OUTREACH, read_jsonl, append_jsonl, load_yaml, now_iso
from handoff_backend import LocalFileBackend, build_backend, SharedHandoffAuthRequired
from handoff_lib import apply_event, event_dedup_key, validate_event
from outreach_lib import add_suppression, VALID_SUPPRESSION_REASONS

RESULTS_PATH = OUTREACH / "outreach_results.jsonl"


def _already_imported_keys(imported_log_path):
    seen = set()
    for e in read_jsonl(imported_log_path):
        seen.add(tuple(e.get("key") or []))
    return seen


def _register_suppression_if_needed(event):
    if event.get("event_type") != "SUPPRESSED":
        return
    reason = event.get("reason") if event.get("reason") in VALID_SUPPRESSION_REASONS else "MANUAL_SUPPRESSION"
    add_suppression(reason=reason, business_id=event.get("lead_id"),
                     source=event.get("source", "external_result_import"),
                     note=event.get("note") or (f"raw external reason: {event.get('reason')}" if event.get("reason") else ""))


def import_results(logfn=print):
    cfg = load_yaml("handoff.yaml")
    local = LocalFileBackend(cfg)
    imported_log_path = local.dir / cfg["local_backend"]["imported_events_log_file"]

    events = read_jsonl(RESULTS_PATH)
    remote_name = cfg.get("backend", "local")
    if remote_name != "local":
        try:
            remote = build_backend(cfg)
            events = events + list(remote.import_results())
        except SharedHandoffAuthRequired as e:
            logfn(f"SHARED_HANDOFF_AUTH_REQUIRED while reading remote results -- {e} "
                  "Continuing with local outreach_results.jsonl only.")

    already = _already_imported_keys(imported_log_path)
    rows = local.all_rows()
    updated_rows = {}
    applied, skipped_duplicate, skipped_stale, skipped_missing, rejected_invalid, failures = 0, 0, 0, 0, 0, []

    # V3.6.1: EVERY per-event step -- validation, dedup-key computation,
    # lookup, and apply -- lives inside this one try/except, so a single
    # malformed/invalid event (missing lead_id, missing event_type,
    # unrecognized event_type, a truncated non-dict row, or any other
    # unexpected shape) is isolated as its own failure and never aborts or
    # blocks processing of any other event in the batch.
    for event in events:
        try:
            valid, reason = validate_event(event)
            if not valid:
                rejected_invalid += 1
                lead_id_hint = event.get("lead_id") if isinstance(event, dict) else None
                failures.append({"lead_id": lead_id_hint, "error": f"rejected: {reason}", "timestamp": now_iso()})
                logfn(f"  ! rejected invalid/malformed event ({reason}): {event!r}")
                continue  # deliberately NOT deduped -- an invalid row has no reliable identity to dedup on,
                          # and must keep surfacing as actionable every run until the source data is fixed

            key = event_dedup_key(event)
            if key in already:
                skipped_duplicate += 1
                continue  # exact duplicate (or same Gmail message/thread event) -- never re-applied

            lead_id = event["lead_id"]  # safe: validate_event() already confirmed this is a real string
            row = updated_rows.get(lead_id) or rows.get(lead_id)
            if row is None:
                skipped_missing += 1
                append_jsonl(imported_log_path, {"key": list(key), "event": event, "applied": False,
                                                   "reason": f"lead_id {lead_id!r} not found in shared queue", "imported_at": now_iso()})
                already.add(key)
                continue

            new_row, was_applied, apply_reason = apply_event(row, event)
            updated_rows[lead_id] = new_row
            append_jsonl(imported_log_path, {"key": list(key), "event": event, "applied": was_applied,
                                               "reason": apply_reason, "imported_at": now_iso()})
            already.add(key)
            if was_applied:
                applied += 1
                _register_suppression_if_needed(event)
                logfn(f"  + {lead_id}: {event['event_type']} applied")
            else:
                skipped_stale += 1
                logfn(f"  - {lead_id}: {event['event_type']} skipped -- {apply_reason}")
        except Exception as e:
            lead_id_hint = event.get("lead_id") if isinstance(event, dict) else None
            failures.append({"lead_id": lead_id_hint, "error": str(e)[:400], "timestamp": now_iso()})
            logfn(f"  ! {lead_id_hint}: import failed -- {e}")
            continue

    if updated_rows:
        local.update_rows(updated_rows)

    return {
        "events_seen": len(events), "events_applied": applied,
        "events_skipped_duplicate": skipped_duplicate, "events_skipped_stale": skipped_stale,
        "events_skipped_missing_lead": skipped_missing, "events_rejected_invalid": rejected_invalid,
        "import_failures": failures,
    }


def main():
    result = import_results()
    print(f"import_outreach_results: {result['events_applied']} applied, "
          f"{result['events_skipped_duplicate']} duplicate, {result['events_skipped_stale']} stale, "
          f"{result['events_skipped_missing_lead']} unknown lead_id, "
          f"{result['events_rejected_invalid']} rejected invalid, "
          f"{len(result['import_failures'])} failure(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
