#!/usr/bin/env python3
"""
Triage view -- Phase 15. Makes it obvious, at a glance, what's blocking each
lead that isn't already resolved (qualified-and-moving, or cleanly rejected).
Pure aggregation over data already on disk -- no AI, no network.

Usage:
  python3 scripts/triage_report.py
  python3 scripts/triage_report.py --date 2026-09-01
"""
import argparse

from _lib import ROOT, PROSPECTS, LEADS, read_jsonl, load_json, market_slug

REPORTS = ROOT / "reports"

TRIAGE_STATUSES = ("NEEDS_ENRICHMENT", "MANUAL_REVIEW", "CONTACT_UNVERIFIED", "REVERIFY_REQUIRED")

NEXT_ACTION = {
    "NEEDS_ENRICHMENT": (
        "Import ranking data for this market (Semrush export or manual CSV via "
        "import_rankings.py), then rescore_leads.py",
        "cheap_enrichment",
    ),
    "MANUAL_REVIEW": (
        "Human review: confirm ranking/competitive signal manually, or decide to drop",
        "manual_verification",
    ),
    "CONTACT_UNVERIFIED": (
        "Find a verifiable recipient (owner/marketing manager/company inbox with a "
        "public source) via verify_contact.py before drafting",
        "manual_verification",
    ),
    "REVERIFY_REQUIRED": (
        "Re-run the quick-audit/opportunity-selector steps (or refresh the ranking "
        "import) -- evidence is stale, not necessarily wrong",
        "claude_quick_audit",
    ),
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    args = ap.parse_args()
    label = args.date or "latest"

    records = read_jsonl(PROSPECTS / "discovered.jsonl")
    rows = [r for r in records if r.get("status") in TRIAGE_STATUSES]

    lines = [f"# Lead Engine Triage — {label}\n"]
    if not rows:
        lines.append("_Nothing blocked right now — every lead is either qualified-and-moving "
                      "or cleanly rejected._\n")
    else:
        lines.append(
            "| Business | Market | Status | Confirmed | Potential | Completeness | "
            "Missing data | Next action | Cost category |"
        )
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for r in sorted(rows, key=lambda r: r.get("potential_score") or r.get("score") or 0, reverse=True):
            market = market_slug(r.get("niche"), r.get("city"), r.get("state"))
            action, category = NEXT_ACTION.get(r["status"], ("Review manually", "manual_verification"))
            missing = ", ".join(r.get("missing_fields") or []) or "-"
            lines.append(
                f"| {r['business_name']} | {market} | {r['status']} | "
                f"{r.get('confirmed_score', r.get('score', '-'))} | "
                f"{r.get('potential_score', '-')} | {r.get('data_completeness', '-')} | "
                f"{missing} | {action} | {category} |"
            )

    report = "\n".join(lines) + "\n"
    REPORTS.mkdir(parents=True, exist_ok=True)
    out_path = REPORTS / f"triage-{label}.md"
    out_path.write_text(report)
    print(report)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
