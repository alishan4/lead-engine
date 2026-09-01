#!/usr/bin/env python3
"""
Email QA (V2). The judgment is done by Claude following prompts/email-qa.md
against the dossier + contact + generated email -- this script never calls
an LLM API itself. Same two-mode pattern as generate_email.py.

Usage:
  python3 scripts/qa_email.py --id <slug> --print-prompt
  python3 scripts/qa_email.py --id <slug> --save verdict.json
  echo '{"verdict": "PASS", "checks": {...}, "notes": "..."}' | \\
      python3 scripts/qa_email.py --id <slug> --save -
"""
import argparse
import json
import sys

from _lib import ROOT, lead_dir, load_json, write_json, set_status_everywhere

PROMPT_PATH = ROOT / "prompts" / "email-qa.md"
VALID_VERDICTS = ("PASS", "REWRITE", "REJECT", "REVERIFY_REQUIRED")

# PASS advances the pipeline; every other verdict sends the lead back to an
# earlier state rather than leaving it stuck at "has an email" with no
# resolution path.
STATUS_MAP = {
    "PASS": "QA_PASS",
    "REWRITE": "CONTACT_VERIFIED",       # contact is still fine, just needs a new draft
    "REJECT": "DOSSIER_READY",           # evidence itself needs re-review, not just wording
    "REVERIFY_REQUIRED": "REVERIFY_REQUIRED",
}


def apply_qa_guards(verdict, contact):
    """
    Defense-in-depth, enforced in code rather than trusted purely to the
    LLM's stated verdict. Each guard can only downgrade a verdict (PASS ->
    something stricter), never upgrade one. Pure function -- no file I/O --
    so it's directly unit-testable (see tests/test_v2_pipeline.py).

    Sets verdict["_status_override"] when a guard implies a more specific
    next status than the generic STATUS_MAP entry for its verdict (e.g. an
    unverified-recipient REJECT needs new contact verification, not a
    dossier rework -- routing it to DOSSIER_READY would be actively
    misleading about what's actually blocking the lead).
    """
    checks = verdict.get("checks") or {}

    if verdict["verdict"] == "PASS" and not (contact and contact.get("contact_verified")):
        verdict["verdict"] = "REJECT"
        verdict["_status_override"] = "CONTACT_UNVERIFIED"
        verdict.setdefault("verification_issues", []).append(
            "guarded: no verified contact.json -- QA cannot PASS an unverified recipient"
        )

    if checks.get("facts_supported") is False and verdict["verdict"] in ("PASS", "REWRITE"):
        verdict["verdict"] = "REJECT"
        verdict.setdefault("verification_issues", []).append(
            "guarded: facts_supported is false -- unsupported claims require REJECT, not a wording fix"
        )

    if checks.get("finding_fresh") is False and verdict["verdict"] == "PASS":
        verdict["verdict"] = "REVERIFY_REQUIRED"
        verdict.setdefault("verification_issues", []).append(
            "guarded: finding_fresh is false -- stale evidence cannot PASS"
        )

    if checks.get("ranking_claims_sourced_and_dated") is False and verdict["verdict"] == "PASS":
        verdict["verdict"] = "REWRITE"
        verdict.setdefault("verification_issues", []).append(
            "guarded: an exact ranking figure is cited without a dated source"
        )

    return verdict


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    ap.add_argument("--print-prompt", action="store_true")
    ap.add_argument("--save")
    args = ap.parse_args()

    ldir = lead_dir(args.id)
    dossier = load_json(ldir / "dossier.json")
    email = load_json(ldir / "email.json")
    contact = load_json(ldir / "contact.json")
    if not dossier or not email:
        raise SystemExit(f"Need both dossier.json and email.json in {ldir} before QA.")

    if args.save:
        raw = sys.stdin.read() if args.save == "-" else open(args.save).read()
        verdict = json.loads(raw)
        if verdict.get("verdict") not in VALID_VERDICTS:
            raise SystemExit(f"verdict must be one of {VALID_VERDICTS}")

        verdict = apply_qa_guards(verdict, contact)
        next_status = verdict.pop("_status_override", None) or STATUS_MAP[verdict["verdict"]]

        email["qa"] = verdict
        write_json(ldir / "email.json", email)
        set_status_everywhere(args.id, next_status)

        print(f"QA verdict: {verdict['verdict']}. Status -> {next_status}. "
              f"Saved to {ldir / 'email.json'}")
        if verdict["verdict"] != "PASS":
            print(f"Notes: {verdict.get('notes', '')}")
            for issue in verdict.get("unsupported_claims", []):
                print(f"  unsupported claim: {issue}")
            for issue in verdict.get("stale_claims", []):
                print(f"  stale claim: {issue}")
        return

    prompt = PROMPT_PATH.read_text()
    print(prompt)
    print("\n---\n## dossier.json (evidence_items[] is the source of truth)\n")
    print(json.dumps(dossier, indent=2))
    print("\n## contact.json\n")
    print(json.dumps(contact, indent=2) if contact else "null  # no verified contact on file")
    print("\n## email.json (subject + body under review)\n")
    print(json.dumps({"subject": email["subject"], "body": email["body"]}, indent=2))


if __name__ == "__main__":
    main()
