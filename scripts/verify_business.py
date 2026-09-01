#!/usr/bin/env python3
"""
Business identity verification -- Phase 2 of the V2 pipeline, runs BEFORE
scoring. The actual cross-checking is done by Claude following
prompts/business-verification.md; this script only assembles the prompt and
applies the threshold decision to the shared discovered.jsonl record.

This is what catches company-name collisions (e.g. multiple businesses
named "Example Restoration") before their mismatched evidence gets scored as
if it all belonged to one company.

Usage:
  python3 scripts/verify_business.py --id <slug> --print-prompt
  python3 scripts/verify_business.py --id <slug> --save verification.json
  echo '{"prospect_id": "...", "business_verified": true, ...}' | \\
      python3 scripts/verify_business.py --id <slug> --save -
"""
import argparse
import json
import sys

from _lib import ROOT, PROSPECTS, load_yaml, read_jsonl, write_jsonl

PROMPT_PATH = ROOT / "prompts" / "business-verification.md"


def apply_verification(p, verification, min_identity_confidence):
    p["business_verified"] = verification["business_verified"]
    p["identity_confidence"] = verification["identity_confidence"]
    p["matched_fields"] = verification.get("matched_fields", [])
    p["conflicting_fields"] = verification.get("conflicting_fields", [])
    p["verification_source_notes"] = verification.get("source_notes", [])

    if not verification["business_verified"]:
        p["status"] = "REJECTED"
        p["reject_reason"] = "business_not_verified"
        return "REJECTED"

    conf = verification["identity_confidence"]
    if conf < min_identity_confidence:
        # Low confidence but not outright unverified: a human should
        # untangle a probable name collision rather than the pipeline
        # guessing which business the rest of the record belongs to.
        if verification.get("conflicting_fields"):
            p["status"] = "MANUAL_REVIEW"
            p["reject_reason"] = None
            return "MANUAL_REVIEW"
        p["status"] = "REJECTED"
        p["reject_reason"] = "identity_confidence_below_threshold"
        return "REJECTED"

    p["status"] = "BUSINESS_VERIFIED"
    p["reject_reason"] = None
    return "BUSINESS_VERIFIED"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    ap.add_argument("--print-prompt", action="store_true")
    ap.add_argument("--save")
    args = ap.parse_args()

    discovered_path = PROSPECTS / "discovered.jsonl"
    records = read_jsonl(discovered_path)
    p = next((r for r in records if r["id"] == args.id), None)
    if not p:
        raise SystemExit(f"Prospect {args.id} not found in {discovered_path}")

    if args.save:
        limits = load_yaml("limits.yaml")
        raw = sys.stdin.read() if args.save == "-" else open(args.save).read()
        verification = json.loads(raw)
        outcome = apply_verification(p, verification, limits["min_identity_confidence"])
        write_jsonl(discovered_path, records)
        print(f"{args.id}: {outcome} (identity_confidence={verification['identity_confidence']})")
        if verification.get("conflicting_fields"):
            print(f"Conflicting fields noted: {verification['conflicting_fields']}")
        return

    prompt = PROMPT_PATH.read_text()
    print(prompt)
    print("\n---\n## prospect record to verify\n")
    print(json.dumps(p, indent=2))


if __name__ == "__main__":
    main()
