#!/usr/bin/env python3
"""
V3.3 email generation contract. Deterministic templating from already-
validated, already-evidenced data (V3.2 primary_wedge.json + staged_asset.json
+ V3.3 contact_record.json) -- no new LLM call, consistent with the cost
philosophy established since V3.2's stage_asset.py. The only "creative" step
-- turning a raw observation into a one-sentence business implication -- is
a fixed per-opportunity-type template, never free-form generation, so it can
never invent a metric that isn't already in the evidence.

Usage:
  python3 scripts/generate_outreach_email.py --id <slug> [--sender-name NAME]
"""
import argparse

from _lib import (PROSPECTS, read_jsonl, lead_dir, load_json, write_json,
                   set_status_everywhere, now_iso, content_hash)
from outreach_lib import record_event

ENTRY_STATUSES = ("CONTACT_VERIFIED", "CONTACT_FORM_READY")

MECHANISM_TEMPLATE = {
    "COMPETITOR_GAP": "That's real search volume going to a name your prospects already recognize as a competitor, not to you.",
    "SERVICE_ARCHITECTURE_GAP": "Right now that service is bundled into a general page, so it has almost no chance of showing up when someone searches for it specifically.",
    "PRACTICE_AREA_GAP": "Right now that practice area is bundled into a general page, so it rarely surfaces for someone searching for it by name.",
    "MAPS_GAP": "An incomplete Maps listing is one of the easiest reasons a nearby, ready-to-call customer picks someone else instead.",
    "GBP_GAP": "Those missing profile details are exactly what Google leans on to decide who shows up first for a nearby search.",
    "REVIEW_GAP": "That review gap is a visible trust signal to anyone comparing you side-by-side with the businesses above you.",
    "TECHNICAL_INDEXATION_GAP": "That gap can quietly keep pages out of Google's index entirely, no matter how good the content on them is.",
    "SCHEMA_GAP": "Without that markup, Google has less to work with when deciding how (and whether) to feature you in local results.",
    "CONVERSION_GAP": "A visitor who has to hunt for a phone number or form is a visitor who often just leaves instead.",
    "PAID_OWNED_VISIBILITY_GAP": "Spending on ads while the organic side has a real gap means paying twice for visibility you could partly own for free.",
    "ORGANIC_VISIBILITY_GAP": "That's a page with real intent behind it that isn't earning the visibility it could.",
    "LOCAL_AUTHORITY_GAP": "That gap in local citations/authority is part of why nearby competitors edge you out in local results.",
}

CTA_TEMPLATE = {
    "THREE_POINT_COMPARISON": "I put together a quick 3-point comparison against them if useful",
    "GBP_CHECKLIST": "I put together a short checklist of the specific fields worth fixing first if useful",
    "REVIEW_GAP_SNAPSHOT": "I put together a quick snapshot of where the review gap actually sits if useful",
    "TECHNICAL_FINDINGS_SUMMARY": "I put together a short summary of exactly what's affecting indexing if useful",
    "SCHEMA_RECOMMENDATION": "I put together a short note on the specific markup worth adding first if useful",
    "CONVERSION_QUICK_WINS": "I put together a couple of quick, concrete fixes for that if useful",
    "ONE_PAGE_OPPORTUNITY_SUMMARY": "I put together a one-page summary of the specific opportunity if useful",
}


def entry_allowed(status):
    return status in ENTRY_STATUSES


def build_subject(business_name, wedge):
    label = wedge["opportunity_type"].replace("_", " ").lower()
    return f"Quick note on {business_name} and the {label} I noticed"


def build_body(prospect, contact, wedge, asset, sender_name):
    business_name = prospect.get("business_name")
    greeting = f"Hi {contact['identity']['person_name']}," if contact["identity"].get("person_name") else "Hi,"
    mechanism = MECHANISM_TEMPLATE.get(wedge["opportunity_type"],
                                        "That's a specific, addressable gap, not a generic scorecard issue.")
    cta = CTA_TEMPLATE.get(asset["asset_type"], "I put together a short summary of the specific opportunity if useful")

    lines = [
        greeting,
        "",
        f"{wedge['observation']}",
        "",
        mechanism,
        "",
        f"{cta} -- no obligation either way, just flagging something specific to {business_name} rather than a generic pitch.",
        "",
        f"{sender_name}",
    ]
    return "\n".join(lines)


def word_count(body):
    return len(body.split())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    ap.add_argument("--sender-name", default="Ali")
    args = ap.parse_args()

    p = next((r for r in read_jsonl(PROSPECTS / "discovered.jsonl") if r["id"] == args.id), None)
    if not p:
        raise SystemExit(f"Prospect {args.id} not found in discovered.jsonl")
    if not entry_allowed(p.get("status")):
        raise SystemExit(f"{args.id}: status is {p.get('status')!r}, not CONTACT_VERIFIED/CONTACT_FORM_READY.")

    contact = load_json(lead_dir(args.id) / "contact_record.json")
    wedge = load_json(lead_dir(args.id) / "primary_wedge.json")
    asset = load_json(lead_dir(args.id) / "staged_asset.json")
    if not (contact and wedge and asset):
        raise SystemExit(f"{args.id}: missing contact_record.json, primary_wedge.json, or staged_asset.json.")

    subject = build_subject(p.get("business_name"), wedge)
    body = build_body(p, contact, wedge, asset, args.sender_name)
    wc = word_count(body)

    draft_hash = content_hash(subject, body, contact["overall_status"], wedge.get("observation"))
    existing = load_json(lead_dir(args.id) / "email_draft.json")
    if existing and existing.get("content_hash") == draft_hash:
        print(f"{args.id}: EMAIL_DRAFT_READY (unchanged, idempotent no-op) -- {wc} words.")
        return

    draft = {
        "prospect_id": args.id, "subject": subject, "body": body, "word_count": wc,
        "recipient_channel": contact["channel"], "sender_name": args.sender_name,
        "opportunity_type": wedge["opportunity_type"], "asset_type": asset["asset_type"],
        "content_hash": draft_hash, "generated_at": now_iso(),
    }
    write_json(lead_dir(args.id) / "email_draft.json", draft)
    set_status_everywhere(args.id, "EMAIL_DRAFT_READY")
    record_event(args.id, "EMAIL_DRAFT_GENERATED", p.get("status"), "EMAIL_DRAFT_READY",
                 f"{wc} words, opportunity_type={wedge['opportunity_type']}")
    print(f"{args.id}: EMAIL_DRAFT_READY -- {wc} words.")


if __name__ == "__main__":
    main()
