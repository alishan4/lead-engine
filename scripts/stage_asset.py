#!/usr/bin/env python3
"""
V3.2 asset staging -- purely deterministic templating from the already-
validated dossier/wedge, so that when outreach later says "want me to send
the short comparison?" a real, useful asset already exists. NO extra LLM
call: this script never rephrases already-structured evidence with a model,
per the explicit V3.2 cost rule. Never a PDF -- structured JSON, renderable
as Markdown.

Usage:
  python3 scripts/stage_asset.py --id <slug>
"""
import argparse

from _lib import PROSPECTS, load_yaml, read_jsonl, lead_dir, load_json, write_json, set_status_everywhere, now_iso


def asset_allowed(status):
    """Pure gate, directly unit-testable: an asset may only be staged after DOSSIER_READY."""
    return status == "DOSSIER_READY"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    args = ap.parse_args()

    p = next((r for r in read_jsonl(PROSPECTS / "discovered.jsonl") if r["id"] == args.id), None)
    if not p:
        raise SystemExit(f"Prospect {args.id} not found in discovered.jsonl")
    if not asset_allowed(p.get("status")):
        raise SystemExit(f"{args.id}: status is {p.get('status')!r}, not DOSSIER_READY -- "
                          "an asset is only staged after the dossier exists.")

    dossier = load_json(lead_dir(args.id) / "intelligence_dossier.json")
    if not dossier:
        raise SystemExit(f"No intelligence_dossier.json for {args.id}.")

    router_cfg = load_yaml("opportunity_router.yaml")
    limits = load_yaml("limits.yaml")
    wedge = dossier["primary_wedge"]
    asset_type = router_cfg["asset_type_map"].get(wedge["opportunity_type"], "ONE_PAGE_OPPORTUNITY_SUMMARY")

    competitor_names = [c.get("name") for c in dossier["competitor_context"] if isinstance(c, dict) and c.get("name")]
    market_comparison = None
    if competitor_names:
        market_comparison = f"Compared with: {', '.join(competitor_names[:3])}."

    max_obs = limits["max_wedge_observations_in_asset"]
    observations = [wedge["observation"]][:max_obs]  # deterministic templates carry exactly ONE validated wedge

    evidence_refs = []
    for e in wedge.get("evidence", []):
        if isinstance(e, dict):
            ref = e.get("source") or e.get("source_type")
            if ref:
                evidence_refs.append(str(ref))

    business_name = dossier["business"]["name"]
    asset = {
        "asset_type": asset_type,
        "title": f"{business_name} — {wedge['opportunity_type'].replace('_', ' ').title()}",
        "sections": {
            "what_i_noticed": wedge["observation"],
            "prospect_state": (
                f"{business_name} ({dossier['business']['niche']}, {dossier['business']['city']}, "
                f"{dossier['business']['state']}) -- FIT {dossier['qualification']['fit']['confirmed']}, "
                f"GAP {dossier['qualification']['gap']['confirmed']}."
            ),
            "market_comparison": market_comparison,
            "observations": observations,
            "recommended_first_action": wedge["recommended_first_action"],
            "evidence_references": evidence_refs,
            "limitations": dossier["limitations"],
        },
        "generated_at": now_iso(),
        "source_wedge_confidence": wedge["confidence"],
    }

    write_json(lead_dir(args.id) / "staged_asset.json", asset)
    set_status_everywhere(args.id, "ASSET_STAGED")
    print(f"{args.id}: ASSET_STAGED -- {asset_type}, {len(observations)} observation(s), "
          f"{len(evidence_refs)} evidence reference(s). STOP (V3.2 complete for this lead).")


if __name__ == "__main__":
    main()
