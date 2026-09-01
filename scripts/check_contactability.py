#!/usr/bin/env python3
"""
Contactability pre-check -- V3.1. NOT final contact verification (see
verify_contact.py, which still runs later, unchanged, at CONTACT_VERIFICATION
for QUALIFIED/HIGH_PRIORITY leads). This is a cheap early gate that stops
expensive downstream work (quick audit, dossier, full verification) on a
business with no realistic contact path at all.

Usage:
  python3 scripts/check_contactability.py --id <slug> --print-prompt
  python3 scripts/check_contactability.py --id <slug> --save contactability.json
"""
import argparse
import json
import sys

from _lib import ROOT, PROSPECTS, read_jsonl, write_jsonl, lead_dir, load_json, write_json

PROMPT_PATH = ROOT / "prompts" / "contactability-check.md"

FIELDS = (
    "named_owner_found", "named_marketing_contact_found", "named_ops_contact_found",
    "official_email_visible", "official_contact_form_available", "likely_contact_role",
)


def route(result, fit_confirmed_score, fit_thresholds):
    """
    Pure routing decision -- unit-testable without file I/O.
    contactability_score == 0 -> CONTACTABILITY_FAILED, UNLESS a contact
    form exists and commercial fit is unusually strong (>= high_priority_min),
    in which case it's preserved as a manual/contact-form candidate instead
    of discarded.
    """
    score = result.get("contactability_score")
    if score == 0:
        if result.get("official_contact_form_available") and (fit_confirmed_score or 0) >= fit_thresholds.get("high_priority_min", 65):
            return "CONTACTABILITY_CHECK", "score=0 but contact form exists and FIT is unusually strong -- preserved as a manual/contact-form candidate"
        return "CONTACTABILITY_FAILED", "no realistic contact path and fit isn't strong enough to justify a manual contact-form exception"
    return "CONTACTABILITY_CHECK", "plausible contact path found"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    ap.add_argument("--print-prompt", action="store_true")
    ap.add_argument("--save")
    args = ap.parse_args()

    discovered = read_jsonl(PROSPECTS / "discovered.jsonl")
    p = next((r for r in discovered if r["id"] == args.id), None)
    if not p:
        raise SystemExit(f"Prospect {args.id} not found in discovered.jsonl")

    if args.save:
        from _lib import load_yaml
        raw = sys.stdin.read() if args.save == "-" else open(args.save).read()
        result = json.loads(raw)
        fit_thresholds = load_yaml("scoring.yaml")["fit_thresholds"]
        status, reason = route(result, p.get("fit_confirmed_score"), fit_thresholds)

        for fname in ("discovered.jsonl", "qualified.jsonl", "manual_review.jsonl", "needs_enrichment.jsonl"):
            recs = read_jsonl(PROSPECTS / fname)
            changed = False
            for r in recs:
                if r["id"] == args.id:
                    r["contactability_score"] = result.get("contactability_score")
                    for f in FIELDS:
                        r[f] = result.get(f)
                    r["contactability_evidence"] = result.get("evidence", [])
                    r["status"] = status
                    changed = True
            if changed:
                write_jsonl(PROSPECTS / fname, recs)

        qual = load_json(lead_dir(args.id) / "qualification_v3.json") or {"prospect_id": args.id}
        qual["contactability"] = {
            "contactability_score": result.get("contactability_score"),
            **{f: result.get(f) for f in FIELDS},
            "evidence": result.get("evidence", []),
        }
        write_json(lead_dir(args.id) / "qualification_v3.json", qual)

        print(f"{args.id}: {status} (contactability_score={result.get('contactability_score')}) -- {reason}")
        return

    prompt = PROMPT_PATH.read_text()
    print(prompt)
    print("\n---\n## prospect record\n")
    print(json.dumps(p, indent=2))


if __name__ == "__main__":
    main()
