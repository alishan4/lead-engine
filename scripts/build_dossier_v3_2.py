#!/usr/bin/env python3
"""
V3.2 compact intelligence dossier -- built only after OPPORTUNITY_IDENTIFIED.
Pure aggregation of already-produced, already-evidenced data (V1/V2/V3.1/
V3.1.1 qualification fields + primary_wedge.json) -- no new research, no
agent call, no LLM call. Distinct from V2's schemas/dossier.schema.json
(the email-generation dossier).

Usage:
  python3 scripts/build_dossier_v3_2.py --id <slug>
"""
import argparse

from _lib import PROSPECTS, read_jsonl, load_market, lead_dir, load_json, write_json, set_status_everywhere, now_iso


def dossier_allowed(status):
    """Pure gate, directly unit-testable: a dossier may only be built after OPPORTUNITY_IDENTIFIED."""
    return status == "OPPORTUNITY_IDENTIFIED"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    args = ap.parse_args()

    p = next((r for r in read_jsonl(PROSPECTS / "discovered.jsonl") if r["id"] == args.id), None)
    if not p:
        raise SystemExit(f"Prospect {args.id} not found in discovered.jsonl")
    if not dossier_allowed(p.get("status")):
        raise SystemExit(f"{args.id}: status is {p.get('status')!r}, not OPPORTUNITY_IDENTIFIED -- "
                          "a dossier is only built after a wedge has been selected.")

    wedge = load_json(lead_dir(args.id) / "primary_wedge.json")
    if not wedge:
        raise SystemExit(f"No primary_wedge.json for {args.id}.")

    market = load_market(p.get("niche"), p.get("city"), p.get("state"))
    qual = load_json(lead_dir(args.id) / "qualification_v3.json") or {}

    limitations = []
    if p.get("fit_missing_fields"):
        limitations.append(f"FIT missing fields: {p['fit_missing_fields']}")
    if p.get("gap_missing_fields"):
        limitations.append(f"GAP missing fields: {p['gap_missing_fields']}")
    if not wedge.get("why_now"):
        limitations.append("why_now is null -- no verified timing/acquisition signal exists for this lead")
    if p.get("contactability_score", 2) < 2:
        limitations.append(f"contactability_score={p.get('contactability_score')} -- final contact verification (verify_contact.py) still required and unchanged by V3.2")

    dossier = {
        "business": {
            "name": p.get("business_name"), "niche": p.get("niche"),
            "city": p.get("city"), "state": p.get("state"), "website": p.get("website"),
        },
        "market": {
            "market_id": None if not market else f"{p.get('niche')}-{p.get('city')}-{p.get('state')}".lower().replace(" ", "-"),
            "top_competitors": (market or {}).get("top_competitors", []),
            "review_benchmark": (market or {}).get("review_benchmarks", {}).get("median_top3"),
        },
        "qualification": {
            "fit": {"confirmed": p.get("fit_confirmed_score"), "potential": p.get("fit_potential_score"), "completeness": p.get("fit_completeness")},
            "gap": {"confirmed": p.get("gap_confirmed_score"), "potential": p.get("gap_potential_score"), "completeness": p.get("gap_completeness")},
            "buying_signals": [k for k in (
                "runs_google_ads", "runs_lsa", "recent_expansion", "new_location",
                "marketing_hiring_signal", "review_velocity_signal", "recent_site_investment",
                "new_high_value_service", "multiple_locations",
            ) if p.get(k) is not None],
        },
        "primary_wedge": wedge,
        "competitor_context": (market or {}).get("top_competitors", [])[:3],
        "decision_maker_context": {
            "likely_contact_role": p.get("likely_contact_role"),
            "contactability_score": p.get("contactability_score"),
            "note": "Not a verified contact -- verify_contact.py (unchanged, out of scope for V3.2) is still required before any outreach.",
        },
        "contactability": {"score": p.get("contactability_score"), "status_at_dossier_time": p.get("status")},
        "sources": sorted({e.get("source") for e in wedge.get("evidence", []) if isinstance(e, dict) and e.get("source")} |
                            {e.get("source_type") for e in wedge.get("evidence", []) if isinstance(e, dict) and e.get("source_type")}),
        "observed_at": now_iso(),
        "limitations": limitations,
        "cost_metrics": qual.get("intelligence_cost"),
    }

    write_json(lead_dir(args.id) / "intelligence_dossier.json", dossier)
    set_status_everywhere(args.id, "DOSSIER_READY")
    print(f"{args.id}: DOSSIER_READY -- {len(limitations)} limitation(s) noted, "
          f"wedge={wedge['opportunity_type']} (confidence={wedge['confidence']})")


if __name__ == "__main__":
    main()
