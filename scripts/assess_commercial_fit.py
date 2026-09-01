#!/usr/bin/env python3
"""
Commercial-fit assessment -- V3.1. Fully deterministic (see
score_leads.score_fit): niche tier (niches.yaml) + business maturity
signals + buying intent + contactability + market attractiveness (cached
market file). No claude-seo agent, no new research call of its own -- it
only aggregates fields other steps already gathered.

FIT answers "would this be a commercially attractive client?" -- NOT "is
their SEO bad?". A business with terrible SEO and no buying/maturity/
contactability signal still scores low on FIT.

Called TWICE in the normal pipeline, same script, same logic, always
recomputed from whatever's on the record right now (idempotent):
  1. Right after the franchise check (before buying signals/contactability
     exist yet) -- sets status COMMERCIAL_FIT_ASSESSED. buying_intent and
     contactability count as "missing" at this point, which correctly
     shows up in fit_potential_score vs. fit_confirmed_score.
  2. After assess_buying_signals.py and check_contactability.py have run --
     sets status FIT_SCORED, now with a materially more complete picture.

Usage:
  python3 scripts/assess_commercial_fit.py --id <slug>
"""
import argparse

from _lib import PROSPECTS, read_jsonl, write_jsonl, load_yaml, load_market, lead_dir, load_json, write_json, now_iso, load_franchise_blocklist
from score_leads import score_fit


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    args = ap.parse_args()

    discovered = read_jsonl(PROSPECTS / "discovered.jsonl")
    p = next((r for r in discovered if r["id"] == args.id), None)
    if not p:
        raise SystemExit(f"Prospect {args.id} not found in discovered.jsonl")

    cfg = load_yaml("scoring.yaml")
    niches_cfg = load_yaml("niches.yaml")
    market = load_market(p.get("niche"), p.get("city"), p.get("state"))
    blocklist = load_franchise_blocklist()

    result = score_fit(p, cfg, niches_cfg, market, blocklist)

    buying_signals_done = p.get("buying_signal_evidence") is not None or p.get("status") not in (None, "BUSINESS_VERIFIED")
    contactability_done = p.get("contactability_score") is not None
    next_status = "FIT_SCORED" if (buying_signals_done and contactability_done) else "COMMERCIAL_FIT_ASSESSED"
    # Never regress a lead that's already further along than this checkpoint
    # (e.g. re-running this script on a QUALIFIED lead to spot-check FIT
    # must not reset its status) -- only advance when currently at or before
    # the relevant checkpoint.
    advance_from = {"BUSINESS_VERIFIED", "COMMERCIAL_FIT_ASSESSED", "BUYING_SIGNALS_ASSESSED",
                     "CONTACTABILITY_CHECK", "GOOGLE_GAP_ASSESSED"}
    should_advance = p.get("status") in advance_from

    for fname in ("discovered.jsonl", "qualified.jsonl", "manual_review.jsonl", "needs_enrichment.jsonl"):
        recs = read_jsonl(PROSPECTS / fname)
        changed = False
        for r in recs:
            if r["id"] == args.id:
                r["fit_confirmed_score"] = result["confirmed_score"]
                r["fit_potential_score"] = result["potential_score"]
                r["fit_completeness"] = result["completeness"]
                r["fit_missing_fields"] = result["missing_fields"]
                r["fit_breakdown"] = result["breakdown"]
                if should_advance:
                    r["status"] = next_status
                changed = True
        if changed:
            write_jsonl(PROSPECTS / fname, recs)

    qual = load_json(lead_dir(args.id) / "qualification_v3.json") or {"prospect_id": args.id}
    qual["fit"] = {
        "confirmed_score": result["confirmed_score"], "potential_score": result["potential_score"],
        "completeness": result["completeness"], "missing_fields": result["missing_fields"],
        "breakdown": result["breakdown"],
        "evidence": [f"deterministic aggregation (niche tier, maturity signals, buying intent, "
                     f"contactability, market cache) as of {now_iso()}"],
    }
    write_json(lead_dir(args.id) / "qualification_v3.json", qual)

    print(f"{args.id}: {next_status if should_advance else p.get('status') + ' (unchanged, already past this checkpoint)'} "
          f"-- confirmed={result['confirmed_score']} potential={result['potential_score']} "
          f"completeness={result['completeness']}% missing={result['missing_fields']}")


if __name__ == "__main__":
    main()
