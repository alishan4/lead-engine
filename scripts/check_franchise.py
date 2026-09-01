#!/usr/bin/env python3
"""
Franchise / corporate-marketing-control check -- V3.1, runs right after
BUSINESS_VERIFIED. Two tiers, cost-ordered:

  1. FREE, deterministic blocklist match (config/franchise_blocklist.yaml).
     No match -> possible_franchise=False, done, zero cost, status
     unchanged (still BUSINESS_VERIFIED, ready for assess_commercial_fit.py).

  2. Only on a match: a research pass (prompts/franchise-check.md) decides
     corporate_marketing_controlled and lead_gen_network for THIS specific
     location. A brand match alone never auto-rejects -- many franchisees
     are independently owned and buy their own local SEO.

Usage:
  python3 scripts/check_franchise.py --id <slug>              # deterministic pass
  python3 scripts/check_franchise.py --id <slug> --print-prompt  # only if escalated
  python3 scripts/check_franchise.py --id <slug> --save result.json
"""
import argparse
import json
import sys

from _lib import (
    ROOT, PROSPECTS, LEADS, lead_dir, read_jsonl, load_json, write_json,
    set_status_everywhere, match_franchise_blocklist, now_iso,
)

PROMPT_PATH = ROOT / "prompts" / "franchise-check.md"


def get_prospect(prospect_id):
    for r in read_jsonl(PROSPECTS / "discovered.jsonl"):
        if r["id"] == prospect_id:
            return r
    return None


def apply_result(p, result):
    """Pure decision logic -- unit-testable without file I/O."""
    p["possible_franchise"] = result.get("possible_franchise")
    p["corporate_marketing_controlled"] = result.get("corporate_marketing_controlled")
    p["lead_gen_network"] = result.get("lead_gen_network")
    p["franchise_evidence"] = result.get("evidence", [])

    if result.get("lead_gen_network") is True:
        return "LEAD_GEN_NETWORK"
    if result.get("corporate_marketing_controlled") is True:
        return "CORPORATE_MARKETING_LOCK"
    if result.get("possible_franchise") and result.get("corporate_marketing_controlled") is None:
        return "FRANCHISE_REVIEW_REQUIRED"
    return None  # clear to proceed -- status left unchanged


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    ap.add_argument("--print-prompt", action="store_true")
    ap.add_argument("--save")
    args = ap.parse_args()

    p = get_prospect(args.id)
    if not p:
        raise SystemExit(f"Prospect {args.id} not found in discovered.jsonl")

    category, pattern = match_franchise_blocklist(p.get("business_name"), p.get("website"))

    if args.save:
        raw = sys.stdin.read() if args.save == "-" else open(args.save).read()
        result = json.loads(raw)
        stop_status = apply_result(p, result)

        qual = load_json(lead_dir(args.id) / "qualification_v3.json") or {}
        qual["prospect_id"] = args.id
        qual["franchise"] = {
            "possible_franchise": result.get("possible_franchise"),
            "corporate_marketing_controlled": result.get("corporate_marketing_controlled"),
            "lead_gen_network": result.get("lead_gen_network"),
            "blocklist_match": pattern,
            "research_escalated": True,
            "evidence": result.get("evidence", []),
        }
        write_json(lead_dir(args.id) / "qualification_v3.json", qual)

        if stop_status:
            set_status_everywhere(args.id, stop_status)
            print(f"{args.id}: {stop_status}")
        else:
            print(f"{args.id}: franchise check clear (researched) -- status unchanged ({p['status']}), "
                  "ready for assess_commercial_fit.py")
        return

    if not (category and pattern):
        # Free path: no blocklist match at all, zero research cost.
        p["possible_franchise"] = False
        p["corporate_marketing_controlled"] = False
        p["lead_gen_network"] = False
        p["franchise_evidence"] = []
        discovered = read_jsonl(PROSPECTS / "discovered.jsonl")
        for r in discovered:
            if r["id"] == args.id:
                r.update({k: p[k] for k in
                          ("possible_franchise", "corporate_marketing_controlled", "lead_gen_network", "franchise_evidence")})
        from _lib import write_jsonl
        write_jsonl(PROSPECTS / "discovered.jsonl", discovered)

        qual = load_json(lead_dir(args.id) / "qualification_v3.json") or {}
        qual["prospect_id"] = args.id
        qual["franchise"] = {
            "possible_franchise": False, "corporate_marketing_controlled": False,
            "lead_gen_network": False, "blocklist_match": None,
            "research_escalated": False, "evidence": [],
        }
        write_json(lead_dir(args.id) / "qualification_v3.json", qual)
        print(f"{args.id}: no franchise blocklist match, zero cost -- clear to proceed "
              f"(status unchanged: {p['status']})")
        return

    if args.print_prompt:
        prompt = PROMPT_PATH.read_text()
        print(prompt)
        print(f"\n---\n## blocklist match: category={category}, pattern={pattern!r}\n")
        print(json.dumps(p, indent=2))
        return

    print(f"{args.id}: blocklist match ({category}: {pattern!r}) -- run with --print-prompt "
          "to get the research prompt, then --save the result. No status change yet.")


if __name__ == "__main__":
    main()
