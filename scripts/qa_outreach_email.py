#!/usr/bin/env python3
"""
V3.3 outreach QA gate -- deterministic, rule-based, NOT an LLM judgment
call. Every check here is either a pattern match or an already-computed
fact from an earlier stage, so the gate is fully auditable and reproducible
(the same draft always gets the same verdict). This mirrors V2's
qa_email.py apply_qa_guards() philosophy, extended with the account/
suppression/specificity checks V3.3 adds.

Usage:
  python3 scripts/qa_outreach_email.py --id <slug>
"""
import argparse
import re

from _lib import PROSPECTS, read_jsonl, lead_dir, load_json, load_yaml, set_status_everywhere, days_since
from outreach_lib import is_suppressed, account_lock_check, record_event
from wedge_selection import passes_company_swap_test, commercial_mechanism_is_defensible

ENTRY_STATUS = "EMAIL_DRAFT_READY"


def entry_allowed(status):
    return status == ENTRY_STATUS


def check_forbidden_phrases(text, forbidden_phrases):
    low = text.lower()
    hits = [p for p in forbidden_phrases if p in low]
    return hits


def check_word_count(word_count, cfg):
    return word_count <= cfg["email"]["word_count_hard_ceiling"]


def check_freshness_for_send(dossier, limits):
    from _lib import check_freshness
    return check_freshness(dossier, limits)


def apply_qa(prospect_id, contact, wedge, asset, draft, dossier, cfg, limits):
    """
    Pure decision function (all inputs already loaded): returns
    (verdict, reasons) where verdict in {"QA_PASS", "QA_FAILED"}.
    """
    reasons = []

    if contact["overall_status"] not in ("CONTACT_VERIFIED", "CONTACT_FORM_READY"):
        reasons.append(f"recipient identity not cleared: contact overall_status={contact['overall_status']!r}")

    suppressed, sup = is_suppressed(email=(contact["channel"] or {}).get("address_or_url"),
                                     business_id=prospect_id)
    if suppressed:
        reasons.append(f"recipient/business is suppressed: {sup.get('reason')}")

    allowed, lock_reason = account_lock_check(prospect_id)
    if not allowed:
        reasons.append(f"account outreach lock: {lock_reason}")

    if not wedge or wedge.get("confidence", 0) < limits.get("usable_confidence_threshold", 0.70):
        reasons.append("primary wedge missing or below usable-confidence threshold")

    if not asset:
        reasons.append("no staged asset exists to back the email's offer")

    if wedge and not passes_company_swap_test(wedge, known_specific_terms=[dossier["business"]["name"]] if dossier else None):
        reasons.append("wedge observation fails the company-swap specificity test")

    body = draft.get("body", "")
    subject = draft.get("subject", "")

    ok_mechanism, mech_reason = commercial_mechanism_is_defensible(body)
    if not ok_mechanism:
        reasons.append(f"unsupported/fabricated mechanism claim in body: {mech_reason}")

    forbidden_hits = check_forbidden_phrases(body, cfg["email"]["forbidden_phrases"]) + \
        check_forbidden_phrases(subject, cfg["email"]["forbidden_phrases"])
    if forbidden_hits:
        reasons.append(f"generic-agency/guarantee/urgency language detected: {sorted(set(forbidden_hits))}")

    if not check_word_count(draft.get("word_count", 0), cfg):
        reasons.append(f"body exceeds word count ceiling ({draft.get('word_count')} > {cfg['email']['word_count_hard_ceiling']})")

    if dossier:
        is_fresh, fresh_reasons = check_freshness_for_send(dossier, limits)
        if not is_fresh:
            reasons.extend(fresh_reasons)
    else:
        reasons.append("no intelligence_dossier.json to verify evidence freshness against")

    # No named person claimed in body unless contact identity actually verified a person
    person_claimed = re.search(r"^Hi ([A-Z][a-zA-Z.\- ]+),", body.split("\n", 1)[0] or "")
    if person_claimed and contact["identity"].get("status") != "VERIFIED":
        reasons.append("email greets a named person but identity was not VERIFIED for a real person")

    verdict = "QA_PASS" if not reasons else "QA_FAILED"
    return verdict, reasons


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    args = ap.parse_args()

    p = next((r for r in read_jsonl(PROSPECTS / "discovered.jsonl") if r["id"] == args.id), None)
    if not p:
        raise SystemExit(f"Prospect {args.id} not found in discovered.jsonl")
    if not entry_allowed(p.get("status")):
        raise SystemExit(f"{args.id}: status is {p.get('status')!r}, not {ENTRY_STATUS}.")

    contact = load_json(lead_dir(args.id) / "contact_record.json")
    wedge = load_json(lead_dir(args.id) / "primary_wedge.json")
    asset = load_json(lead_dir(args.id) / "staged_asset.json")
    draft = load_json(lead_dir(args.id) / "email_draft.json")
    dossier = load_json(lead_dir(args.id) / "intelligence_dossier.json")
    cfg = load_yaml("outreach.yaml")
    limits = load_yaml("limits.yaml")

    verdict, reasons = apply_qa(args.id, contact, wedge, asset, draft, dossier, cfg, limits)
    # QA_PASS is a momentary verdict, not a resting state -- a passed draft is
    # immediately READY_TO_SEND (all gates cleared, just needs a window).
    persisted_status = "READY_TO_SEND" if verdict == "QA_PASS" else "QA_FAILED"
    set_status_everywhere(args.id, persisted_status)
    record_event(args.id, "QA_EVALUATED", p.get("status"), persisted_status,
                 "; ".join(reasons) if reasons else "all checks passed")

    if verdict == "QA_PASS":
        print(f"{args.id}: QA_PASS -> READY_TO_SEND")
    else:
        print(f"{args.id}: QA_FAILED -- {len(reasons)} issue(s):")
        for r in reasons:
            print(f"  - {r}")


if __name__ == "__main__":
    main()
