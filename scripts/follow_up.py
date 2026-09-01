#!/usr/bin/env python3
"""
V3.3 follow-up sequence. Day 0 (the original send) + up to
len(day_offsets)-1 further touches (config/outreach.yaml: follow_up).
Every touch must add real new value -- there is no "just checking in"
template. If no new evidence exists for a given touch, the honest outcome
is to NOT send that touch, not to fabricate one.

Usage:
  python3 scripts/follow_up.py --id <slug> --check                       # is a touch due?
  python3 scripts/follow_up.py --id <slug> --new-evidence evidence.json  # generate + gate a touch
"""
import argparse
import json

from _lib import PROSPECTS, read_jsonl, lead_dir, load_json, write_json, load_yaml, set_status_everywhere, now_iso, days_since
from outreach_lib import is_suppressed, get_account, register_touch, record_event

ENTRY_STATUSES = ("NO_BOUNCE_DETECTED", "FOLLOW_UP_DUE")


def next_touch_index(first_touch_at, touches_sent_so_far, day_offsets):
    """
    Pure: touches_sent_so_far counts the original Day-0 send as touch 1.
    Returns the index into day_offsets of the NEXT follow-up due (>=1), or
    None if the next scheduled day hasn't arrived yet, or if the sequence
    is already exhausted (touches_sent_so_far >= len(day_offsets)).
    """
    if touches_sent_so_far >= len(day_offsets):
        return None
    elapsed = days_since(first_touch_at)
    if elapsed is None:
        return None
    next_index = touches_sent_so_far  # 1 sent -> next is index 1, etc.
    if elapsed >= day_offsets[next_index]:
        return next_index
    return None


def follow_up_gate(prospect_id, email, domain):
    """Pure-ish: re-reconcile reply/bounce/suppression immediately before
    generating a follow-up. Returns (allowed: bool, reason: str)."""
    suppressed, sup = is_suppressed(email=email, domain=domain, business_id=prospect_id)
    if suppressed:
        return False, f"suppressed: {sup.get('reason')}"
    acct = get_account(prospect_id, domain)
    if acct and acct.get("state") in ("REPLIED", "CLOSED", "SUPPRESSED"):
        return False, f"account state is {acct.get('state')} -- sequence already resolved, no further follow-up"
    return True, "gate passed"


def build_follow_up_content(touch_index, wedge, new_evidence):
    """
    Pure: requires an explicit new_evidence item (dict with at least
    'observation' and 'source') distinct from the original wedge observation.
    Returns (content_dict_or_None, reason). Refuses to manufacture a
    generic check-in when no real new value exists for this touch.
    """
    if not new_evidence or not new_evidence.get("observation"):
        return None, ("no new value-add evidence supplied for this touch -- per the "
                       "no-generic-check-in rule, this follow-up should NOT be sent")
    if wedge and new_evidence["observation"].strip() == (wedge.get("observation") or "").strip():
        return None, "new_evidence is identical to the original wedge observation -- that is not new value"
    return {
        "touch_index": touch_index,
        "body": f"Quick follow-up -- one more specific thing I noticed: {new_evidence['observation']}",
        "evidence_source": new_evidence.get("source"),
    }, "new value-add content built"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--new-evidence", default=None)
    args = ap.parse_args()

    p = next((r for r in read_jsonl(PROSPECTS / "discovered.jsonl") if r["id"] == args.id), None)
    if not p:
        raise SystemExit(f"Prospect {args.id} not found in discovered.jsonl")
    if p.get("status") not in ENTRY_STATUSES:
        raise SystemExit(f"{args.id}: status is {p.get('status')!r}, not in {ENTRY_STATUSES}.")

    cfg = load_yaml("outreach.yaml")["follow_up"]
    account = get_account(args.id)
    first_touch_at = (account or {}).get("first_touch_at")
    touches_sent = (account or {}).get("touch_count", 0)
    touch_idx = next_touch_index(first_touch_at, touches_sent, cfg["day_offsets"])

    if args.check:
        if touch_idx is None:
            print(f"{args.id}: no follow-up due yet (first_touch_at={first_touch_at})")
        else:
            set_status_everywhere(args.id, "FOLLOW_UP_DUE")
            record_event(args.id, "FOLLOW_UP_DUE", p.get("status"), "FOLLOW_UP_DUE", f"touch #{touch_idx} due")
            print(f"{args.id}: FOLLOW_UP_DUE (touch #{touch_idx})")
        return

    contact = load_json(lead_dir(args.id) / "contact_record.json") or {}
    email = (contact.get("channel") or {}).get("address_or_url")
    allowed, reason = follow_up_gate(args.id, email, p.get("website"))
    if not allowed:
        record_event(args.id, "FOLLOW_UP_BLOCKED", p.get("status"), p.get("status"), reason)
        print(f"{args.id}: follow-up blocked -- {reason}")
        return

    if touch_idx is None:
        if touches_sent >= len(cfg["day_offsets"]):
            set_status_everywhere(args.id, "CLOSED", extra_fields={"closed_reason": "SEQUENCE_EXHAUSTED_NO_REPLY"})
            register_touch(args.id, p.get("website"), "CLOSED", extra={"closed_reason": "SEQUENCE_EXHAUSTED_NO_REPLY"})
            record_event(args.id, "SEQUENCE_CLOSED", p.get("status"), "CLOSED", "follow-up sequence exhausted with no reply")
            print(f"{args.id}: CLOSED -- follow-up sequence exhausted with no reply.")
        else:
            print(f"{args.id}: no follow-up due yet (first_touch_at={first_touch_at}, touches_sent={touches_sent})")
        return

    wedge = load_json(lead_dir(args.id) / "primary_wedge.json")
    new_evidence = json.loads(open(args.new_evidence).read()) if args.new_evidence else None
    content, content_reason = build_follow_up_content(touch_idx, wedge, new_evidence)
    if content is None:
        record_event(args.id, "FOLLOW_UP_SKIPPED", p.get("status"), p.get("status"), content_reason)
        print(f"{args.id}: follow-up NOT sent -- {content_reason}")
        return

    write_json(lead_dir(args.id) / f"follow_up_{touch_idx}.json", content)
    register_touch(args.id, p.get("website"), "FOLLOW_UP")
    record_event(args.id, "FOLLOW_UP_SENT_DRY_RUN", p.get("status"), "AWAITING_REPLY", content_reason, dry_run=True)
    print(f"{args.id}: follow-up #{touch_idx} prepared (dry-run) -- {content_reason}")


if __name__ == "__main__":
    main()
