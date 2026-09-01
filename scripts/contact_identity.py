#!/usr/bin/env python3
"""
V3.3 contact identity + mailbox resolution. Entry gate: only ASSET_STAGED
prospects are eligible (they already cleared V3.2's wedge/dossier/asset
work). This re-verifies contact evidence from scratch -- it does not blindly
trust a prior V2 contact.json (a contact record from an earlier phase can be
stale, and V3.3 is the stage that can actually lead to a send).

Usage:
  python3 scripts/contact_identity.py --print-prompt --id <slug>
  python3 scripts/contact_identity.py --save research.json --id <slug>
"""
import argparse
import json
import sys

from _lib import (PROSPECTS, LEADS, load_yaml, read_jsonl, lead_dir, load_json, write_json,
                   set_status_everywhere, now_iso)
from outreach_lib import is_suppressed, account_lock_check, record_event

ENTRY_STATUS = "ASSET_STAGED"


def entry_allowed(status):
    return status == ENTRY_STATUS


def classify_identity_source(source_type, cfg):
    """Pure: is this source_type strong enough to establish identity on its own?"""
    return source_type in set(cfg["identity"]["acceptable_source_types"])


def resolve_identity(research, cfg):
    """
    Pure function: research dict -> identity object. `research` is the
    ingested output of prompts/contact-identity-verification.md.
    """
    sources = research.get("sources") or []
    acceptable = [s for s in sources if classify_identity_source(s.get("source_type"), cfg)]
    person_name = research.get("person_name")
    email = research.get("email")

    if acceptable and person_name and email:
        status, confidence = "VERIFIED", max(0.75, min(0.95, 0.75 + 0.05 * len(acceptable)))
    elif acceptable and email and not person_name:
        status, confidence = "COMPANY_INBOX_ONLY", 0.65
    elif research.get("has_contact_form"):
        status, confidence = "FORM_ONLY", 0.50
    else:
        status, confidence = "UNVERIFIED", 0.0

    return {
        "status": status,
        "confidence": round(confidence, 2),
        "person_name": person_name if status == "VERIFIED" else None,
        "role": research.get("role") if status == "VERIFIED" else None,
        "email": email if status in ("VERIFIED", "COMPANY_INBOX_ONLY") else None,
        "sources": acceptable,
        "rejected_evidence": research.get("rejected_evidence") or [],
    }


def resolve_mailbox(research):
    hint = research.get("mailbox_hint")
    if hint in ("VALID", "RISKY", "INVALID"):
        return {"status": hint, "checked_at": now_iso(), "basis": "supplied deliverability signal (e.g. prior bounce record)"}
    return {"status": "UNKNOWN", "checked_at": None, "basis": "no active mailbox probing performed -- UNKNOWN is the honest default"}


def resolve_channel(identity, research):
    if identity["status"] == "VERIFIED":
        return {"type": "NAMED_EMAIL", "address_or_url": identity["email"]}
    if identity["status"] == "COMPANY_INBOX_ONLY":
        return {"type": "COMPANY_EMAIL", "address_or_url": identity["email"]}
    if identity["status"] == "FORM_ONLY" and research.get("contact_form_url"):
        return {"type": "CONTACT_FORM", "address_or_url": research.get("contact_form_url")}
    return {"type": "NONE", "address_or_url": None}


def overall_status(identity, channel, mailbox):
    if mailbox["status"] == "INVALID":
        return "CONTACT_REVERIFY_REQUIRED"
    if identity["status"] in ("VERIFIED", "COMPANY_INBOX_ONLY") and channel["type"] != "NONE":
        return "CONTACT_VERIFIED"
    if identity["status"] == "FORM_ONLY" and channel["type"] == "CONTACT_FORM":
        return "CONTACT_FORM_READY"
    return "CONTACT_UNVERIFIED"


def build_contact_record(prospect_id, business_name, research, cfg):
    identity = resolve_identity(research, cfg)
    mailbox = resolve_mailbox(research)
    channel = resolve_channel(identity, research)
    status = overall_status(identity, channel, mailbox)
    return {
        "prospect_id": prospect_id, "business_name": business_name,
        "identity": identity, "mailbox": mailbox, "channel": channel,
        "overall_status": status,
        "reverify_reason": "mailbox flagged INVALID by a real deliverability signal" if status == "CONTACT_REVERIFY_REQUIRED" else None,
        "generated_at": now_iso(),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--print-prompt", action="store_true")
    g.add_argument("--save", help="path to research JSON, or '-' for stdin")
    args = ap.parse_args()

    p = next((r for r in read_jsonl(PROSPECTS / "discovered.jsonl") if r["id"] == args.id), None)
    if not p:
        raise SystemExit(f"Prospect {args.id} not found in discovered.jsonl")

    if args.print_prompt:
        with open("prompts/contact-identity-verification.md") as f:
            template = f.read()
        print(template)
        print(f"\nBusiness: {p.get('business_name')} ({p.get('niche')}, {p.get('city')}, {p.get('state')})")
        print(f"Website: {p.get('website')}")
        return

    if not entry_allowed(p.get("status")):
        raise SystemExit(f"{args.id}: status is {p.get('status')!r}, not {ENTRY_STATUS} -- "
                          "contact identity work only starts after V3.2's asset staging.")

    suppressed, sup_record = is_suppressed(business_id=args.id)
    if suppressed:
        set_status_everywhere(args.id, "SUPPRESSED")
        record_event(args.id, "CONTACT_IDENTITY_BLOCKED", p.get("status"), "SUPPRESSED",
                     f"business_id is in the suppression registry: {sup_record.get('reason')}")
        print(f"{args.id}: SUPPRESSED ({sup_record.get('reason')}) -- outreach blocked, no draft will be produced.")
        return

    allowed, lock_reason = account_lock_check(args.id, domain=p.get("website"))
    if not allowed:
        set_status_everywhere(args.id, "ACCOUNT_LOCKED")
        record_event(args.id, "CONTACT_IDENTITY_BLOCKED", p.get("status"), "ACCOUNT_LOCKED", lock_reason)
        print(f"{args.id}: ACCOUNT_LOCKED -- {lock_reason}")
        return

    raw = sys.stdin.read() if args.save == "-" else open(args.save).read()
    research = json.loads(raw)

    cfg = load_yaml("outreach.yaml")
    record = build_contact_record(args.id, p.get("business_name"), research, cfg)
    write_json(lead_dir(args.id) / "contact_record.json", record)
    set_status_everywhere(args.id, record["overall_status"])
    record_event(args.id, "CONTACT_IDENTITY_RESOLVED", p.get("status"), record["overall_status"],
                 f"identity={record['identity']['status']} channel={record['channel']['type']} mailbox={record['mailbox']['status']}")
    print(f"{args.id}: {record['overall_status']} (identity={record['identity']['status']}, "
          f"confidence={record['identity']['confidence']}, channel={record['channel']['type']})")


if __name__ == "__main__":
    main()
