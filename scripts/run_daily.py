#!/usr/bin/env python3
"""
Lead Engine daily orchestration (Tue-Fri automated run).

HONEST SCOPE: this script automates every stage of the pipeline that is
genuinely deterministic -- scoring, FIT/GAP routing, the zero-agent
intelligence scan, dossier/asset build, email generation, QA, send-window
planning, and the READY_TO_SEND export. It deliberately does NOT and
CANNOT automate stages that require real web research or human/Claude
judgment: new-market discovery, first-time business verification, buying-
signal evidence collection, franchise-status research, or contact-identity
verification. Those stages have always used a --print-prompt / --save
pattern precisely because they need a real research pass -- a cron job has
no research capability, and this script never fabricates one. Leads
blocked on a research stage are counted and reported, never guessed past.

This script NEVER calls send_executor.py, delivery_reconciliation.py,
follow_up.py, or reply_handling.py -- those stages start only after a real
Gmail send, which is explicitly ChatGPT's / the user's responsibility, not
this pipeline's. Lead Engine's automated output stops at READY_TO_SEND.

Usage:
  python3 scripts/run_daily.py [--dry-run]

--dry-run runs every real read/compute step but writes the run summary to
data/runtime/daily_runs/DRY-RUN-<timestamp>.json instead of the dated
production path, so a validation run can never be mistaken for a real one.
"""
import argparse
import json
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from _lib import ROOT, PROSPECTS, MARKETS, LEADS, DATA, read_jsonl, load_json, write_json, now_iso

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
    args = ap.parse_args()

    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    DAILY_RUNS_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    run_id = f"run-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    started_at = now_iso()
    logfile = LOG_DIR / f"{run_id}.log"
    failures = []
    limitations = []

    log(f"=== Lead Engine daily run {run_id} (dry_run={args.dry_run}) ===", logfile)

    # --- Step: workspace verification -----------------------------------
    ok, problems = verify_workspace()
    if not ok:
        for p in problems:
            log(f"FATAL: {p}", logfile)
        summary = {
            "run_id": run_id, "started_at": started_at, "completed_at": now_iso(),
            "dry_run": args.dry_run, "infrastructure_failure": True, "problems": problems,
        }
        out = DAILY_RUNS_DIR / (f"DRY-RUN-{run_id}.json" if args.dry_run else f"{datetime.now(timezone.utc).date().isoformat()}.json")
        write_json(out, summary)
        log(f"Workspace verification FAILED -- exiting non-zero. Summary: {out}", logfile)
        return 2  # infrastructure-level failure -> non-zero exit, per the explicit requirement

    log("Workspace verified. OPERATING-RULES.md and CLAUDE.md present, config loads cleanly.", logfile)
    log("Permanent operating rules loaded (existence + parse check) before any pipeline stage ran.", logfile)

    before = artifact_snapshot()
    prospects_before = read_jsonl(PROSPECTS / "discovered.jsonl")

    # --- Step: discovery (honest no-op) ----------------------------------
    limitations.append("Market/discovery workflow: no deterministic discovery exists in this codebase. "
                        "New markets/businesses must be added via a human/Claude research session, not this cron job.")

    # --- Step: business verification (honest no-op for NEW businesses) ---
    unverified = [p for p in prospects_before if p.get("status") == "DISCOVERED"]
    if unverified:
        limitations.append(f"{len(unverified)} prospect(s) at DISCOVERED still need business-identity "
                            "verification research (verify_business.py --print-prompt) before this job can advance them.")

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
        limitations.append(f"{needs_research_intel} lead(s) need a specialist agent research session "
                            "(route_to_specialist.py) -- unattended automation never invokes a live agent call.")

    # --- Step: contact identity (honest no-op for NEW contact research) -
    prospects = read_jsonl(PROSPECTS / "discovered.jsonl")
    needs_contact_research = 0
    for p in prospects:
        pid = p["id"]
        if p.get("status") == "ASSET_STAGED" and not (LEADS / pid / "contact_record.json").exists():
            needs_contact_research += 1
    if needs_contact_research:
        limitations.append(f"{needs_contact_research} lead(s) at ASSET_STAGED need a contact-identity research "
                            "pass (contact_identity.py --print-prompt) -- never auto-guessed.")

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
    }

    out_name = f"DRY-RUN-{run_id}.json" if args.dry_run else f"{datetime.now(timezone.utc).date().isoformat()}.json"
    out_path = DAILY_RUNS_DIR / out_name
    write_json(out_path, summary)
    log(f"Run summary written to {out_path}", logfile)
    log(f"=== Lead Engine daily run {run_id} complete ===", logfile)

    return 0  # per-lead failures/limitations never cause a non-zero exit -- only infrastructure failure does


if __name__ == "__main__":
    sys.exit(main())
