#!/usr/bin/env python3
"""
V3.3 delivery reconciliation + deliverability health (circuit breaker).

Delivery outcomes are NEVER inferred or assumed -- absent a real, injected
delivery signal (a DSN/bounce record, or an explicit "checked the thread,
no bounce" confirmation from a real mailbox check), a message stays in
DELIVERY_CHECK indefinitely. That is a correct, honest state, not a bug:
this project has no live Gmail integration, so for real leads during V3.3
validation, DELIVERY_CHECK is the expected terminal state of the dry run.

Usage:
  python3 scripts/delivery_reconciliation.py --id <slug> [--evidence '{"bounce": true, "bounce_type": "hard", "detail": "...", "source": "..."}']
"""
import argparse
import json

from _lib import (PROSPECTS, read_jsonl, lead_dir, load_json, set_status_everywhere, now_iso, append_jsonl)
from outreach_lib import add_suppression, register_touch, record_event, DELIVERABILITY_PATH, normalize_domain

ENTRY_STATUS = "DRY_RUN_SENT"


def resolve_delivery(evidence):
    """
    Pure: evidence dict (or None) -> (status, reason). status is one of
    "NO_BOUNCE_DETECTED", "DELIVERY_FAILED", or None (stay in DELIVERY_CHECK,
    i.e. genuinely unknown).
    """
    if not evidence or evidence.get("bounce") is None:
        return None, "no delivery signal available yet -- remaining in DELIVERY_CHECK (never assumed)"
    if evidence.get("bounce") is True:
        return "DELIVERY_FAILED", evidence.get("detail") or "bounce signal received"
    return "NO_BOUNCE_DETECTED", evidence.get("detail") or "explicit no-bounce confirmation received"


def handle_bounce(prospect_id, email, domain, bounce_type, detail=""):
    """A hard bounce permanently suppresses and cancels any pending follow-up
    sequence; a soft bounce alone does not suppress (transient mail issues
    happen) but does force a re-verification before any further send."""
    if bounce_type == "hard":
        add_suppression("BOUNCE", email=email, domain=domain, business_id=prospect_id,
                         source="delivery_reconciliation", note=detail)
        register_touch(prospect_id, domain, "SUPPRESSED")
        return "CONTACT_REVERIFY_REQUIRED", True
    return "CONTACT_REVERIFY_REQUIRED", False


def compute_deliverability_health(events, cfg):
    """
    Pure: rolling stats over recorded delivery events -> health verdict.
    Refuses to draw a rate-based conclusion below the configured minimum
    sample size (a 1/3 bounce rate off three sends is noise, not a signal).
    """
    dcfg = cfg["deliverability"]
    attempts = [e for e in events if e.get("event") == "ATTEMPT"]
    hard_bounces = [e for e in events if e.get("event") == "BOUNCE" and e.get("bounce_type") == "hard"]
    abuse = [e for e in events if e.get("event") == "ABUSE_COMPLAINT"]

    if abuse and dcfg["abuse_complaint_triggers_immediate_pause"]:
        return {"status": "PAUSED", "reason": "abuse complaint recorded -- immediate pause regardless of sample size",
                "sample_size": len(attempts)}

    sample = len(attempts)
    if sample < dcfg["min_sample_for_rate_rules"]:
        return {"status": "HEALTHY", "reason": f"sample size {sample} below minimum {dcfg['min_sample_for_rate_rules']} -- rate rules not yet applicable",
                "sample_size": sample}

    rate = len(hard_bounces) / sample
    if rate >= dcfg["hard_bounce_pause_rate"]:
        return {"status": "PAUSED", "reason": f"hard bounce rate {rate:.1%} >= pause threshold {dcfg['hard_bounce_pause_rate']:.1%}",
                "sample_size": sample, "bounce_rate": rate}
    if rate >= dcfg["hard_bounce_warn_rate"]:
        return {"status": "WARNING", "reason": f"hard bounce rate {rate:.1%} >= warn threshold {dcfg['hard_bounce_warn_rate']:.1%}",
                "sample_size": sample, "bounce_rate": rate}
    return {"status": "HEALTHY", "reason": f"hard bounce rate {rate:.1%} within normal range", "sample_size": sample, "bounce_rate": rate}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    ap.add_argument("--evidence", default=None, help="JSON delivery evidence, or omit for 'no signal yet'")
    args = ap.parse_args()

    p = next((r for r in read_jsonl(PROSPECTS / "discovered.jsonl") if r["id"] == args.id), None)
    if not p:
        raise SystemExit(f"Prospect {args.id} not found in discovered.jsonl")
    if p.get("status") != ENTRY_STATUS:
        raise SystemExit(f"{args.id}: status is {p.get('status')!r}, not {ENTRY_STATUS}.")

    evidence = json.loads(args.evidence) if args.evidence else None
    append_jsonl(DELIVERABILITY_PATH, {"prospect_id": args.id, "event": "ATTEMPT", "recorded_at": now_iso()})

    status, reason = resolve_delivery(evidence)
    if status is None:
        set_status_everywhere(args.id, "DELIVERY_CHECK")
        record_event(args.id, "DELIVERY_CHECK_PENDING", p.get("status"), "DELIVERY_CHECK", reason)
        print(f"{args.id}: DELIVERY_CHECK (pending) -- {reason}")
        return

    if status == "DELIVERY_FAILED":
        contact = load_json(lead_dir(args.id) / "contact_record.json") or {}
        email = (contact.get("channel") or {}).get("address_or_url")
        bounce_type = evidence.get("bounce_type", "hard")
        append_jsonl(DELIVERABILITY_PATH, {"prospect_id": args.id, "event": "BOUNCE", "bounce_type": bounce_type, "recorded_at": now_iso()})
        next_status, suppressed = handle_bounce(args.id, email, normalize_domain(p.get("website")), bounce_type, reason)
        set_status_everywhere(args.id, next_status)
        record_event(args.id, "DELIVERY_FAILED", p.get("status"), next_status, reason,
                     extra={"suppressed": suppressed, "bounce_type": bounce_type})
        print(f"{args.id}: DELIVERY_FAILED -> {next_status} (suppressed={suppressed}) -- {reason}")
    else:
        set_status_everywhere(args.id, "NO_BOUNCE_DETECTED")
        record_event(args.id, "NO_BOUNCE_DETECTED", p.get("status"), "NO_BOUNCE_DETECTED", reason)
        print(f"{args.id}: NO_BOUNCE_DETECTED -- {reason}")


if __name__ == "__main__":
    main()
