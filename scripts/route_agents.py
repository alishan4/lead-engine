#!/usr/bin/env python3
"""
Decide which claude-seo specialist(s) to invoke for one qualified prospect's
quick audit (or, with --stage deep, its deep audit). This script never calls
an agent itself -- it only produces a capped, deterministic plan. A human or
Claude (in this session) then actually invokes the Agent tool per the plan
and feeds results back into build_dossier.py.

Problem types are INFERRED from discovery-time signals (obvious_website_issue,
obvious_gbp_issue, competitor_gap, maps/organic position) so routing can
happen before any AI has looked at the lead. Deep-audit routing instead takes
the problem_type chosen by the opportunity-selector step.

Usage:
  python3 scripts/route_agents.py --id roofing-charlotte-nc-kingdom-roofing
  python3 scripts/route_agents.py --id ... --stage deep --problem-type gbp_gap
"""
import argparse
import json
import sys

from _lib import PROSPECTS, load_yaml, read_jsonl, lead_dir, write_json


def infer_problem_types(p):
    issues = set(p.get("obvious_website_issue") or [])
    gbp_issues = set(p.get("obvious_gbp_issue") or [])
    types = []

    if gbp_issues:
        types.append("gbp_gap")
    if "thin_service_pages" in issues or (p.get("service_page_count") or 99) <= 2:
        types.append("service_architecture_gap")
    if issues & {"slow_site", "no_https", "broken_links", "no_schema_markup"}:
        types.append("technical_gap")
    if "no_schema_markup" in issues:
        types.append("entity_nap_gap")
    if issues & {"weak_cta", "no_online_booking", "no_visible_phone", "no_contact_form"}:
        types.append("conversion_gap")
    if "slow_load" in issues or "poor_performance" in issues:
        types.append("performance_gap")
    if "thin_content" in issues:
        types.append("content_gap")
    if p.get("maps_position") and p["maps_position"] > 3:
        types.append("ranking_gap")
    if p.get("competitor_gap"):
        types.append("competitor_gap")
    if "outdated_design" in issues:
        types.append("website_gap")

    seen = set()
    ordered = []
    for t in types:
        if t not in seen:
            seen.add(t)
            ordered.append(t)
    return ordered or ["website_gap"]  # always route at least one generalist check


def build_plan(problem_types, routing_cfg, limits_cfg, stage="quick"):
    never = set(routing_cfg.get("never_auto_run", []))
    table = routing_cfg["quick_audit_routes"] if stage == "quick" else routing_cfg["deep_audit_routes"]
    cap = limits_cfg["max_quick_agents"] if stage == "quick" else limits_cfg["max_deep_agents"]

    agents = []
    for pt in problem_types:
        route = table.get(pt)
        if not route:
            continue
        route_agents_list = route["agents"] if stage == "quick" else route
        for a in route_agents_list:
            if a in never:
                continue
            if a not in agents:
                agents.append(a)
        if len(agents) >= cap:
            break
    agents = agents[:cap]
    return agents


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    ap.add_argument("--stage", choices=["quick", "deep"], default="quick")
    ap.add_argument("--problem-type", action="append", default=[])
    ap.add_argument(
        "--prospect-json",
        help="Path to a standalone prospect JSON file (or '-' for stdin), used instead of "
        "looking the id up in the shared qualified.jsonl. Use this for isolated/parallel "
        "runs so concurrent callers never read or write the shared file.",
    )
    args = ap.parse_args()

    routing_cfg = load_yaml("routing.yaml")
    limits_cfg = load_yaml("limits.yaml")

    if args.prospect_json:
        raw = sys.stdin.read() if args.prospect_json == "-" else open(args.prospect_json).read()
        p = json.loads(raw)
    else:
        qualified = {r["id"]: r for r in read_jsonl(PROSPECTS / "qualified.jsonl")}
        p = qualified.get(args.id)
        if not p:
            raise SystemExit(f"Prospect {args.id} not found in qualified.jsonl")

    problem_types = args.problem_type or infer_problem_types(p)
    agents = build_plan(problem_types, routing_cfg, limits_cfg, stage=args.stage)

    plan = {
        "prospect_id": args.id,
        "stage": args.stage,
        "inferred_problem_types": problem_types,
        "routed_agents": agents,
        "agent_count": len(agents),
        "cap_applied": limits_cfg["max_quick_agents"] if args.stage == "quick" else limits_cfg["max_deep_agents"],
    }
    out_path = lead_dir(args.id) / f"agent_plan_{args.stage}.json"
    write_json(out_path, plan)
    print(json.dumps(plan, indent=2))
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
