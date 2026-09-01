#!/usr/bin/env python3
"""
V3.3 send executor. THIS MODULE NEVER SENDS REAL EMAIL.

`dry_run_send()` is the only send path that actually runs code in this
project. It re-verifies every gate immediately before "sending" (time has
passed since QA -- suppression, the account lock, and daily volume could
all have changed), then simulates a send: no network call, no Gmail API
call, nothing --  it writes a local dry-run record and advances state.

`production_send_DESIGNED_NOT_IMPLEMENTED()` documents the shape a real
send would need (same contract, minus the dry-run substitution) but is a
stub that always raises. There is no code path anywhere in this repository
that can turn dry_run_send's simulation into a real Gmail send -- doing
that is explicitly out of scope for V3.3 and requires a separate, later,
explicitly-authorized implementation step.

Usage:
  python3 scripts/send_executor.py --id <slug> --dry-run
  (--dry-run is required and is the only accepted mode; omitting it is an error)
"""
import argparse

from _lib import PROSPECTS, read_jsonl, lead_dir, load_json, write_json, load_yaml, set_status_everywhere, now_iso, content_hash
from outreach_lib import is_suppressed, account_lock_check, register_touch, record_event, SEND_LOG_PATH, load_suppression
from _lib import append_jsonl

ENTRY_STATUS = "SEND_WINDOW_PLANNED"


def _today_dry_run_count():
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).date().isoformat()
    from _lib import read_jsonl
    return sum(1 for r in read_jsonl(SEND_LOG_PATH) if (r.get("attempted_at") or "").startswith(today))


def check_send_contract(prospect_id, p, contact, draft, cfg, daily_count_fn=_today_dry_run_count):
    """
    Pure-ish decision function (one injected side-effecting count fn for
    testability): returns (allowed: bool, reason: str). Every one of these
    is re-checked here even though QA already checked most of them, because
    real time has elapsed since QA_PASS and any of them could have changed.
    """
    if p.get("status") != ENTRY_STATUS:
        return False, f"status is {p.get('status')!r}, not {ENTRY_STATUS}"

    suppressed, sup = is_suppressed(email=(contact["channel"] or {}).get("address_or_url"), business_id=prospect_id)
    if suppressed:
        return False, f"suppressed since QA: {sup.get('reason')}"

    allowed, lock_reason = account_lock_check(prospect_id)
    if not allowed:
        return False, f"account lock: {lock_reason}"

    if contact["overall_status"] not in ("CONTACT_VERIFIED", "CONTACT_FORM_READY"):
        return False, f"contact overall_status regressed to {contact['overall_status']!r}"

    if not draft or not draft.get("body"):
        return False, "no email draft on record"

    stage = cfg["volume"]["current_stage"]
    ceiling = cfg["volume"][{1: "daily_ceiling_initial", 2: "daily_ceiling_stage_2", 3: "daily_ceiling_stage_3"}[stage]]
    sent_today = daily_count_fn()
    if sent_today >= ceiling:
        return False, f"daily volume ceiling reached ({sent_today}/{ceiling} for stage {stage}) -- a ceiling, not a quota; this is a valid stop, not a bug"

    return True, "all send-contract checks passed"


def dry_run_send(prospect_id, p, contact, draft, cfg, daily_count_fn=_today_dry_run_count):
    """
    Returns (record_or_None, reason). Never calls a real transport. The
    returned record's message_id is a deterministic local placeholder
    (DRYRUN-<hash>), never mistakable for a real Gmail message id.
    """
    allowed, reason = check_send_contract(prospect_id, p, contact, draft, cfg, daily_count_fn)
    if not allowed:
        return None, reason

    message_id = f"DRYRUN-{content_hash(prospect_id, draft.get('content_hash', ''), now_iso())}"
    record = {
        "prospect_id": prospect_id, "message_id": message_id, "dry_run": True,
        "recipient_channel": contact["channel"], "subject": draft["subject"],
        "attempted_at": now_iso(), "simulated_outcome": "SENT",
    }
    return record, "dry-run send simulated -- no real email was sent"


def production_send_DESIGNED_NOT_IMPLEMENTED(*args, **kwargs):
    """
    Documents the production send contract's shape without providing any
    working path to it. Calling this always raises. Turning this into a
    real send is explicitly OUT OF SCOPE for V3.3 and requires a separate,
    later, explicitly-authorized implementation step -- per the critical
    safety rule: "Production sending requires a separate explicit execution
    step."

    The real contract, when eventually implemented, must run every check in
    check_send_contract() PLUS a live Gmail-account health/quota check, and
    must require an explicit, out-of-band authorization flag that does not
    exist anywhere in this codebase today.
    """
    raise NotImplementedError(
        "Production sending is not implemented in V3.3 by explicit design. "
        "Use dry_run_send() for all testing and validation."
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    ap.add_argument("--dry-run", action="store_true", required=True,
                     help="Required. This executor has no other mode.")
    args = ap.parse_args()

    p = next((r for r in read_jsonl(PROSPECTS / "discovered.jsonl") if r["id"] == args.id), None)
    if not p:
        raise SystemExit(f"Prospect {args.id} not found in discovered.jsonl")

    contact = load_json(lead_dir(args.id) / "contact_record.json")
    draft = load_json(lead_dir(args.id) / "email_draft.json")
    cfg = load_yaml("outreach.yaml")

    record_event(args.id, "SEND_ATTEMPTED", p.get("status"), p.get("status"), "entering send contract (dry-run)")

    record, reason = dry_run_send(args.id, p, contact, draft, cfg)
    if record is None:
        # No status transition actually happens on a block -- the prospect
        # stays exactly where it was. The audit log must reflect that
        # reality, not a guessed/fabricated destination state.
        record_event(args.id, "SEND_BLOCKED", p.get("status"), p.get("status"), reason)
        print(f"{args.id}: send blocked -- {reason}")
        return

    append_jsonl(SEND_LOG_PATH, record)
    set_status_everywhere(args.id, "DRY_RUN_SENT")
    register_touch(args.id, p.get("website"), "FIRST_TOUCH_DRY_RUN", message_id=record["message_id"])
    record_event(args.id, "DRY_RUN_SENT", p.get("status"), "DRY_RUN_SENT", reason,
                 evidence_refs=[record["message_id"]], dry_run=True)
    print(f"{args.id}: DRY_RUN_SENT (message_id={record['message_id']}) -- no real email was sent.")


if __name__ == "__main__":
    main()
