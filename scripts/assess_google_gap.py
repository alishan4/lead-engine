#!/usr/bin/env python3
"""
Basic Google-gap assessment -- V3.1. Fully deterministic (see
score_leads.score_gap): no page-fetching, no claude-seo agent, no full
audit. Reuses whatever structural signals are already on the prospect
record (service_page_count, obvious_website_issue/obvious_gbp_issue tags,
competitor_gap, known maps/organic positions) and the cached market file --
the same signals V2's quick-audit routing already relies on, scored on a
separate GAP axis instead of V2's single blended `score`.

Unknown rankings are never scored as poor rankings -- see score_gap()'s
confirmed/potential/completeness discipline.

Usage:
  python3 scripts/assess_google_gap.py --id <slug>
"""
import argparse

from _lib import PROSPECTS, LEADS, read_jsonl, write_jsonl, load_yaml, load_market, lead_dir, load_json, write_json, now_iso
from score_leads import score_gap


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

    result = score_gap(p, cfg, niches_cfg, market)

    for fname in ("discovered.jsonl", "qualified.jsonl", "manual_review.jsonl", "needs_enrichment.jsonl"):
        recs = read_jsonl(PROSPECTS / fname)
        changed = False
        for r in recs:
            if r["id"] == args.id:
                r["gap_confirmed_score"] = result["confirmed_score"]
                r["gap_potential_score"] = result["potential_score"]
                r["gap_completeness"] = result["completeness"]
                r["gap_missing_fields"] = result["missing_fields"]
                r["gap_breakdown"] = result["breakdown"]
                r["status"] = "GOOGLE_GAP_ASSESSED"
                changed = True
        if changed:
            write_jsonl(PROSPECTS / fname, recs)

    qual = load_json(lead_dir(args.id) / "qualification_v3.json") or {"prospect_id": args.id}
    qual["gap"] = {
        "confirmed_score": result["confirmed_score"], "potential_score": result["potential_score"],
        "completeness": result["completeness"], "missing_fields": result["missing_fields"],
        "breakdown": result["breakdown"], "gap_type": result["gap_type"],
        "evidence": [f"deterministic from prospect record + {('market cache' if market else 'no market cache')} "
                     f"as of {now_iso()}"],
    }
    write_json(lead_dir(args.id) / "qualification_v3.json", qual)

    print(f"{args.id}: GOOGLE_GAP_ASSESSED -- confirmed={result['confirmed_score']} "
          f"potential={result['potential_score']} completeness={result['completeness']}% "
          f"gap_type={result['gap_type']} missing={result['missing_fields']}")


if __name__ == "__main__":
    main()
