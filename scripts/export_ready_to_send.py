#!/usr/bin/env python3
"""
V3.4 handoff export: every lead that has cleared QA and has a planned send
window gets one record in data/outreach/ready_to_send.jsonl. This file is
the ONLY thing ChatGPT/Gmail-side automation needs to perform real Gmail
reconciliation and sending -- it contains no credentials, and reproducing
it never requires redoing the SEO/intelligence analysis.

Idempotent: keyed by prospect_id + content_hash, so re-running this export
never duplicates a row for a lead whose draft/window hasn't changed, and
naturally picks up a lead whose draft *did* change (regenerated after a
QA_FAILED fix, or a replanned window) as a fresh row.

This script NEVER sends anything and NEVER touches Gmail -- it only reads
already-produced local JSON and appends plain-text lines to a local file.

Usage:
  python3 scripts/export_ready_to_send.py
"""
from _lib import (PROSPECTS, LEADS, OUTREACH, read_jsonl, write_jsonl, load_json,
                   now_iso, content_hash)

ENTRY_STATUSES = ("SEND_WINDOW_PLANNED", "READY_TO_SEND")
OUT_PATH = OUTREACH / "ready_to_send.jsonl"


def build_handoff_record(p, contact, draft, window, wedge, dossier):
    row_hash = content_hash(
        p["id"], draft.get("content_hash", ""), window.get("local_datetime", ""),
        contact.get("overall_status", ""),
    )
    return {
        "prospect_id": p["id"],
        "business_name": p.get("business_name"),
        "website": p.get("website"),
        "niche": p.get("niche"), "city": p.get("city"), "state": p.get("state"),
        "recipient": {
            "channel_type": contact["channel"]["type"],
            "address_or_url": contact["channel"]["address_or_url"],
            "identity_status": contact["identity"]["status"],
            "mailbox_status": contact["mailbox"]["status"],
        },
        "email": {"subject": draft["subject"], "body": draft["body"], "word_count": draft["word_count"]},
        "opportunity_summary": {
            "opportunity_type": wedge.get("opportunity_type") if wedge else None,
            "confidence": wedge.get("confidence") if wedge else None,
            "observation": wedge.get("observation") if wedge else None,
        },
        "send_window": window,
        "qa_status": "QA_PASS",
        "fit_gap_snapshot": {
            "fit": (dossier or {}).get("qualification", {}).get("fit"),
            "gap": (dossier or {}).get("qualification", {}).get("gap"),
        },
        "content_hash": row_hash,
        "lead_engine_status_at_export": p.get("status"),
        "exported_at": now_iso(),
        "note": "Lead Engine's role stops here. Gmail send, delivery/bounce reconciliation, "
                "reply detection, and follow-up execution are owned downstream, not by this file's producer.",
    }


def main():
    prospects = read_jsonl(PROSPECTS / "discovered.jsonl")
    existing = read_jsonl(OUT_PATH)
    seen_hashes = {r.get("content_hash") for r in existing}

    new_rows = []
    for p in prospects:
        if p.get("status") not in ENTRY_STATUSES:
            continue
        pid = p["id"]
        ldir = LEADS / pid
        contact = load_json(ldir / "contact_record.json")
        draft = load_json(ldir / "email_draft.json")
        window = load_json(ldir / "send_window.json")
        wedge = load_json(ldir / "primary_wedge.json")
        dossier = load_json(ldir / "intelligence_dossier.json")
        if not (contact and draft and window):
            continue  # incomplete lead -- never export a partial/guessed handoff record

        row = build_handoff_record(p, contact, draft, window, wedge, dossier)
        if row["content_hash"] in seen_hashes:
            continue
        new_rows.append(row)
        seen_hashes.add(row["content_hash"])

    if new_rows:
        write_jsonl(OUT_PATH, existing + new_rows)
    print(f"export_ready_to_send: {len(new_rows)} new row(s), {len(existing) + len(new_rows)} total in {OUT_PATH}")
    return len(new_rows)


if __name__ == "__main__":
    main()
