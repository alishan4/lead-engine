#!/usr/bin/env python3
"""
Stale-finding protection -- Phase 9. Run before email generation (also
enforced automatically as a hard gate inside generate_email.py, which calls
the same check_freshness() helper). No AI, no network -- just comparing
timestamps already on disk.

If the dossier's strongest finding is older than finding_freshness_days, or
it makes a ranking claim backed by data older than ranking_freshness_days
(or no dated ranking source at all), the prospect is marked
REVERIFY_REQUIRED and outreach is blocked until it's re-checked -- this is
always applied, regardless of how far the lead has already progressed,
since stale evidence should block outreach from whatever stage it's at.

If the evidence IS fresh, this script deliberately does NOT set a status --
"fresh" just means "no problem found," not "advance the pipeline." A lead
that's already past DOSSIER_READY (e.g. CONTACT_VERIFIED, EMAIL_DRAFT_READY)
must not be silently reset backward to DOSSIER_READY by a routine freshness
recheck; that was a real bug caught in production (see reports/).

Usage:
  python3 scripts/check_freshness.py --id <slug>
"""
import argparse

from _lib import load_yaml, lead_dir, load_json, check_freshness, set_status_everywhere


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    args = ap.parse_args()

    limits = load_yaml("limits.yaml")
    dossier = load_json(lead_dir(args.id) / "dossier.json")
    if not dossier:
        raise SystemExit(f"No dossier.json for {args.id}. Run build_dossier.py first.")

    is_fresh, reasons = check_freshness(dossier, limits)

    if is_fresh:
        print(f"{args.id}: FRESH -- evidence is current. Status left unchanged "
              "(freshness confirms no blocker, it does not advance or reset the pipeline stage).")
    else:
        set_status_everywhere(args.id, "REVERIFY_REQUIRED")
        print(f"{args.id}: REVERIFY_REQUIRED -- stale evidence, outreach blocked until re-checked:")
        for r in reasons:
            print(f"  - {r}")
        print("Re-run the quick-audit/opportunity-selector steps (or a fresh ranking import) "
              "before generating an email for this lead.")


if __name__ == "__main__":
    main()
