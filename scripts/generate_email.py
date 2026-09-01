#!/usr/bin/env python3
"""
Email draft generation. The actual writing is done by Claude following
prompts/email-writer.md against ONLY the dossier -- this script never calls
an LLM API itself. It has two modes:

  --print-prompt   Print the exact prompt + dossier JSON to send to Claude.
  --save <file>     Validate a drafted {subject, body} JSON (or '-' for stdin)
                     against config/limits.yaml and save it to
                     data/leads/<slug>/email.json.

V2 hard gates (both --print-prompt and --save enforce these; use --preview
to bypass for manual/preview-only drafting, per Phase 11):
  1. Freshness (Phase 9): the dossier's evidence must not be stale
     (config/limits.yaml: finding_freshness_days / ranking_freshness_days).
  2. Contact verification (Phase 10/11): data/leads/<slug>/contact.json must
     exist with contact_verified: true. No draft is generated for an
     unverified recipient in normal (non-preview) mode.

Usage:
  python3 scripts/generate_email.py --id <slug> --print-prompt
  python3 scripts/generate_email.py --id <slug> --save draft.json
  echo '{"prospect_id": "...", "subject": "...", "body": "..."}' | \\
      python3 scripts/generate_email.py --id <slug> --save -
  python3 scripts/generate_email.py --id <slug> --print-prompt --preview
"""
import argparse
import json
import sys

from _lib import (
    ROOT, PROSPECTS, load_yaml, read_jsonl, write_jsonl, lead_dir, load_json,
    write_json, now_iso, check_freshness,
)

PROMPT_PATH = ROOT / "prompts" / "email-writer.md"


def gate_or_exit(ldir, dossier, limits, preview):
    is_fresh, reasons = check_freshness(dossier, limits)
    if not is_fresh and not preview:
        raise SystemExit(
            f"REVERIFY_REQUIRED: {ldir.name}'s evidence is stale, refusing to generate outreach:\n"
            + "\n".join(f"  - {r}" for r in reasons)
            + "\nRun scripts/check_freshness.py, then re-run the quick-audit/opportunity-selector "
            "steps (or a fresh ranking import) before drafting an email. "
            "Use --preview to bypass for a manual/internal draft only (never send it)."
        )

    contact = load_json(ldir / "contact.json")
    if not (contact and contact.get("contact_verified")) and not preview:
        raise SystemExit(
            f"CONTACT_UNVERIFIED: no verified recipient for {ldir.name} "
            f"(data/leads/{ldir.name}/contact.json missing or contact_verified is false).\n"
            "Run scripts/verify_contact.py first -- V2 never drafts outreach for an unverified "
            "recipient. Use --preview to generate a preview-only draft with no send-ready recipient."
        )
    return contact


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    ap.add_argument("--print-prompt", action="store_true")
    ap.add_argument("--save")
    ap.add_argument(
        "--preview", action="store_true",
        help="Bypass the freshness/contact-verification gates for a manual preview draft only. "
        "The result is never marked EMAIL_DRAFT_READY.",
    )
    args = ap.parse_args()

    ldir = lead_dir(args.id)
    dossier = load_json(ldir / "dossier.json")
    if not dossier:
        raise SystemExit(f"No dossier.json for {args.id}. Run build_dossier.py first.")

    limits = load_yaml("limits.yaml")
    contact = gate_or_exit(ldir, dossier, limits, args.preview)

    if args.save:
        raw = sys.stdin.read() if args.save == "-" else open(args.save).read()
        draft = json.loads(raw)
        for field in ("subject", "body"):
            if field not in draft:
                raise SystemExit(f"Draft missing required field: {field}")
        word_count = len(draft["body"].split())
        if word_count > limits["max_email_words"]:
            print(f"WARNING: draft is {word_count} words, over the "
                  f"{limits['max_email_words']}-word limit. Saved anyway for QA review.",
                  file=sys.stderr)
        email = {
            "prospect_id": args.id,
            "subject": draft["subject"],
            "body": draft["body"],
            "word_count": word_count,
            "generated_at": now_iso(),
            "preview_only": args.preview,
            "recipient_email": (contact or {}).get("email") if not args.preview else None,
            "qa": {"verdict": None, "checks": {}, "notes": "not yet QA'd -- run qa_email.py"},
        }
        write_json(ldir / "email.json", email)

        if not args.preview:
            discovered_path = PROSPECTS / "discovered.jsonl"
            discovered = read_jsonl(discovered_path)
            for r in discovered:
                if r["id"] == args.id:
                    r["status"] = "EMAIL_DRAFT_READY"
            write_jsonl(discovered_path, discovered)

        tag = " [PREVIEW ONLY -- not send-ready]" if args.preview else " -- status: EMAIL_DRAFT_READY"
        print(f"Saved draft ({word_count} words) to {ldir / 'email.json'}{tag}")
        return

    prompt = PROMPT_PATH.read_text()
    print(prompt)
    print("\n---\n## dossier.json for this lead (only evidence_items[] facts may be cited)\n")
    print(json.dumps(dossier, indent=2))
    if contact:
        print("\n## verified contact\n")
        print(json.dumps({"contact_name": contact.get("contact_name"), "role": contact.get("role")}, indent=2))


if __name__ == "__main__":
    main()
