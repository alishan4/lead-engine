#!/usr/bin/env python3
"""
Daily pipeline report: funnel counts, agent usage, cache performance, and
top leads by opportunity confidence. Writes a markdown file to reports/ and
prints a summary to stdout.

Usage:
  python3 scripts/report_pipeline.py
  python3 scripts/report_pipeline.py --date 2026-09-01
"""
import argparse
from collections import Counter

from _lib import ROOT, PROSPECTS, LEADS, RANKINGS, read_jsonl, load_json

REPORTS = ROOT / "reports"
FULL_ROSTER_SIZE = 18   # actual claude-seo specialist count; see claude-seo/AGENTS.md
BASELINE_14_AGENTS = 14  # the "14 SEO agents" baseline named in the V1/V2 briefs


def load_leads():
    leads = []
    if not LEADS.exists():
        return leads
    for d in sorted(LEADS.iterdir()):
        if not d.is_dir():
            continue
        dossier = load_json(d / "dossier.json")
        email = load_json(d / "email.json")
        plan = load_json(d / "agent_plan_quick.json")
        deep_plan = load_json(d / "agent_plan_deep.json")
        opportunity = load_json(d / "opportunity.json")
        quick_audit = load_json(d / "quick_audit.json")
        contact = load_json(d / "contact.json")
        leads.append({
            "id": d.name,
            "dossier": dossier,
            "email": email,
            "plan": plan,
            "deep_plan": deep_plan,
            "opportunity": opportunity,
            "quick_audit": quick_audit,
            "contact": contact,
        })
    return leads


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="label for the report; defaults to 'latest'")
    args = ap.parse_args()
    label = args.date or "latest"

    discovered = read_jsonl(PROSPECTS / "discovered.jsonl")
    qualified = read_jsonl(PROSPECTS / "qualified.jsonl")
    rejected = read_jsonl(PROSPECTS / "rejected.jsonl")
    manual_review = read_jsonl(PROSPECTS / "manual_review.jsonl")
    needs_enrichment = read_jsonl(PROSPECTS / "needs_enrichment.jsonl")
    cache_log = read_jsonl(LEADS / "_cache_log.jsonl")

    rejected_before_ai = [
        r for r in rejected
        if r.get("reject_reason") in (
            "broken_or_non_legitimate", "no_commercial_intent", "score_below_threshold",
            "business_not_verified", "identity_confidence_below_threshold",
        )
    ]
    rejected_after_ai = [r for r in rejected if r not in rejected_before_ai]

    verified_businesses = [r for r in discovered if r.get("business_verified") is True]
    rescored = [r for r in discovered if r.get("score_before_enrichment") is not None]
    rank_import_files = list(RANKINGS.glob("*.csv")) if RANKINGS.exists() else []
    contact_unresolved = [r for r in discovered if r.get("status") == "CONTACT_UNVERIFIED"]
    reverify_required = [r for r in discovered if r.get("status") == "REVERIFY_REQUIRED"]

    leads = load_leads()
    quick_audits_run = [l for l in leads if l["quick_audit"]]
    opportunities_found = [l for l in leads if l["opportunity"] and not l["opportunity"].get("reject_lead")]
    dossiers_created = [l for l in leads if l["dossier"]]
    email_drafts = [l for l in leads if l["email"]]
    qa_pass = [l for l in email_drafts if l["email"]["qa"].get("verdict") == "PASS"]
    qa_rewrite = [l for l in email_drafts if l["email"]["qa"].get("verdict") == "REWRITE"]
    qa_reject = [l for l in email_drafts if l["email"]["qa"].get("verdict") == "REJECT"]
    qa_reverify = [l for l in email_drafts if l["email"]["qa"].get("verdict") == "REVERIFY_REQUIRED"]
    qa_decided = [l for l in email_drafts if l["email"]["qa"].get("verdict")]
    qa_pass_rate = round(100 * len(qa_pass) / len(qa_decided), 1) if qa_decided else None
    contacts_verified = [l for l in leads if l["contact"] and l["contact"].get("contact_verified")]

    agent_counter = Counter()
    agent_counts_per_lead = []
    for l in quick_audits_run:
        agents = (l["plan"] or {}).get("routed_agents", []) or (l["dossier"] or {}).get("agents_used", [])
        agent_counts_per_lead.append(len(agents))
        agent_counter.update(agents)
    for l in leads:
        if l["deep_plan"]:
            agent_counter.update(l["deep_plan"].get("routed_agents", []))

    avg_agents = (sum(agent_counts_per_lead) / len(agent_counts_per_lead)) if agent_counts_per_lead else 0

    cache_hits = sum(1 for c in cache_log if c["event"] == "hit")
    cache_misses = sum(1 for c in cache_log if c["event"] == "miss")

    naive_agent_calls = len(qualified) * FULL_ROSTER_SIZE if qualified else 0
    actual_agent_calls = sum(agent_counter.values())
    relative_savings_pct = (
        round(100 * (1 - actual_agent_calls / naive_agent_calls), 1)
        if naive_agent_calls else None
    )
    naive_14_agent_calls = len(qualified) * BASELINE_14_AGENTS if qualified else 0
    agent_calls_saved_vs_14_agent_baseline = naive_14_agent_calls - actual_agent_calls if qualified else None

    top_leads = sorted(
        [l for l in leads if l["opportunity"]],
        key=lambda l: l["opportunity"].get("confidence", 0),
        reverse=True,
    )[:10]

    lines = []
    lines.append(f"# Lead Engine Daily Report — {label}\n")
    lines.append("## Funnel\n")
    lines.append(f"- Discovered: {len(discovered)}")
    lines.append(f"- Business-verified: {len(verified_businesses)}")
    lines.append(f"- Rejected before any AI call (hard rules, low score, or failed identity verification): {len(rejected_before_ai)}")
    lines.append(f"- Needs enrichment (confirmed score low, potential score qualifies): {len(needs_enrichment)}")
    lines.append(f"- Rescored after enrichment: {len(rescored)}")
    lines.append(f"- Manual review (55-69, no material missing data): {len(manual_review)}")
    lines.append(f"- Qualified (>=70): {len(qualified)}")
    lines.append(f"- Quick audits run: {len(quick_audits_run)}")
    lines.append(f"- Opportunities found (confidence-passing): {len(opportunities_found)}")
    lines.append(f"- Rejected after AI review (weak/low-confidence): {len(rejected_after_ai)}")
    lines.append(f"- Dossiers created: {len(dossiers_created)}")
    lines.append(f"- Contacts verified: {len(contacts_verified)} | Contact-unresolved (blocked): {len(contact_unresolved)}")
    lines.append(f"- Email drafts created: {len(email_drafts)}")
    lines.append(f"  - QA PASS: {len(qa_pass)} | REWRITE: {len(qa_rewrite)} | REJECT: {len(qa_reject)} | "
                  f"REVERIFY_REQUIRED: {len(qa_reverify)}")
    lines.append(f"  - QA pass rate: {qa_pass_rate}%" if qa_pass_rate is not None else "  - QA pass rate: n/a (no QA decisions yet)")
    lines.append(f"- Reverify-required (stale evidence, blocked from outreach): {len(reverify_required)}")
    lines.append(f"- Ranking import files on hand: {len(rank_import_files)} ({', '.join(p.stem for p in rank_import_files) or 'none'})\n")

    lines.append("## Agent usage\n")
    lines.append(f"- Average agents per lead (quick audit): {avg_agents:.2f}")
    lines.append(f"- Agent call breakdown: {dict(agent_counter)}")
    if relative_savings_pct is not None:
        lines.append(
            f"- Estimated relative savings vs. running all {FULL_ROSTER_SIZE} specialists on every "
            f"qualified lead: {relative_savings_pct}% fewer agent invocations "
            f"({actual_agent_calls} actual vs. {naive_agent_calls} naive)"
        )
        lines.append(
            f"- agent_calls_saved_vs_14_agent_baseline: {agent_calls_saved_vs_14_agent_baseline} "
            f"({actual_agent_calls} actual vs. {naive_14_agent_calls} at 14 agents/lead)\n"
        )
    else:
        lines.append("- No qualified leads yet to compare against naive full-roster baseline.\n")

    lines.append("## Cache performance\n")
    lines.append(f"- Cache hits: {cache_hits}")
    lines.append(f"- Cache misses (rebuilt): {cache_misses}\n")

    lines.append("## Top leads by opportunity confidence\n")
    if top_leads:
        lines.append("| Business | Opportunity | Confidence |")
        lines.append("|---|---|---|")
        for l in top_leads:
            biz = (l["dossier"] or {}).get("business", l["id"])
            opp = l["opportunity"]["primary_opportunity"]
            conf = l["opportunity"]["confidence"]
            lines.append(f"| {biz} | {opp} | {conf} |")
    else:
        lines.append("_No opportunities recorded yet._")

    report_md = "\n".join(lines) + "\n"
    REPORTS.mkdir(parents=True, exist_ok=True)
    out_path = REPORTS / f"report-{label}.md"
    out_path.write_text(report_md)
    print(report_md)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
