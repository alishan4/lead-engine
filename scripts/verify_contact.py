#!/usr/bin/env python3
"""
Contact verification -- Phase 10/11. The actual lookup is done by Claude
following prompts/contact-verification.md; this script assembles the prompt
and enforces the hard rule in code (not just in the prompt): a guessed email
can never become contact_verified, and a prospect can never reach
EMAIL_DRAFT_READY without a verified contact.

Usage:
  python3 scripts/verify_contact.py --id <slug> --print-prompt
  python3 scripts/verify_contact.py --id <slug> --save contact.json
"""
import argparse
import json
import sys

from _lib import ROOT, load_yaml, lead_dir, load_json, write_json, set_status_everywhere

PROMPT_PATH = ROOT / "prompts" / "contact-verification.md"

# Enforced in code, not just in the prompt: these source_types can never
# back a contact_verified: true claim, no matter what confidence is reported.
NEVER_VERIFIED_SOURCE_TYPES = {"guessed", "none"}


def apply_contact(contact, min_contact_confidence):
    if contact.get("source_type") in NEVER_VERIFIED_SOURCE_TYPES:
        contact["contact_verified"] = False
        contact.setdefault("notes", "")
        contact["notes"] = (contact["notes"] + " [guarded: guessed/no source can never verify]").strip()
    elif not contact.get("source_url"):
        contact["contact_verified"] = False
        contact["notes"] = (contact.get("notes", "") + " [guarded: no source_url provided]").strip()
    elif contact.get("verification_confidence", 0) < min_contact_confidence:
        contact["contact_verified"] = False

    return "CONTACT_VERIFIED" if contact["contact_verified"] else "CONTACT_UNVERIFIED"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    ap.add_argument("--print-prompt", action="store_true")
    ap.add_argument("--save")
    args = ap.parse_args()

    ldir = lead_dir(args.id)
    dossier = load_json(ldir / "dossier.json")
    if not dossier:
        raise SystemExit(f"No dossier.json for {args.id}. Run build_dossier.py first.")

    if args.save:
        limits = load_yaml("limits.yaml")
        raw = sys.stdin.read() if args.save == "-" else open(args.save).read()
        contact = json.loads(raw)
        status = apply_contact(contact, limits["min_contact_confidence"])
        write_json(ldir / "contact.json", contact)
        set_status_everywhere(args.id, status)

        print(f"{args.id}: {status}"
              + (f" ({contact.get('role')}: {contact.get('email')}, source={contact.get('source_type')})"
                 if status == "CONTACT_VERIFIED" else " -- no email-ready recipient found; "
                 "email generation is blocked unless run with generate_email.py --preview"))
        return

    prompt = PROMPT_PATH.read_text()
    print(prompt)
    print("\n---\n## dossier.json\n")
    print(json.dumps(dossier, indent=2))


if __name__ == "__main__":
    main()
