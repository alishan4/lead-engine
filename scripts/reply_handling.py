#!/usr/bin/env python3
"""
V3.3 reply classification, human handoff packaging, and recycle policy.

Reply classification is deterministic keyword matching, deliberately
conservative: only a literal, unambiguous phrase match yields POSITIVE or
UNSUBSCRIBE/NEGATIVE. Anything else -- including anything merely "possibly
positive" -- resolves to UNKNOWN and routes to a human, never to an
automated positive interpretation. This is the explicit guard against an
LLM (or any classifier) aggressively reading ambiguity as interest.

Usage:
  python3 scripts/reply_handling.py --id <slug> --reply-text "..."
  python3 scripts/reply_handling.py --id <slug> --check-recycle --new-trigger-signal runs_google_ads
"""
import argparse

from _lib import PROSPECTS, read_jsonl, lead_dir, load_json, write_json, load_yaml, set_status_everywhere, now_iso, days_since
from outreach_lib import add_suppression, register_touch, record_event, normalize_domain

UNSUBSCRIBE_PHRASES = ("unsubscribe", "remove me", "stop emailing", "take me off", "opt out", "opt-out")
NEGATIVE_PHRASES = ("not interested", "no thanks", "no thank you", "please stop", "not looking", "we're all set", "we are all set")
POSITIVE_PHRASES = ("tell me more", "sounds good", "let's talk", "lets talk", "schedule a call",
                     "yes let's", "send me more info", "interested, tell me", "i'm interested", "im interested")


def classify_reply(text):
    """Pure: returns one of POSITIVE/NEUTRAL/NEGATIVE/UNSUBSCRIBE/UNKNOWN.
    Order matters: unsubscribe/negative checked before positive so a reply
    like "not interested, please remove me" isn't miscounted as positive."""
    low = (text or "").strip().lower()
    if not low:
        return "UNKNOWN"
    if any(p in low for p in UNSUBSCRIBE_PHRASES):
        return "UNSUBSCRIBE"
    if any(p in low for p in NEGATIVE_PHRASES):
        return "NEGATIVE"
    if any(p in low for p in POSITIVE_PHRASES):
        return "POSITIVE"
    if len(low.split()) <= 3:
        return "NEUTRAL"  # short acknowledgements ("thanks", "got it") -- not a signal either way
    return "UNKNOWN"  # ambiguous/longer text never auto-resolves to POSITIVE


def build_human_handoff(prospect_id, p, dossier, wedge, contact, reply_text, classification):
    return {
        "prospect_id": prospect_id,
        "business_name": p.get("business_name"),
        "reply_text": reply_text,
        "classification": classification,
        "primary_wedge": wedge,
        "contact": contact,
        "dossier_summary": {
            "fit": (dossier or {}).get("qualification", {}).get("fit"),
            "gap": (dossier or {}).get("qualification", {}).get("gap"),
        },
        "recommended_next_step": {
            "POSITIVE": "Schedule intro call -- lead has expressed explicit interest.",
            "NEGATIVE": "No further outreach -- respect the decline.",
            "UNSUBSCRIBE": "Already suppressed -- confirm no further contact occurs.",
            "NEUTRAL": "Low-signal short reply -- human judgment call on whether to continue sequence.",
            "UNKNOWN": "Ambiguous reply -- requires human read before any further action.",
        }[classification],
        "generated_at": now_iso(),
    }


def recycle_eligible(closed_at, min_days_elapsed, new_trigger_signal_type, valid_trigger_types):
    """
    Pure: time elapsed alone is never sufficient. Requires BOTH the minimum
    elapsed window AND an explicitly-supplied fresh trigger signal type that
    is on the valid list -- no automatic scan for "something changed."
    """
    elapsed = days_since(closed_at)
    if elapsed is None or elapsed < min_days_elapsed:
        return False, f"only {elapsed if elapsed is not None else '?'} days elapsed, needs >= {min_days_elapsed}"
    if not new_trigger_signal_type or new_trigger_signal_type not in valid_trigger_types:
        return False, "no valid new trigger signal supplied -- time alone never justifies recycling a closed lead"
    return True, f"{elapsed:.0f} days elapsed and a valid new trigger ({new_trigger_signal_type}) was supplied"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    ap.add_argument("--reply-text", default=None)
    ap.add_argument("--check-recycle", action="store_true")
    ap.add_argument("--new-trigger-signal", default=None)
    args = ap.parse_args()

    p = next((r for r in read_jsonl(PROSPECTS / "discovered.jsonl") if r["id"] == args.id), None)
    if not p:
        raise SystemExit(f"Prospect {args.id} not found in discovered.jsonl")

    cfg = load_yaml("outreach.yaml")

    if args.check_recycle:
        from outreach_lib import get_account
        acct = get_account(args.id) or {}
        eligible, reason = recycle_eligible(acct.get("last_touch_at"), cfg["recycle"]["min_days_elapsed"],
                                             args.new_trigger_signal, cfg["recycle"]["valid_trigger_signal_types"])
        if eligible:
            register_touch(args.id, p.get("website"), "CLOSED", extra={"closed_reason": "RECYCLE_ELIGIBLE"})
            record_event(args.id, "RECYCLE_ELIGIBLE", p.get("status"), "RECYCLE_ELIGIBLE", reason)
            print(f"{args.id}: RECYCLE_ELIGIBLE -- {reason}")
        else:
            print(f"{args.id}: not recycle-eligible -- {reason}")
        return

    if args.reply_text is None:
        raise SystemExit("--reply-text is required unless --check-recycle is used")

    classification = classify_reply(args.reply_text)
    contact = load_json(lead_dir(args.id) / "contact_record.json")
    dossier = load_json(lead_dir(args.id) / "intelligence_dossier.json")
    wedge = load_json(lead_dir(args.id) / "primary_wedge.json")

    from outreach_lib import register_touch as _rt  # local alias, no behavior change
    register_touch(args.id, p.get("website"), "REPLY_RECEIVED")

    if classification == "UNSUBSCRIBE":
        email = (contact or {}).get("channel", {}).get("address_or_url")
        add_suppression("UNSUBSCRIBE", email=email, domain=normalize_domain(p.get("website")), business_id=args.id,
                         source="reply_handling", note=args.reply_text)
        set_status_everywhere(args.id, "SUPPRESSED")
        record_event(args.id, "REPLY_CLASSIFIED", p.get("status"), "SUPPRESSED", "UNSUBSCRIBE reply -- suppressed")
        print(f"{args.id}: SUPPRESSED (unsubscribe reply)")
        return

    if classification == "NEGATIVE":
        set_status_everywhere(args.id, "CLOSED", extra_fields={"closed_reason": "NEGATIVE_REPLY"})
        record_event(args.id, "REPLY_CLASSIFIED", p.get("status"), "CLOSED", "NEGATIVE reply")
        print(f"{args.id}: CLOSED (negative reply)")
        return

    if classification == "POSITIVE":
        handoff = build_human_handoff(args.id, p, dossier, wedge, contact, args.reply_text, classification)
        write_json(lead_dir(args.id) / "human_handoff.json", handoff)
        set_status_everywhere(args.id, "REPLIED")
        record_event(args.id, "REPLY_CLASSIFIED", p.get("status"), "REPLIED", "POSITIVE reply -- human handoff created")
        print(f"{args.id}: REPLIED -- human handoff package written (POSITIVE)")
        return

    # NEUTRAL / UNKNOWN both route to a human -- never auto-progressed further
    handoff = build_human_handoff(args.id, p, dossier, wedge, contact, args.reply_text, classification)
    write_json(lead_dir(args.id) / "human_handoff.json", handoff)
    set_status_everywhere(args.id, "HUMAN_REVIEW")
    record_event(args.id, "REPLY_CLASSIFIED", p.get("status"), "HUMAN_REVIEW", f"{classification} reply -- routed to human")
    print(f"{args.id}: HUMAN_REVIEW ({classification} reply)")


if __name__ == "__main__":
    main()
