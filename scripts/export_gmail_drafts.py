#!/usr/bin/env python3
"""
Export QA-PASS, contact-verified leads to a flat JSONL file a human (or a
separate tool) can turn into actual Gmail drafts later. This script makes
ZERO network calls and has NO Gmail integration of any kind -- it only
writes a local file. `send_status` is always "DRAFT_ONLY". Nothing is ever
sent from this project.

Usage:
  python3 scripts/export_gmail_drafts.py
"""
from _lib import LEADS, OUTREACH, load_json, read_jsonl, write_jsonl

# Best-effort state -> timezone map for the send-window recommendation.
# Missing/unknown states just get timezone: null -- never guessed.
STATE_TIMEZONE = {
    "NC": "America/New_York", "FL": "America/New_York", "GA": "America/New_York",
    "NY": "America/New_York", "VA": "America/New_York", "TN": "America/Chicago",
    "TX": "America/Chicago", "IL": "America/Chicago", "CO": "America/Denver",
    "AZ": "America/Phoenix", "CA": "America/Los_Angeles", "WA": "America/Los_Angeles",
    "OH": "America/New_York", "PA": "America/New_York", "MI": "America/New_York",
}
DEFAULT_SEND_WINDOW = "08:30-09:30"


def main():
    out_path = OUTREACH / "ready-for-draft.jsonl"
    already_exported = {r["business"] + "|" + r["email"] for r in read_jsonl(out_path)}

    rows = []
    skipped_no_qa_pass = 0
    skipped_no_contact = 0

    for ldir in sorted(LEADS.iterdir()):
        if not ldir.is_dir():
            continue
        dossier = load_json(ldir / "dossier.json")
        email = load_json(ldir / "email.json")
        contact = load_json(ldir / "contact.json")
        if not (dossier and email):
            continue
        qa = email.get("qa") or {}
        if qa.get("verdict") != "PASS":
            skipped_no_qa_pass += 1
            continue
        if not (contact and contact.get("contact_verified") and contact.get("email")):
            skipped_no_contact += 1
            continue

        row = {
            "business": dossier["business"],
            "contact_name": contact.get("contact_name"),
            "email": contact["email"],
            "subject": email["subject"],
            "body": email["body"],
            "verification_status": "CONTACT_VERIFIED",
            "qa_status": "PASS",
            "send_status": "DRAFT_ONLY",
            "timezone": STATE_TIMEZONE.get((dossier.get("state") or "").strip().upper()),
            "recommended_local_send_window": DEFAULT_SEND_WINDOW,
        }
        key = row["business"] + "|" + row["email"]
        if key not in already_exported:
            rows.append(row)
            already_exported.add(key)

    if rows:
        existing = read_jsonl(out_path)
        write_jsonl(out_path, existing + rows)

    print(
        f"Exported {len(rows)} new draft(s) to {out_path} "
        f"(skipped {skipped_no_qa_pass} not QA-PASS, {skipped_no_contact} without a verified contact). "
        "send_status is always DRAFT_ONLY -- this script never sends anything."
    )


if __name__ == "__main__":
    main()
