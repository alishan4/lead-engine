#!/usr/bin/env python3
"""
V3.8.1/V3.8.2 -- the simplified discovery-only daily report. Deliberately much
shorter than report_pipeline.py/triage_report.py's full-funnel output --
discovery-only mode has no FIT/GAP/qualification/triage funnel to report,
so pretending it does would be misleading. Written to reports/report-latest.md
(the same "latest" filename convention every prior mode already used;
report-*.md is gitignored -- see .gitignore) whenever
config/acquisition.yaml: production_mode is discovery_only.

Pure rendering function (render_report) + a thin write_report() I/O
wrapper, matching the rest of this codebase's pure/impure split.
"""
import sys
from pathlib import Path

from _lib import ROOT, now_iso

REPORTS_DIR = ROOT / "reports"


def render_report(discovery_stats, candidates_sync_result):
    """Pure: (discovery_worker.run() stats, sync_handoff.sync_candidates()
    result) -> the markdown report text. Never reads a file itself."""
    lines = ["# Lead Engine Daily Report — DISCOVERY-ONLY DAILY RUN", ""]

    lines.append(f"Run completed at: {now_iso()}")
    lines.append(f"Trigger: {discovery_stats.get('trigger_type', 'NORMAL_SCHEDULE')}")
    lines.append("")
    lines.append(f"Candidates discovered: {discovery_stats.get('candidates_discovered', 0)}")
    lines.append(f"Businesses verified: {discovery_stats.get('candidates_verified', 0)}")
    lines.append(f"Candidates saved: {discovery_stats.get('candidates_saved', 0)}")
    lines.append(f"Duplicates skipped: {discovery_stats.get('duplicates_skipped', 0)}")
    lines.append(f"Verification failures: {discovery_stats.get('verification_failures', 0)}")
    lines.append("")

    lines.append("Markets explored:")
    markets = discovery_stats.get("markets_explored") or []
    if markets:
        for m in markets:
            lines.append(f"- {m}")
    else:
        lines.append("- (none this run)")
    lines.append("")

    sync_status = candidates_sync_result.get("candidates_sync_status", "NOT_RUN")
    sync_rows = candidates_sync_result.get("candidates_rows", 0)
    lines.append(f"Google Sheet CANDIDATES synced: {sync_rows} ({sync_status})")
    lines.append("")

    lines.append("Downstream qualification: SKIPPED")
    lines.append("Ranking enrichment: SKIPPED")
    lines.append("SEO agents: SKIPPED")
    lines.append("Contact research: SKIPPED")
    lines.append("Outreach drafting: SKIPPED")
    lines.append("Gmail: NOT ACCESSED")
    lines.append("")

    lines.append("## Cost control")
    lines.append("")
    lines.append(f"- Claude calls attempted: {discovery_stats.get('claude_calls_attempted', 0)} "
                  f"({discovery_stats.get('claude_calls_succeeded', 0)} succeeded, "
                  f"{discovery_stats.get('claude_calls_failed', 0)} failed)")
    lines.append(f"- Budget status: {discovery_stats.get('budget_status', 'OK')}")
    lines.append(f"- Budget accounting: {discovery_stats.get('budget_accounting_status', 'COMPLETE')}")
    lines.append(f"- Budget limit (USD/day): {discovery_stats.get('budget_limit_usd')}")
    obs_success = discovery_stats.get("observed_successful_call_cost_usd", 0.0)
    obs_failed = discovery_stats.get("observed_failed_call_cost_usd", 0.0)
    obs_total = discovery_stats.get("observed_total_cost_usd", 0.0)
    unknown_n = discovery_stats.get("unknown_cost_attempts", 0)
    incomplete_note = f" (INCOMPLETE -- {unknown_n} attempt(s) with unknown cost not included)" if unknown_n else ""
    lines.append(f"- Observed cost, successful calls (USD): {obs_success}")
    lines.append(f"- Observed cost, failed calls (USD): {obs_failed}")
    lines.append(f"- Observed total cost this run (USD): {obs_total}{incomplete_note}")
    remaining = discovery_stats.get("budget_remaining_usd")
    lines.append(f"- Budget remaining today (USD): {remaining if remaining is not None else 'UNKNOWN (accounting incomplete for today)'}")
    total_tokens = discovery_stats.get("total_tokens")
    lines.append(f"- Total tokens: {total_tokens if total_tokens is not None else 'UNKNOWN (not observable in this environment)'}")
    cpv = discovery_stats.get("cost_per_verified_candidate")
    cpv_note = " (based on total OBSERVED cost, including failed calls)" if cpv is not None else ""
    lines.append(f"- Cost per verified candidate (USD): {cpv if cpv is not None else 'UNKNOWN'}{cpv_note}")
    lines.append("")

    if discovery_stats.get("failures"):
        lines.append("## Failures (isolated, batch continued)")
        lines.append("")
        for f in discovery_stats["failures"]:
            who = f.get("prospect_id") or f.get("market_cell") or "?"
            lines.append(f"- {who}: {f.get('reason')}")
        lines.append("")

    lines.append("Run completed.")
    lines.append("")
    return "\n".join(lines)


def write_report(discovery_stats, candidates_sync_result, out_path=None):
    out_path = out_path or (REPORTS_DIR / "report-latest.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_report(discovery_stats, candidates_sync_result))
    return out_path


def main():
    print("report_discovery_only.py is invoked programmatically from run_daily.py -- "
          "no standalone CLI entry point (nothing meaningful to render without a real run's stats).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
