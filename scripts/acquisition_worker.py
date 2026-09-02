#!/usr/bin/env python3
"""
V3.5 -- the unattended Claude acquisition worker. Sits IN FRONT OF
run_daily.py's existing, unchanged deterministic loop: this module's job
ends once a lead reaches CONTACT_VERIFIED / CONTACT_FORM_READY (or a valid
stop status short of that) -- draft / QA / send-window / export are left to
the deterministic code that already exists and is not touched here.

Every research-stage script it drives (verify_business.py,
assess_buying_signals.py, check_contactability.py, check_franchise.py,
route_to_specialist.py, contact_identity.py, discover_prospects.py) is
used EXACTLY as it already exists via its --print-prompt/--print-context +
--save contract -- none of them are modified. This module only replaces
"a human pastes into an interactive Claude session" with a real
claude_invoke.run_claude() call under the safety-restricted profile
documented there.

Two hard invariants, enforced by construction (see also claude_invoke.py):
  1. Never import/call send_executor, delivery_reconciliation, follow_up,
     or reply_handling -- everything downstream of a real Gmail send is out
     of scope for this repository, full stop.
  2. Every Claude subprocess this module launches runs with the same
     Read/WebSearch/WebFetch-only, --restricted profile -- including
     specialist escalation (see `ask_specialist`), which deliberately does
     NOT shell out to the interactive claude-seo Skill packages (those
     require Bash/Write, which this module never grants to any Claude
     subprocess, under any circumstance).

Usage:
  python3 scripts/acquisition_worker.py                     # normal run
  python3 scripts/acquisition_worker.py --max-prospects 2    # capped validation run
  python3 scripts/acquisition_worker.py --trigger-type SAME_DAY_CATCH_UP
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from _lib import (
    ROOT, PROSPECTS, DATA, read_jsonl, load_yaml, load_json, now_iso,
)
from claude_invoke import run_claude, ClaudeAuthRequired, ClaudeTimeout, ClaudeInvocationError
import claude_preflight

SCRIPTS = ROOT / "scripts"
SCHEMAS = ROOT / "schemas"
LOCK_PATH = DATA / "runtime" / "acquisition.lock"

STAGE_A_STATUSES = {
    "DISCOVERED", "BUSINESS_VERIFIED", "COMMERCIAL_FIT_ASSESSED",
    "BUYING_SIGNALS_ASSESSED", "CONTACTABILITY_CHECK", "GOOGLE_GAP_ASSESSED",
}
STAGE_C_STATUSES = {
    "QUALIFIED", "HIGH_PRIORITY", "AGENT_ROUTED", "SECOND_OPINION_REQUIRED",
    "OPPORTUNITY_IDENTIFIED", "DOSSIER_READY", "ASSET_STAGED",
}
MAX_STEPS_PER_LEAD = 15  # defensive cap -- the real state machine never needs this many


class Deadline:
    def __init__(self, seconds):
        self.expires_at = time.monotonic() + seconds

    def exceeded(self):
        return time.monotonic() >= self.expires_at


class Counters(dict):
    FIELDS = (
        "pending_leads_processed", "fresh_candidates_discovered", "market_cells_explored",
        "businesses_verified", "fit_scored", "gap_scored", "buying_signals_verified",
        "needs_enrichment", "rejected", "qualified", "high_priority",
        "deterministic_wedges", "one_agent_escalations", "two_agent_escalations",
        "assets_staged", "contacts_verified", "contact_form_ready", "no_defensible_wedge",
        "franchise_stops",
    )

    def __init__(self):
        super().__init__({f: 0 for f in self.FIELDS})


class WorkerContext:
    def __init__(self, cfg, limits_cfg, scoring_cfg, deadline, log, max_prospects, sandbox):
        self.cfg = cfg
        self.limits_cfg = limits_cfg
        self.scoring_cfg = scoring_cfg
        self.deadline = deadline
        self.log = log
        self.max_prospects = max_prospects
        self.sandbox = sandbox
        self.counters = Counters()
        self.failures = []
        self.leads_touched = set()

    def lead_budget_available(self):
        return self.max_prospects is None or len(self.leads_touched) < self.max_prospects


def load_schema(name):
    return json.loads((SCHEMAS / name).read_text())


def get_prospect(pid, discovered=None):
    discovered = discovered if discovered is not None else read_jsonl(PROSPECTS / "discovered.jsonl")
    return next((r for r in discovered if r["id"] == pid), None)


def call_print(args, timeout=30):
    """Runs a --print-prompt/--print-context call and returns its stdout.
    Raises RuntimeError on a non-zero exit -- e.g. route_to_specialist.py's
    hard 2-call cap, or a prospect no longer found -- so the caller's
    per-lead try/except records it as a failure instead of silently handing
    an empty/garbage prompt to Claude."""
    proc = subprocess.run([sys.executable] + args, cwd=SCRIPTS, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(
            (proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else f"{args[0]} exited {proc.returncode}")
        )
    return proc.stdout


def call_save(args, result_obj, timeout=30):
    proc = subprocess.run(
        [sys.executable] + args + ["--save", "-"], cwd=SCRIPTS,
        input=json.dumps(result_obj), capture_output=True, text=True, timeout=timeout,
    )
    return proc


def call_plain(args, timeout=60):
    return subprocess.run([sys.executable] + args, cwd=SCRIPTS, capture_output=True, text=True, timeout=timeout)


def record_failure(ctx, pid, stage, reason):
    ctx.failures.append({"prospect_id": pid, "stage": stage, "reason": str(reason)[:400]})
    ctx.log(f"  ! {pid}: {stage} failed -- {reason}")


def claude_research(ctx, prompt, schema_name_or_dict, timeout_key="research"):
    """Wraps claude_invoke.run_claude with the config-driven timeout/budget.
    ClaudeAuthRequired always propagates (fail-closed, mid-run auth loss
    must stop the run, not be treated as one lead's failure).

    V3.7: a real ClaudeTimeout is retried exactly once (config/acquisition.yaml:
    reliability.max_timeout_retries, default 1) before propagating -- found
    in production on 2026-09-02 that a research-heavy WebSearch call landing
    right at the cap is common enough to be worth one bounded retry, not a
    sign of a permanently broken call. Never retried for any other error
    type (ClaudeInvocationError/malformed response won't be fixed by
    retrying blindly), and still bounded -- after the retry is exhausted,
    the caller's existing per-lead/per-cell try/except records the failure
    exactly as before; the batch is never blocked."""
    schema = schema_name_or_dict if isinstance(schema_name_or_dict, dict) else load_schema(schema_name_or_dict)
    timeout_s = ctx.cfg[f"max_claude_call_seconds_{timeout_key}"]
    budget = ctx.cfg["max_budget_usd_per_call"]
    reliability = ctx.cfg.get("reliability", {})
    max_retries = reliability.get("max_timeout_retries", 0) if reliability.get("retry_on_timeout") else 0

    attempt = 0
    while True:
        try:
            return run_claude(prompt, json_schema=schema, timeout_s=timeout_s, max_budget_usd=budget)
        except ClaudeTimeout:
            if attempt >= max_retries:
                raise
            attempt += 1
            ctx.log(f"  ~ claude -p timed out at {timeout_s}s -- retrying (attempt {attempt + 1}/{max_retries + 1})")


# --------------------------------------------------------------------------
# Individual Claude-driven stages -- each: print-prompt -> claude -> --save
# --------------------------------------------------------------------------

def verify_business_stage(ctx, pid):
    prompt = call_print(["verify_business.py", "--id", pid, "--print-prompt"])
    result = claude_research(ctx, prompt, "verification.schema.json", "research")
    proc = call_save(["verify_business.py", "--id", pid], result)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else "verify_business.py --save failed")
    ctx.counters["businesses_verified"] += 1


FRANCHISE_SCHEMA = {
    "type": "object",
    "required": ["prospect_id", "possible_franchise", "corporate_marketing_controlled", "lead_gen_network", "evidence"],
    "properties": {
        "prospect_id": {"type": "string"},
        "possible_franchise": {"type": "boolean"},
        "corporate_marketing_controlled": {"type": ["boolean", "null"]},
        "lead_gen_network": {"type": ["boolean", "null"]},
        "evidence": {"type": "array", "items": {"type": "string"}},
    },
}


def franchise_escalate_stage(ctx, pid):
    prompt = call_print(["check_franchise.py", "--id", pid, "--print-prompt"])
    result = claude_research(ctx, prompt, FRANCHISE_SCHEMA, "short")
    proc = call_save(["check_franchise.py", "--id", pid], result)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else "check_franchise.py --save failed")
    if "LEAD_GEN_NETWORK" in proc.stdout or "CORPORATE_MARKETING_LOCK" in proc.stdout:
        ctx.counters["franchise_stops"] += 1


BUYING_SIGNALS_SCHEMA = {
    "type": "object",
    "required": ["prospect_id", "evidence"],
    "properties": {
        "prospect_id": {"type": "string"},
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["signal_type", "value", "confidence", "source", "source_type", "observed_at", "evidence", "entity_match_confidence"],
                "properties": {
                    "signal_type": {"type": "string"},
                    "value": {"type": ["boolean", "string", "null"]},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "source": {"type": ["string", "null"]},
                    "source_type": {"type": "string"},
                    "observed_at": {"type": "string"},
                    "published_at": {"type": ["string", "null"]},
                    "evidence": {"type": "string"},
                    "entity_match_confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "notes": {"type": ["string", "null"]},
                },
            },
        },
    },
}


def buying_signals_stage(ctx, pid):
    prompt = call_print(["assess_buying_signals.py", "--id", pid, "--print-prompt"])
    result = claude_research(ctx, prompt, BUYING_SIGNALS_SCHEMA, "research")
    proc = call_save(["assess_buying_signals.py", "--id", pid], result)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else "assess_buying_signals.py --save failed")
    ctx.counters["buying_signals_verified"] += 1


CONTACTABILITY_SCHEMA = {
    "type": "object",
    "required": ["prospect_id", "contactability_score", "named_owner_found", "named_marketing_contact_found",
                 "named_ops_contact_found", "official_email_visible", "official_contact_form_available",
                 "likely_contact_role", "evidence"],
    "properties": {
        "prospect_id": {"type": "string"},
        "contactability_score": {"type": "integer", "enum": [0, 1, 2]},
        "named_owner_found": {"type": "boolean"},
        "named_marketing_contact_found": {"type": "boolean"},
        "named_ops_contact_found": {"type": "boolean"},
        "official_email_visible": {"type": "boolean"},
        "official_contact_form_available": {"type": "boolean"},
        "likely_contact_role": {"type": ["string", "null"]},
        "evidence": {"type": "array", "items": {"type": "string"}},
    },
}


def contactability_stage(ctx, pid):
    prompt = call_print(["check_contactability.py", "--id", pid, "--print-prompt"])
    result = claude_research(ctx, prompt, CONTACTABILITY_SCHEMA, "short")
    proc = call_save(["check_contactability.py", "--id", pid], result)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else "check_contactability.py --save failed")


CONTACT_IDENTITY_SCHEMA = {
    "type": "object",
    "required": ["person_name", "role", "email", "sources", "rejected_evidence", "has_contact_form", "contact_form_url", "mailbox_hint"],
    "properties": {
        "person_name": {"type": ["string", "null"]},
        "role": {"type": ["string", "null"]},
        "email": {"type": ["string", "null"]},
        "sources": {"type": "array", "items": {"type": "object"}},
        "rejected_evidence": {"type": "array", "items": {"type": "object"}},
        "has_contact_form": {"type": "boolean"},
        "contact_form_url": {"type": ["string", "null"]},
        "mailbox_hint": {"type": ["string", "null"], "enum": ["VALID", "RISKY", "INVALID", None]},
    },
}


def contact_identity_stage(ctx, pid):
    prompt = call_print(["contact_identity.py", "--id", pid, "--print-prompt"])
    result = claude_research(ctx, prompt, CONTACT_IDENTITY_SCHEMA, "research")
    proc = call_save(["contact_identity.py", "--id", pid], result)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else "contact_identity.py --save failed")
    if "CONTACT_VERIFIED" in proc.stdout:
        ctx.counters["contacts_verified"] += 1
    elif "CONTACT_FORM_READY" in proc.stdout:
        ctx.counters["contact_form_ready"] += 1


def ask_specialist(ctx, pid):
    """
    Specialist escalation, capped at 1-2 calls per lead by
    route_to_specialist.py's own hard-coded logic (unchanged). Deliberately
    does NOT invoke the interactive claude-seo Skill package for the named
    agent (e.g. claude-seo:seo-local) -- those require Bash/Write, which
    this worker never grants to any Claude subprocess under any
    circumstance (see module docstring). Instead it reuses the exact same
    routing/capping decision (config/opportunity_router.yaml via
    route_to_specialist.py) and the same specialist framing
    (prompts/opportunity-specialist.md) to answer the identical narrow,
    capped question through the same restricted Read/WebSearch/WebFetch-only
    profile every other stage uses. This is a deliberate fidelity trade-off,
    documented in reports/V3.5-UNATTENDED-ACQUISITION-REPORT.md: a
    specialist finding that would have required Bash-based tooling
    correctly surfaces as low confidence rather than silently degrading
    into a fabricated result.
    """
    raw = call_print(["route_to_specialist.py", "--id", pid, "--print-context"])
    if raw.startswith("## ROUTE TO:"):
        agent_line, rest = raw.split("\n", 1)
        agent_name = agent_line.replace("## ROUTE TO:", "").strip()
    else:
        agent_name, rest = "claude-seo:seo-content", raw
    framed_prompt = (
        f"Adopt the perspective of the `{agent_name}` SEO specialist. You have "
        "Read/WebSearch/WebFetch only (no local command execution) -- answer "
        "using those tools.\n\n" + rest
    )
    result = claude_research(ctx, framed_prompt, "specialist_output.schema.json", "research")
    proc = call_save(["route_to_specialist.py", "--id", pid], result)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else "route_to_specialist.py --save failed")
    if "OPPORTUNITY_IDENTIFIED" in proc.stdout:
        if "(1 agent" in proc.stdout:
            ctx.counters["one_agent_escalations"] += 1
        elif "(2 agent" in proc.stdout:
            ctx.counters["two_agent_escalations"] += 1


# --------------------------------------------------------------------------
# Stage A: DISCOVERED -> ... -> FIT_SCORED (or a valid stop short of that)
# --------------------------------------------------------------------------

def advance_stage_a(ctx, pid):
    for _ in range(MAX_STEPS_PER_LEAD):
        if ctx.deadline.exceeded():
            return
        p = get_prospect(pid)
        if not p:
            return
        status = p.get("status")
        try:
            if status == "DISCOVERED":
                verify_business_stage(ctx, pid)
            elif status == "BUSINESS_VERIFIED":
                call_plain(["check_franchise.py", "--id", pid])
                p2 = get_prospect(pid)
                if p2.get("status") != "BUSINESS_VERIFIED":
                    continue  # franchise stop status (LEAD_GEN_NETWORK / CORPORATE_MARKETING_LOCK / FRANCHISE_REVIEW_REQUIRED)
                franchise_resolved = p2.get("possible_franchise") is False or p2.get("corporate_marketing_controlled") is not None
                if not franchise_resolved:
                    franchise_escalate_stage(ctx, pid)
                    continue
                call_plain(["assess_commercial_fit.py", "--id", pid])
            elif status == "COMMERCIAL_FIT_ASSESSED":
                buying_signals_stage(ctx, pid)
            elif status == "BUYING_SIGNALS_ASSESSED":
                contactability_stage(ctx, pid)
            elif status == "CONTACTABILITY_CHECK":
                call_plain(["assess_google_gap.py", "--id", pid])
                ctx.counters["gap_scored"] += 1
            elif status == "GOOGLE_GAP_ASSESSED":
                call_plain(["assess_commercial_fit.py", "--id", pid])
                ctx.counters["fit_scored"] += 1
            else:
                return  # stopped: FIT_SCORED (ready for bulk qualify), REJECTED, MANUAL_REVIEW,
                         # CONTACTABILITY_FAILED, or a franchise/other stop status
        except (ClaudeTimeout, ClaudeInvocationError, RuntimeError, subprocess.TimeoutExpired) as e:
            record_failure(ctx, pid, f"stage_a:{status}", e)
            return
    record_failure(ctx, pid, "stage_a", f"exceeded {MAX_STEPS_PER_LEAD} steps without reaching a stop status")


# --------------------------------------------------------------------------
# Stage C: QUALIFIED/HIGH_PRIORITY -> ... -> CONTACT_VERIFIED/CONTACT_FORM_READY
# --------------------------------------------------------------------------

def advance_stage_c(ctx, pid):
    for _ in range(MAX_STEPS_PER_LEAD):
        if ctx.deadline.exceeded():
            return
        p = get_prospect(pid)
        if not p:
            return
        status = p.get("status")
        try:
            if status in ("QUALIFIED", "HIGH_PRIORITY"):
                call_plain(["run_deterministic_scan.py", "--id", pid], timeout=90)
            elif status in ("AGENT_ROUTED", "SECOND_OPINION_REQUIRED"):
                ask_specialist(ctx, pid)
            elif status == "OPPORTUNITY_IDENTIFIED":
                call_plain(["build_dossier_v3_2.py", "--id", pid])
                ctx.counters["deterministic_wedges"] += 1
            elif status == "DOSSIER_READY":
                call_plain(["stage_asset.py", "--id", pid])
                ctx.counters["assets_staged"] += 1
            elif status == "ASSET_STAGED":
                contact_identity_stage(ctx, pid)
            else:
                return  # CONTACT_VERIFIED/CONTACT_FORM_READY/CONTACT_UNVERIFIED/CONTACT_REVERIFY_REQUIRED/
                         # NO_DEFENSIBLE_WEDGE/INTELLIGENCE_FAILED/SUPPRESSED/ACCOUNT_LOCKED, or unrelated status
        except (ClaudeTimeout, ClaudeInvocationError, RuntimeError, subprocess.TimeoutExpired) as e:
            record_failure(ctx, pid, f"stage_c:{status}", e)
            return
    record_failure(ctx, pid, "stage_c", f"exceeded {MAX_STEPS_PER_LEAD} steps without reaching a stop status")


def process_lead(ctx, pid):
    if not ctx.lead_budget_available():
        return
    ctx.leads_touched.add(pid)
    ctx.counters["pending_leads_processed"] += 1
    advance_stage_a(ctx, pid)
    p = get_prospect(pid)
    if p and p.get("status") == "NO_DEFENSIBLE_WEDGE":
        ctx.counters["no_defensible_wedge"] += 1


def process_stage_c_only(ctx, pid):
    if not ctx.lead_budget_available():
        return
    ctx.leads_touched.add(pid)
    advance_stage_c(ctx, pid)
    p = get_prospect(pid)
    if p and p.get("status") == "NO_DEFENSIBLE_WEDGE":
        ctx.counters["no_defensible_wedge"] += 1


def qualified_count():
    counts = {"QUALIFIED": 0, "HIGH_PRIORITY": 0}
    for p in read_jsonl(PROSPECTS / "qualified.jsonl"):
        if p.get("status") in counts:
            counts[p["status"]] += 1
    return counts["QUALIFIED"] + counts["HIGH_PRIORITY"]


def outreach_capacity_remaining(ctx):
    return max(0, ctx.cfg["outreach_worthy_ceiling"] - qualified_count())


# --------------------------------------------------------------------------
# Fresh discovery
# --------------------------------------------------------------------------

# V3.7: niche tier (config/niches.yaml -- the existing V3.1 FIT
# niche_economics axis, not a new concept) sets how many "slots" a niche
# gets in one pass of the interleaved rotation below. Empirically motivated:
# 2026-09-02's two production passes found niche tier, not review count or
# years-in-business, was the dominant correlate of low FIT (see
# reports/V3.7-ACQUISITION-QUALITY-REPORT.md Sec.A) -- every tier-2 niche
# candidate landed at FIT 28-44 regardless of review count/rating/years,
# while the single tier-1 candidate scored highest (56). Every tier still
# gets at least 1 slot -- a niche is never fully excluded from the rotation.
TIER_ROTATION_WEIGHT = {1: 3, 2: 2, 3: 1}


def niche_tier(niche, niches_cfg):
    return (niches_cfg.get("niches") or {}).get(niche, {}).get("tier", 3)


def niche_rotation_weight(niche, niches_cfg):
    return TIER_ROTATION_WEIGHT.get(niche_tier(niche, niches_cfg), 1)


def niche_rotation_weight_for_tier(tier):
    return TIER_ROTATION_WEIGHT.get(tier, 1)


def build_market_rotation(niches, cities, niches_cfg):
    """
    Pure: returns every (niche, city, state) cell exactly once, city-major
    order (all niches for city[0], then all niches for city[1], ...) so
    that consecutive entries cycle through DIFFERENT niches -- never a long
    contiguous run of the same niche, which is what let 2026-09-02's two
    production passes explore almost nothing but family_law (the old
    niche-major loop put an entire niche's 12 cities in one contiguous
    block). Full coverage is preserved -- every cell still appears exactly
    once; niche_rotation_weight is applied separately, as a priority SORT
    over whichever cells a given run actually selects (see
    pick_discovery_cells), not by duplicating cells here -- there is no
    "explore a cell twice" concept in this one-shot-per-cell architecture
    (a cell is permanently skipped once data/markets/<slug>/ exists), so
    weighting has to act on selection order, not on representation count.
    """
    return [(n, c["city"], c["state"]) for c in cities for n in niches]


def weighted_tier_sequence(weights, length):
    """
    Classic smooth-weighted-round-robin: returns `length` tier ids,
    interleaved proportionally to `weights` (e.g. {1: 3, 2: 2} over 5 slots
    gives a 3:2 mix, not "all of tier 1 first, then tier 2"). Deterministic,
    no randomness -- same inputs always produce the same sequence.
    """
    counters = {t: 0.0 for t in weights}
    total = sum(weights.values())
    seq = []
    for _ in range(length):
        for t in counters:
            counters[t] += weights[t]
        pick = max(counters, key=lambda t: (counters[t], weights[t]))
        counters[pick] -= total
        seq.append(pick)
    return seq


def pick_discovery_cells(ctx, day_ordinal):
    dm = ctx.cfg["discovery_markets"]
    niches, cities = dm["niches"], dm["cities"]
    niches_cfg = load_yaml("niches.yaml")
    existing_markets = {d.name for d in (DATA / "markets").iterdir()} if (DATA / "markets").exists() else set()
    from _lib import market_slug

    all_cells = build_market_rotation(niches, cities, niches_cfg)
    n = len(all_cells)
    ordered = [all_cells[(day_ordinal + i) % n] for i in range(n)]
    fresh = [cell for cell in ordered if market_slug(*cell) not in existing_markets]
    pool = fresh or ordered
    total_slots = ctx.cfg["max_fresh_market_cells_per_run"]

    # V3.7: allocate this run's limited cell budget across tiers by a
    # smooth WEIGHTED mix (e.g. roughly 3:2 tier-1:tier-2 for the default
    # weights), not an absolute "always tier 1 first" priority -- the
    # latter would let tier-1 niches exhaust the ENTIRE multi-city pool
    # before a tier-2 niche (family_law, plumbing, estate_law,
    # moving_relocation) is ever explored again, which is functionally a
    # niche exclusion even though no single candidate was ever globally
    # rejected. Cells stay in rotation order within their own tier, so
    # day-to-day variety is preserved.
    by_tier = {}
    for cell in pool:
        by_tier.setdefault(niche_tier(cell[0], niches_cfg), []).append(cell)
    weights = {t: niche_rotation_weight_for_tier(t) for t in by_tier}

    tier_iters = {t: iter(cells) for t, cells in by_tier.items()}
    chosen, chosen_set = [], set()
    for t in weighted_tier_sequence(weights, total_slots * 4):  # oversample the sequence to allow skipping exhausted tiers
        if len(chosen) >= total_slots:
            break
        cell = next(tier_iters[t], None)
        if cell is not None and cell not in chosen_set:
            chosen.append(cell)
            chosen_set.add(cell)

    if len(chosen) < total_slots:  # every tier exhausted before filling the budget -- backfill from whatever remains
        leftover = sorted((c for c in pool if c not in chosen_set), key=lambda c: niche_tier(c[0], niches_cfg))
        chosen += leftover[: total_slots - len(chosen)]
    return chosen[:total_slots]


DISCOVERY_TIMEOUT_KEY = "research"


def discovery_phase(ctx):
    if outreach_capacity_remaining(ctx) <= 0:
        ctx.log("  discovery skipped -- outreach_worthy_ceiling already reached today")
        return
    day_ordinal = int(now_iso()[:10].replace("-", ""))
    cells = pick_discovery_cells(ctx, day_ordinal)
    researched_this_run = 0
    for niche, city, state in cells:
        if ctx.deadline.exceeded():
            return
        if outreach_capacity_remaining(ctx) <= 0:
            return
        if researched_this_run >= ctx.cfg["max_fresh_candidates_researched_per_run"]:
            return
        market_cell = f"{niche} / {city}, {state}"
        try:
            prompt = call_print(["discover_prospects.py", "--niche", niche, "--city", city, "--state", state, "--print-prompt"])
            result = claude_research(ctx, prompt, "discovery_candidate.schema.json", DISCOVERY_TIMEOUT_KEY)
            proc = call_save(["discover_prospects.py", "--niche", niche, "--city", city, "--state", state], result)
            ctx.counters["market_cells_explored"] += 1
            if proc.returncode != 0:
                record_failure(ctx, None, f"discovery:{market_cell}", proc.stderr.strip()[-200:] or "discover_prospects.py --save failed")
                continue
        except (ClaudeTimeout, ClaudeInvocationError, RuntimeError, subprocess.TimeoutExpired) as e:
            record_failure(ctx, None, f"discovery:{market_cell}", e)
            continue

        new_ids = [line.strip()[2:] for line in proc.stdout.splitlines() if line.startswith("  + ")]
        ctx.counters["fresh_candidates_discovered"] += len(new_ids)
        for pid in new_ids:
            if ctx.deadline.exceeded() or not ctx.lead_budget_available():
                return
            if researched_this_run >= ctx.cfg["max_fresh_candidates_researched_per_run"]:
                return
            if outreach_capacity_remaining(ctx) <= 0:
                return
            researched_this_run += 1
            process_lead(ctx, pid)
            p = get_prospect(pid)
            if p and p.get("status") in ("QUALIFIED", "HIGH_PRIORITY"):
                process_stage_c_only(ctx, pid)


# --------------------------------------------------------------------------
# Orchestration entry point
# --------------------------------------------------------------------------

def acquire_lock():
    """Non-blocking flock on data/runtime/acquisition.lock. Returns an open
    file handle (keep it alive for the run's duration) or None if another
    acquisition worker already holds it."""
    import fcntl
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    fh = open(LOCK_PATH, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        return None
    fh.write(f"{now_iso()} pid={__import__('os').getpid()}\n")
    fh.flush()
    return fh


def run(max_prospects=None, trigger_type="NORMAL_SCHEDULE", sandbox=False, log=print):
    """
    The full V3.5 acquisition cycle. Returns a stats dict (see
    Counters.FIELDS plus claude_auth_status / run_already_active /
    worker_timeout / claude_worker_started / claude_worker_completed /
    acquisition_run_completed) suitable for merging into run_daily.py's
    daily summary. Never raises for a per-lead problem -- only an
    infrastructure-level issue (lock contention, auth failure) short-
    circuits before any state is touched.
    """
    stats = {
        "trigger_type": trigger_type, "claude_worker_started": now_iso(),
        "claude_auth_status": None, "run_already_active": False,
        "claude_worker_completed": None, "acquisition_run_completed": False,
        "worker_timeout": False, "sandbox": sandbox,
    }

    lock_fh = acquire_lock()
    if lock_fh is None:
        log("RUN_ALREADY_ACTIVE -- another acquisition worker holds the lock, exiting without touching state.")
        stats["run_already_active"] = True
        stats["claude_worker_completed"] = now_iso()
        return stats

    try:
        ok, status, detail = claude_preflight.check()
        stats["claude_auth_status"] = status
        if not ok:
            log(f"{status}: {detail} -- failing closed, no acquisition work performed.")
            stats["claude_worker_completed"] = now_iso()
            return stats
        log("Claude auth preflight: AUTH_OK.")

        cfg = load_yaml("acquisition.yaml")
        limits_cfg = load_yaml("limits.yaml")
        scoring_cfg = load_yaml("scoring.yaml")
        deadline = Deadline(cfg["max_worker_runtime_seconds"])
        ctx = WorkerContext(cfg, limits_cfg, scoring_cfg, deadline, log, max_prospects, sandbox)

        discovered = read_jsonl(PROSPECTS / "discovered.jsonl")
        stage_a_ids = [p["id"] for p in discovered if p.get("status") in STAGE_A_STATUSES]
        stage_c_ids = [p["id"] for p in discovered if p.get("status") in STAGE_C_STATUSES]

        log(f"Pending work found: {len(stage_a_ids)} lead(s) need FIT-track research, "
            f"{len(stage_c_ids)} lead(s) need intelligence/contact-identity work.")

        for pid in stage_a_ids:
            if deadline.exceeded() or not ctx.lead_budget_available():
                break
            process_lead(ctx, pid)

        if not deadline.exceeded():
            call_plain(["qualify_leads.py", "--v3"], timeout=60)

        discovered = read_jsonl(PROSPECTS / "discovered.jsonl")
        newly_qualified = [p["id"] for p in discovered if p.get("status") in ("QUALIFIED", "HIGH_PRIORITY")]
        for pid in set(stage_c_ids) | set(newly_qualified):
            if deadline.exceeded() or not ctx.lead_budget_available():
                break
            process_stage_c_only(ctx, pid)

        if not deadline.exceeded() and ctx.lead_budget_available():
            log("Pending-lead work complete. Checking fresh-discovery capacity...")
            discovery_phase(ctx)

        final = read_jsonl(PROSPECTS / "discovered.jsonl")
        for p in final:
            s = p.get("status")
            if s == "REJECTED":
                ctx.counters["rejected"] += 1
            elif s == "NEEDS_ENRICHMENT":
                ctx.counters["needs_enrichment"] += 1
            elif s == "QUALIFIED":
                ctx.counters["qualified"] += 1
            elif s == "HIGH_PRIORITY":
                ctx.counters["high_priority"] += 1

        stats.update(dict(ctx.counters))
        stats["per_lead_failures"] = ctx.failures
        stats["worker_timeout"] = deadline.exceeded()
        stats["limitations"] = []
        if ctx.counters["needs_enrichment"]:
            stats["limitations"].append(
                f"{ctx.counters['needs_enrichment']} lead(s) at NEEDS_ENRICHMENT are missing maps_position/"
                "organic_position (ranking data). This worker deliberately does not attempt to fill rankings via "
                "Claude research -- reliable rank-tracking needs a real SERP-tracking data source or careful "
                "manual verification (scripts/import_rankings.py), and 'never fabricate rankings' is a hard rule. "
                "These leads are counted and carried over, never guessed past."
            )
        if stats["worker_timeout"]:
            stats["limitations"].append(
                f"worker hit its {cfg['max_worker_runtime_seconds']}s runtime budget -- "
                "completed work is preserved, no in-flight lead was marked done that wasn't."
            )
        stats["acquisition_run_completed"] = True
        stats["claude_worker_completed"] = now_iso()
        log(f"Acquisition cycle complete. {json.dumps(dict(ctx.counters))}")
        return stats
    finally:
        import fcntl
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-prospects", type=int, default=None)
    ap.add_argument("--trigger-type", default="NORMAL_SCHEDULE")
    ap.add_argument("--sandbox", action="store_true", help="informational only here -- set LEAD_ENGINE_DATA_DIR before invoking for an actual sandboxed data dir")
    args = ap.parse_args()
    stats = run(max_prospects=args.max_prospects, trigger_type=args.trigger_type, sandbox=args.sandbox)
    print(json.dumps(stats, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
