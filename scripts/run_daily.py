#!/usr/bin/env python3
"""
Lead Engine daily orchestration (Tue-Fri automated run).

V3.5 UPDATE (see OPERATING-RULES.md Sec.4 and docs/AUTOMATION.md for the
full policy change): this script now runs a real, unattended Claude
acquisition worker (scripts/acquisition_worker.py) BEFORE the deterministic
loop below -- it advances pending leads through business verification,
commercial FIT, buying-signal evidence, contactability, GAP, deterministic
intelligence, capped specialist escalation, and contact-identity
verification, and performs bounded fresh-prospect discovery, all under a
fail-closed auth preflight, a wall-clock timeout, and a Read/WebSearch/
WebFetch-only tool profile that makes the Gmail/contact-form/arbitrary-
write boundaries structural rather than merely promptable (see
claude_invoke.py). Pass --deterministic-only to reproduce this script's
exact pre-V3.5 behavior (the safe rollback lever if that worker is ever
disabled).

HONEST SCOPE, unchanged below this point: everything from here down
automates only what is genuinely deterministic -- FIT/GAP routing, the
zero-agent-eligible portion of the intelligence scan, dossier/asset build
for leads already advanced, email generation, QA, send-window planning, and
the READY_TO_SEND export. A lead still blocked on a research stage after
the acquisition worker's own budget/ceiling/timeout is counted and
reported, never guessed past.

This script NEVER calls send_executor.py, delivery_reconciliation.py,
follow_up.py, or reply_handling.py -- those stages start only after a real
Gmail send, which is explicitly ChatGPT's / the user's responsibility, not
this pipeline's. Lead Engine's automated output stops at READY_TO_SEND.

Usage:
  python3 scripts/run_daily.py [--dry-run] [--deterministic-only]

--dry-run runs every real read/compute step but writes the run summary to
data/runtime/daily_runs/DRY-RUN-<timestamp>.json instead of the dated
production path, so a validation run can never be mistaken for a real one.
--deterministic-only skips the V3.5 acquisition worker entirely (pre-V3.5
behavior).
"""
import argparse
import json
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from _lib import ROOT, PROSPECTS, MARKETS, LEADS, DATA, read_jsonl, load_json, write_json, now_iso
import acquisition_worker

SCHEDULE_TZ = ZoneInfo("Asia/Karachi")

RUNTIME_DIR = DATA / "runtime"
DAILY_RUNS_DIR = RUNTIME_DIR / "daily_runs"
LOG_DIR = RUNTIME_DIR / "logs"
SCRIPTS = ROOT / "scripts"

REQUIRED_FILES = [
    ROOT / "OPERATING-RULES.md", ROOT / "CLAUDE.md",
    ROOT / "config" / "scoring.yaml", ROOT / "config" / "limits.yaml",
    ROOT / "config" / "outreach.yaml", ROOT / "config" / "niches.yaml",
    ROOT / "schemas" / "prospect.schema.json",
]


def log(msg, logfile=None):
    line = f"[{now_iso()}] {msg}"
    print(line)
    if logfile:
        with open(logfile, "a") as f:
            f.write(line + "\n")


def verify_workspace():
    """Deterministic pre-flight check. Returns (ok, problems)."""
    problems = []
    for f in REQUIRED_FILES:
        if not f.exists():
            problems.append(f"missing required file: {f.relative_to(ROOT)}")
    try:
        import yaml  # noqa: F401
        from _lib import load_yaml
        load_yaml("scoring.yaml")
        load_yaml("limits.yaml")
        load_yaml("outreach.yaml")
    except Exception as e:
        problems.append(f"config failed to load: {e}")
    return (len(problems) == 0, problems)


def run_script(args, logfile, failures, prospect_id=None):
    """
    Runs one pipeline script as an isolated subprocess. A non-zero exit or
    exception is recorded as a per-lead failure and does NOT stop the run --
    per the explicit failure-isolation requirement, one lead's failure must
    never block the batch.
    """
    cmd = [sys.executable] + args
    try:
        result = subprocess.run(cmd, cwd=SCRIPTS, capture_output=True, text=True, timeout=120)
        log(f"  $ {' '.join(args)} -> exit {result.returncode}", logfile)
        if result.stdout.strip():
            log(f"    {result.stdout.strip().splitlines()[-1]}", logfile)
        if result.returncode != 0:
            failures.append({
                "prospect_id": prospect_id, "stage": args[0],
                "reason": (result.stderr.strip().splitlines()[-1] if result.stderr.strip() else f"exit code {result.returncode}"),
            })
            return False
        return True
    except Exception as e:
        log(f"  ! exception running {args}: {e}", logfile)
        failures.append({"prospect_id": prospect_id, "stage": args[0], "reason": str(e)})
        return False


def artifact_snapshot():
    """Cheap fingerprint of which key artifacts exist right now, per lead --
    used to compute what THIS run actually produced (a before/after diff),
    not a cumulative all-time count."""
    snap = {}
    if not LEADS.exists():
        return snap
    for ldir in LEADS.iterdir():
        if not ldir.is_dir():
            continue
        snap[ldir.name] = {
            "wedge": (ldir / "primary_wedge.json").exists(),
            "asset": (ldir / "staged_asset.json").exists(),
            "contact_verified": (load_json(ldir / "contact_record.json") or {}).get("overall_status") == "CONTACT_VERIFIED",
            "contact_form_ready": (load_json(ldir / "contact_record.json") or {}).get("overall_status") == "CONTACT_FORM_READY",
        }
    return snap


def status_counts(records):
    from collections import Counter
    return Counter(r.get("status") for r in records)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--deterministic-only", action="store_true",
                     help="V3.5: skip the Claude acquisition worker entirely -- reproduces this script's "
                          "exact pre-V3.5 behavior. The rollback lever if the acquisition worker is disabled.")
    ap.add_argument("--trigger-type", default="NORMAL_SCHEDULE",
                     help="Recorded in the run summary as-is. The normal systemd timer firing leaves this at "
                          "its default; scripts/run_claude_acquisition.sh sets it explicitly for a manual/"
                          "catch-up invocation of the acquisition worker directly (not through this script).")
    ap.add_argument("--max-prospects", type=int, default=None,
                     help="V3.5: cap how many leads the acquisition worker touches this run -- for a "
                          "controlled validation invocation, never used by the production timer.")
    args = ap.parse_args()

    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    DAILY_RUNS_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    run_id = f"run-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    started_at = now_iso()
    logfile = LOG_DIR / f"{run_id}.log"
    failures = []
    limitations = []

    def today_key():
        # Karachi-local date, matching the timer's own schedule timezone --
        # see scripts/catchup.py, which reads this same dated summary file
        # to decide same-day catch-up eligibility.
        return datetime.now(SCHEDULE_TZ).date().isoformat()

    log(f"=== Lead Engine daily run {run_id} (dry_run={args.dry_run}, deterministic_only={args.deterministic_only}, "
        f"trigger_type={args.trigger_type}) ===", logfile)

    # --- Step: workspace verification -----------------------------------
    ok, problems = verify_workspace()
    if not ok:
        for p in problems:
            log(f"FATAL: {p}", logfile)
        summary = {
            "run_id": run_id, "started_at": started_at, "completed_at": now_iso(),
            "trigger_type": args.trigger_type,
            "dry_run": args.dry_run, "infrastructure_failure": True, "problems": problems,
        }
        out = DAILY_RUNS_DIR / (f"DRY-RUN-{run_id}.json" if args.dry_run else f"{today_key()}.json")
        write_json(out, summary)
        log(f"Workspace verification FAILED -- exiting non-zero. Summary: {out}", logfile)
        return 2  # infrastructure-level failure -> non-zero exit, per the explicit requirement

    log("Workspace verified. OPERATING-RULES.md and CLAUDE.md present, config loads cleanly.", logfile)
    log("Permanent operating rules loaded (existence + parse check) before any pipeline stage ran.", logfile)

    before = artifact_snapshot()
    prospects_before = read_jsonl(PROSPECTS / "discovered.jsonl")

    # --- Step: V3.5 Claude acquisition worker (pending leads + fresh
    #     discovery) -- runs BEFORE the deterministic loop below, which is
    #     otherwise completely unchanged from pre-V3.5 behavior. See
    #     scripts/acquisition_worker.py for the full safety model.
    acquisition_stats = None
    if args.deterministic_only:
        limitations.append("--deterministic-only: V3.5 acquisition worker skipped by explicit flag.")
        limitations.append("Market/discovery workflow: skipped (deterministic-only mode). "
                            "New markets/businesses must be added via a separate acquisition-worker run.")
        unverified = [p for p in prospects_before if p.get("status") == "DISCOVERED"]
        if unverified:
            limitations.append(f"{len(unverified)} prospect(s) at DISCOVERED still need business-identity "
                                "verification research (verify_business.py --print-prompt) before this job can advance them.")
    else:
        log("Starting V3.5 Claude acquisition worker...", logfile)
        acquisition_stats = acquisition_worker.run(
            max_prospects=args.max_prospects, trigger_type=args.trigger_type,
            log=lambda msg: log(msg, logfile),
        )
        log(f"Claude acquisition worker finished: auth_status={acquisition_stats.get('claude_auth_status')}, "
            f"run_already_active={acquisition_stats.get('run_already_active')}, "
            f"acquisition_run_completed={acquisition_stats.get('acquisition_run_completed')}, "
            f"worker_timeout={acquisition_stats.get('worker_timeout')}", logfile)
        if acquisition_stats.get("claude_auth_status") == "CLAUDE_AUTH_REQUIRED":
            limitations.append("CLAUDE_AUTH_REQUIRED -- acquisition worker failed closed, no research performed this run.")
        if acquisition_stats.get("run_already_active"):
            limitations.append("RUN_ALREADY_ACTIVE -- another acquisition worker was already running; this run's "
                                "acquisition phase was skipped, deterministic finalization still proceeded.")
        limitations.extend(acquisition_stats.get("limitations", []))
        failures.extend(acquisition_stats.get("per_lead_failures", []))

    # --- Step: FIT/GAP + qualification routing (deterministic, bulk) ----
    run_script(["qualify_leads.py", "--v3"], logfile, failures)

    # --- Step: per-lead deterministic intelligence -> dossier -> asset --
    prospects = read_jsonl(PROSPECTS / "discovered.jsonl")
    needs_research_intel = 0
    for p in prospects:
        pid = p["id"]
        status = p.get("status")
        if status in ("HIGH_PRIORITY", "QUALIFIED", "FIT_SCORED", "GAP_SCORED"):
            run_script(["run_deterministic_scan.py", "--id", pid], logfile, failures, prospect_id=pid)
        p2 = next((r for r in read_jsonl(PROSPECTS / "discovered.jsonl") if r["id"] == pid), p)
        if p2.get("status") in ("AGENT_ROUTED", "SECOND_OPINION_REQUIRED"):
            needs_research_intel += 1
        if p2.get("status") == "OPPORTUNITY_IDENTIFIED":
            run_script(["build_dossier_v3_2.py", "--id", pid], logfile, failures, prospect_id=pid)
        p3 = next((r for r in read_jsonl(PROSPECTS / "discovered.jsonl") if r["id"] == pid), p)
        if p3.get("status") == "DOSSIER_READY":
            run_script(["stage_asset.py", "--id", pid], logfile, failures, prospect_id=pid)
    if needs_research_intel:
        if args.deterministic_only:
            limitations.append(f"{needs_research_intel} lead(s) need a specialist agent research session "
                                "(route_to_specialist.py) -- --deterministic-only mode never invokes a live agent call.")
        else:
            limitations.append(f"{needs_research_intel} lead(s) still need specialist escalation after the "
                                "acquisition worker's budget/ceiling/timeout -- carried over to the next run.")

    # --- Step: contact identity (honest no-op for NEW contact research) -
    prospects = read_jsonl(PROSPECTS / "discovered.jsonl")
    needs_contact_research = 0
    for p in prospects:
        pid = p["id"]
        if p.get("status") == "ASSET_STAGED" and not (LEADS / pid / "contact_record.json").exists():
            needs_contact_research += 1
    if needs_contact_research:
        limitations.append(f"{needs_contact_research} lead(s) at ASSET_STAGED still need a contact-identity research "
                            "pass (contact_identity.py) -- never auto-guessed, carried over to the next run "
                            "(deterministic-only mode never performs this research at all).")

    # --- Step: draft -> QA -> send window (deterministic, uses existing --
    #     contact_record.json only; never performs new research) ---------
    prospects = read_jsonl(PROSPECTS / "discovered.jsonl")
    for p in prospects:
        pid = p["id"]
        if p.get("status") in ("CONTACT_VERIFIED", "CONTACT_FORM_READY"):
            run_script(["generate_outreach_email.py", "--id", pid], logfile, failures, prospect_id=pid)
        p2 = next((r for r in read_jsonl(PROSPECTS / "discovered.jsonl") if r["id"] == pid), p)
        if p2.get("status") == "EMAIL_DRAFT_READY":
            run_script(["qa_outreach_email.py", "--id", pid], logfile, failures, prospect_id=pid)
        p3 = next((r for r in read_jsonl(PROSPECTS / "discovered.jsonl") if r["id"] == pid), p)
        if p3.get("status") == "READY_TO_SEND":
            run_script(["send_window_planner.py", "--id", pid], logfile, failures, prospect_id=pid)

    # --- Step: READY_TO_SEND export for ChatGPT/Gmail-side reconciliation
    export_ok = run_script(["export_ready_to_send.py"], logfile, failures)

    # --- Step: reporting exports (dated runtime artifacts, gitignored) ---
    run_script(["report_pipeline.py"], logfile, failures)
    run_script(["triage_report.py"], logfile, failures)

    # --- Final counts -----------------------------------------------------
    final_prospects = read_jsonl(PROSPECTS / "discovered.jsonl")
    counts = status_counts(final_prospects)
    after = artifact_snapshot()

    wedges_created = sum(1 for pid, a in after.items() if a["wedge"] and not before.get(pid, {}).get("wedge"))
    assets_staged_now = sum(1 for pid, a in after.items() if a["asset"] and not before.get(pid, {}).get("asset"))
    contacts_verified_now = sum(1 for pid, a in after.items() if a["contact_verified"] and not before.get(pid, {}).get("contact_verified"))
    contact_form_ready_now = sum(1 for pid, a in after.items() if a["contact_form_ready"] and not before.get(pid, {}).get("contact_form_ready"))

    ready_to_send_path = DATA / "outreach" / "ready_to_send.jsonl"
    ready_to_send_total = len(read_jsonl(ready_to_send_path))

    summary = {
        "run_id": run_id,
        "trigger_type": args.trigger_type,
        "started_at": started_at,
        "completed_at": now_iso(),
        "dry_run": args.dry_run,
        "infrastructure_failure": False,
        "markets_processed": len(list(MARKETS.iterdir())) if MARKETS.exists() else 0,
        "prospects_discovered": len(final_prospects),
        "businesses_verified": sum(1 for p in final_prospects if p.get("status") not in ("DISCOVERED",)),
        "qualified": counts.get("QUALIFIED", 0),
        "high_priority": counts.get("HIGH_PRIORITY", 0),
        "needs_enrichment": counts.get("NEEDS_ENRICHMENT", 0),
        "rejected": counts.get("REJECTED", 0),
        "wedges_created": wedges_created,
        "assets_staged": assets_staged_now,
        "contacts_verified": contacts_verified_now,
        "contact_form_ready": contact_form_ready_now,
        "qa_pass": sum(1 for p in final_prospects if p.get("status") in ("READY_TO_SEND", "SEND_WINDOW_PLANNED")),
        "ready_to_send": ready_to_send_total,
        "failures": failures,
        "limitations": limitations,
        # --- V3.5 fields (see OPERATING-RULES.md Sec.4 / docs/AUTOMATION.md) ---
        "claude_auth_status": None,
        "claude_worker_started": None,
        "claude_worker_completed": None,
        "acquisition_run_completed": False,
        "pending_leads_processed": 0,
        "fresh_candidates_discovered": 0,
        "fit_scored": 0,
        "gap_scored": 0,
        "buying_signals_verified": 0,
        "deterministic_wedges": 0,
        "one_agent_escalations": 0,
        "two_agent_escalations": 0,
        "no_defensible_wedge": 0,
        "per_lead_failures": [],
        "worker_timeout": False,
        "run_already_active": False,
    }
    if acquisition_stats:
        # Exclude keys this block above already computed AFTER the full
        # pipeline (including qualify_leads.py --v3, which the acquisition
        # worker itself runs before) -- acquisition_stats' own values for
        # these are a stale snapshot from before routing happened and would
        # silently clobber the correct final counts otherwise (e.g. a lead
        # ROUTED to REJECTED by qualify_leads never shows up in
        # acquisition_stats["rejected"], since that tally ran first).
        SUPERSEDED_BY_FINAL_TALLY = {"limitations", "qualified", "high_priority", "rejected", "needs_enrichment"}
        summary.update({k: v for k, v in acquisition_stats.items() if k not in SUPERSEDED_BY_FINAL_TALLY})

    out_name = f"DRY-RUN-{run_id}.json" if args.dry_run else f"{today_key()}.json"
    out_path = DAILY_RUNS_DIR / out_name
    write_json(out_path, summary)
    log(f"Run summary written to {out_path}", logfile)
    log(f"=== Lead Engine daily run {run_id} complete ===", logfile)

    return 0  # per-lead failures/limitations never cause a non-zero exit -- only infrastructure failure does


if __name__ == "__main__":
    sys.exit(main())
