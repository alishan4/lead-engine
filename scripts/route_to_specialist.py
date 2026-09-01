#!/usr/bin/env python3
"""
V3.2 Path B/C: routes ambiguous candidates (requires_specialist=True) to
exactly ONE claude-seo specialist at a time, validates the result, and
decides OPPORTUNITY_IDENTIFIED / SECOND_OPINION_REQUIRED / NO_DEFENSIBLE_WEDGE.
Hard cap of 2 specialist calls total per prospect, enforced in code (not
just by convention) via `specialist_calls_used` on the qualification_v3.json
record. A weak first result does NOT justify a second call unless the lead
is HIGH_PRIORITY, the result was genuinely (not just weakly) ambiguous, or a
materially different second commercial dimension is still on the table.

Usage:
  python3 scripts/route_to_specialist.py --id <slug> --print-context
  python3 scripts/route_to_specialist.py --id <slug> --save result.json
"""
import argparse
import json
import sys

from _lib import (
    ROOT, PROSPECTS, load_yaml, read_jsonl, lead_dir, load_json, write_json,
    set_status_everywhere, now_iso, load_market,
)
from wedge_selection import select_primary_wedge, commercial_mechanism_is_defensible
from run_deterministic_scan import finalize_wedge

PROMPT_PATH = ROOT / "prompts" / "opportunity-specialist.md"


def get_prospect(prospect_id):
    for r in read_jsonl(PROSPECTS / "discovered.jsonl"):
        if r["id"] == prospect_id:
            return r
    return None


def decide_after_specialist(confidence, qualification_tier, pending_second_dimension_exists, calls_used, limits):
    """
    Pure decision function -- the actual agent stop-rule logic, extracted so
    it's directly unit-testable without file I/O. Returns one of
    "VIABLE" (confidence clears the usable threshold -- proceed to wedge
    selection), "SECOND_OPINION" (weak/borderline but justified), or "STOP"
    (weak result, no justification -- NO_DEFENSIBLE_WEDGE, never call a
    second agent just to try to manufacture a reason).
    """
    threshold = limits["usable_confidence_threshold"]
    ambiguity_floor = limits["genuine_ambiguity_min_confidence"]

    if confidence >= threshold:
        return "VIABLE", "confidence clears the usable threshold"

    if calls_used >= 2:
        return "STOP", "2 specialist calls already used -- hard cap, no further agents regardless of confidence"

    genuinely_ambiguous = ambiguity_floor <= confidence < threshold
    if qualification_tier == "HIGH_PRIORITY":
        return "SECOND_OPINION", "HIGH_PRIORITY tier justifies a second opinion"
    if genuinely_ambiguous:
        return "SECOND_OPINION", f"confidence {confidence} is genuinely ambiguous (>= {ambiguity_floor}), not just weak"
    if pending_second_dimension_exists:
        return "SECOND_OPINION", "a materially different, still-promising opportunity dimension remains unexamined"
    return "STOP", (f"confidence {confidence} is weak (< {ambiguity_floor}), not HIGH_PRIORITY, and no material "
                     "second dimension exists -- a weak result alone never justifies another agent call")



def pick_specialist(candidates, router_cfg, already_used, prefer_second_opinion_pair_for=None):
    never_run = set(router_cfg.get("never_auto_run", []))
    if prefer_second_opinion_pair_for:
        pair = router_cfg.get("second_opinion_pairs", {}).get(prefer_second_opinion_pair_for, [])
        for agent in pair:
            if agent not in already_used and agent not in never_run:
                return agent
        return None
    pending = [c for c in candidates if c.get("requires_specialist") and c.get("type") != "NO_CLEAR_OPPORTUNITY"]
    pending.sort(key=lambda c: c["commercial_relevance"], reverse=True)
    for c in pending:
        agents = router_cfg["opportunity_specialist_map"].get(c["type"], [])
        for agent in agents:
            if agent not in already_used and agent not in never_run:
                return agent, c
    return None, None


def build_specialist_context(p, candidate, market):
    """The compact specialist input contract -- never the full raw record or a site dump."""
    return {
        "verified_business": {"name": p.get("business_name"), "city": p.get("city"), "state": p.get("state"),
                               "website": p.get("website"), "niche": p.get("niche")},
        "fit_gap_summary": {"fit_confirmed": p.get("fit_confirmed_score"), "gap_confirmed": p.get("gap_confirmed_score"),
                              "gap_type": None},
        "suspected_opportunity": {"type": candidate["type"], "statement": candidate["statement"],
                                    "confidence": candidate["confidence"], "evidence": candidate["evidence"]},
        "market_context": {
            "top_competitors": (market or {}).get("top_competitors", []),
            "review_benchmark": (market or {}).get("review_benchmarks", {}).get("median_top3"),
            "common_service_architecture": (market or {}).get("common_service_architecture", []),
        },
        "verified_ranking": {"maps_position": p.get("maps_position"), "organic_position": p.get("organic_position")},
        "relevant_buying_signals": {k: p.get(k) for k in ("runs_google_ads", "runs_lsa", "paid_search_organic_gap") if p.get(k) is not None},
        "question": (
            f"Determine whether this prospect has a commercially meaningful {candidate['type'].replace('_', ' ').lower()} "
            f"for its core money keyword, compared with the named local competitors above. "
            f"Do not perform a general SEO audit -- answer only this question."
        ),
    }


def ingest_specialist_output(output):
    """Validates shape and strips any new_facts entry lacking evidence -- never silently kept."""
    kept_facts, dropped = [], []
    for fact in output.get("new_facts", []):
        if fact.get("evidence"):
            kept_facts.append(fact)
        else:
            dropped.append(fact.get("statement", "<unnamed>"))
    output["new_facts"] = kept_facts
    return output, dropped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    ap.add_argument("--print-context", action="store_true")
    ap.add_argument("--save")
    args = ap.parse_args()

    p = get_prospect(args.id)
    if not p:
        raise SystemExit(f"Prospect {args.id} not found in discovered.jsonl")

    candidates_doc = load_json(lead_dir(args.id) / "opportunity_candidates.json")
    if not candidates_doc:
        raise SystemExit(f"No opportunity_candidates.json for {args.id} -- run run_deterministic_scan.py first.")
    candidates = candidates_doc["candidates"]

    router_cfg = load_yaml("opportunity_router.yaml")
    limits = load_yaml("limits.yaml")
    market = load_market(p.get("niche"), p.get("city"), p.get("state"))

    qual = load_json(lead_dir(args.id) / "qualification_v3.json") or {"prospect_id": args.id}
    specialist_findings = qual.get("specialist_findings", [])
    agents_used = [f["specialist"] for f in specialist_findings]
    calls_used = len(specialist_findings)

    if args.save:
        raw = sys.stdin.read() if args.save == "-" else open(args.save).read()
        output = json.loads(raw)
        output, dropped_facts = ingest_specialist_output(output)

        ok, why = commercial_mechanism_is_defensible(output.get("commercial_mechanism", ""))
        if not ok:
            output["commercial_mechanism"] = output.get("finding", "")
            output.setdefault("limitations", []).append(f"guarded: {why} -- fell back to raw finding text")

        specialist_findings.append(output)
        qual["specialist_findings"] = specialist_findings
        qual["dropped_unsupported_facts"] = qual.get("dropped_unsupported_facts", []) + dropped_facts
        set_status_everywhere(args.id, "SPECIALIST_ANALYSIS_COMPLETE")

        confidence = output.get("confidence", 0)
        threshold = limits["usable_confidence_threshold"]

        # Fold the specialist finding into a real candidate for wedge selection.
        # Attribute it to whichever opportunity type this specialist was
        # actually routed for (recorded by --print-context), never guessed
        # from matching hypothesis text against candidate statements.
        routed_type = candidates_doc.get("_last_routed_type") or (candidates[0]["type"] if candidates else "NO_CLEAR_OPPORTUNITY")
        specialist_candidate = {
            "type": routed_type,
            "statement": output["finding"], "evidence": [{"statement": e, "source": None, "source_type": "specialist_finding", "observed_at": now_iso()} for e in output.get("evidence", [])],
            "confidence": confidence, "commercial_relevance": 0.75, "specificity": 0.7, "actionability": 0.6,
            "requires_specialist": False, "commercial_mechanism": output["commercial_mechanism"],
            "recommended_action": output.get("recommended_action"), "source_stage": "specialist",
        }

        known_terms = [c["name"] for c in (market or {}).get("top_competitors", []) if isinstance(c, dict) and c.get("name")]

        if confidence >= threshold:
            best, score, why = select_primary_wedge([specialist_candidate], known_terms, router_cfg["wedge_weights"])
            if best:
                wedge = finalize_wedge(p, best, score, agents_used=agents_used + [output["specialist"]])
                write_json(lead_dir(args.id) / "primary_wedge.json", wedge)
                set_status_everywhere(args.id, "OPPORTUNITY_IDENTIFIED", extra_fields={
                    "primary_wedge_type": wedge["opportunity_type"], "primary_wedge_confidence": wedge["confidence"],
                    "intelligence_agents_used": agents_used + [output["specialist"]],
                })
                write_json(lead_dir(args.id) / "qualification_v3.json", qual)
                print(f"{args.id}: OPPORTUNITY_IDENTIFIED ({len(agents_used) + 1} agent(s) used) -- {wedge['opportunity_type']}, wedge_score={score}")
                return
            # High confidence but failed the company-swap/specificity test.
            reason = why
        else:
            reason = f"specialist confidence {confidence} below usable threshold {threshold}"

        # Confidence/specificity insufficient -- decide stop vs second opinion
        # via the shared pure stop-rule function. A "material second
        # dimension" means a DIFFERENT opportunity type, not yet analyzed,
        # that's still commercially promising on its own -- not just "an
        # agent name we haven't called yet" (agents_used tracks specialist
        # names, which never equal an opportunity `type` string).
        pending_second_dimension = [
            c for c in candidates
            if c.get("requires_specialist") and c["type"] != routed_type and c["commercial_relevance"] >= 0.6
        ]
        decision, decision_reason = decide_after_specialist(
            confidence, p.get("qualification_tier"), bool(pending_second_dimension), calls_used, limits,
        )

        write_json(lead_dir(args.id) / "qualification_v3.json", qual)

        if decision == "SECOND_OPINION":
            set_status_everywhere(args.id, "SECOND_OPINION_REQUIRED")
            print(f"{args.id}: SECOND_OPINION_REQUIRED -- {reason}. Justification: {decision_reason}. "
                  "Run --print-context again for the second (and final) specialist call.")
        else:
            set_status_everywhere(args.id, "NO_DEFENSIBLE_WEDGE", extra_fields={"no_defensible_wedge_reason": reason})
            print(f"{args.id}: NO_DEFENSIBLE_WEDGE -- {reason} ({decision_reason}). No further specialist calls -- "
                  "a weak result alone is never grounds to call another agent hoping to manufacture a reason to contact this business.")
        return

    # --print-context
    if calls_used >= 2:
        raise SystemExit(f"{args.id}: 2 specialist calls already used -- the hard cap forbids a third. "
                          "This should never be reached by normal V3.2 orchestration.")
    if calls_used == 0:
        agent, candidate = pick_specialist(candidates, router_cfg, agents_used)
    else:
        last_type = candidates_doc.get("_last_routed_type")
        agent = pick_specialist(candidates, router_cfg, agents_used, prefer_second_opinion_pair_for=last_type)
        candidate = next((c for c in candidates if c["type"] == last_type), candidates[0] if candidates else None)

    if not agent or not candidate:
        raise SystemExit(f"{args.id}: no eligible specialist found to route -- check config/opportunity_router.yaml coverage")

    candidates_doc["_last_routed_type"] = candidate["type"]
    write_json(lead_dir(args.id) / "opportunity_candidates.json", candidates_doc)
    set_status_everywhere(args.id, "AGENT_ROUTED")

    context = build_specialist_context(p, candidate, market)
    prompt = PROMPT_PATH.read_text()
    print(f"## ROUTE TO: {agent}\n")
    print(prompt)
    print("\n---\n## compact specialist context\n")
    print(json.dumps(context, indent=2))


if __name__ == "__main__":
    main()
