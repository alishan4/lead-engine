#!/usr/bin/env python3
"""
V3.8.1 -- Discovery-Only Production Mode. The DEFAULT scheduled worker
(config/acquisition.yaml: production_mode: discovery_only). Implements the
new permanent architecture:

    FEDORA / LEAD ENGINE: discover -> cheap/deterministic verification ->
        save candidates -> sync CANDIDATES to Google Sheets -> STOP.
    CHATGPT + USER: qualification, Google/Maps opportunity research,
        competitor research, SEO wedge, contact research, personalized
        outreach, Gmail execution, reply handling / sales.

Operating principle: CLAUDE DISCOVERS, CLAUDE DOES NOT ANALYZE. The ONLY
Claude call anywhere in this module is scripts/discover_prospects.py's
existing, unchanged per-market-cell discovery call. Verification
(scripts/candidate_verification.py) is 100% deterministic -- zero
additional Claude spend per candidate. This module never imports
assess_commercial_fit, assess_google_gap, rank_enrichment,
reevaluate_needs_enrichment, route_to_specialist, contact_identity,
build_dossier*, stage_asset, generate_outreach_email/generate_email,
qa_outreach_email/qa_email, send_window_planner, export_ready_to_send,
send_executor, delivery_reconciliation, follow_up, or reply_handling --
see tests/test_v3_8_1_discovery_only.py's static guard.

Cost governors (config/discovery_only.yaml), checked BEFORE every Claude
call, never only after -- see scripts/cost_ledger.py for the durable,
cross-invocation daily $ ledger that makes "one shared daily budget" real
across the scheduled run, a same-day catch-up run, and any manual
invocation:
  1. daily_claude_budget_usd  -- a hard $ ceiling, shared for the WHOLE day
  2. max_claude_calls_per_run -- an independent circuit breaker, enforced
     even when $ cost is not observable in this environment
  3. max_worker_runtime_seconds -- a much smaller wall-clock ceiling than
     full_pipeline's 2700s, since this mode never proceeds past cheap
     verification
  4. max_market_cells_per_run -- bounds research SCOPE independently of
     the call cap

Candidate count (min/max_candidates_target) is a GOAL, never a quota --
every governor above always wins over reaching the target. If only 3
defensible candidates are found within budget, this module saves 3 and
stops; it never manufactures a candidate to hit a number.

Usage:
  python3 scripts/discovery_worker.py
  python3 scripts/discovery_worker.py --trigger-type SAME_DAY_CATCH_UP
"""
import argparse
import fcntl
import json
import subprocess
import sys
import time
from pathlib import Path

from _lib import ROOT, DATA, PROSPECTS, LEADS, load_yaml, read_jsonl, write_json, set_status_everywhere, now_iso, slugify
from claude_invoke import run_claude_with_meta, ClaudeAuthRequired, ClaudeTimeout, ClaudeInvocationError
import claude_preflight
import cost_ledger
from candidate_verification import verify_candidate_basic, CANDIDATE_VERIFIED, CANDIDATE_REJECTED
import acquisition_worker as aw  # reuses ONLY the pure market-rotation helpers below -- never aw.run()/process_lead/
                                   # discovery_phase/any Claude-driven stage function. See the static import guard test.

SCRIPTS = ROOT / "scripts"
SCHEMAS = ROOT / "schemas"
LOCK_PATH = DATA / "runtime" / "discovery.lock"
DISCOVERY_TIMEOUT_KEY = "research"


def load_schema(name):
    return json.loads((SCHEMAS / name).read_text())


class Deadline:
    def __init__(self, seconds):
        self.expires_at = time.monotonic() + seconds

    def exceeded(self):
        return time.monotonic() >= self.expires_at

    def remaining_seconds(self):
        """V3.8.2 -- never negative; 0.0 once the deadline has passed."""
        return max(0.0, self.expires_at - time.monotonic())


def empty_stats():
    return {
        "candidates_discovered": 0, "candidates_verified": 0, "candidates_saved": 0,
        "duplicates_skipped": 0, "verification_failures": 0, "market_cells_explored": 0,
        "markets_explored": [], "failures": [],
        # V3.8.2 -- attempt-based call accounting (see CostGuard). A call
        # counts the instant it is spawned, whether it succeeds, fails,
        # times out, returns malformed output, or hits its own per-call
        # budget -- never refunded.
        "claude_calls_attempted": 0, "claude_calls_succeeded": 0, "claude_calls_failed": 0,
        "observed_successful_call_cost_usd": 0.0, "observed_failed_call_cost_usd": 0.0,
        "observed_total_cost_usd": 0.0, "unknown_cost_attempts": 0,
        "budget_accounting_status": "COMPLETE",
        "input_tokens": None, "output_tokens": None, "total_tokens": None,
        # Backward-compatible aliases (reports/consumers written against
        # V3.8.1's shape): actual_cost_usd mirrors observed_total_cost_usd.
        "actual_cost_usd": 0.0, "estimated_cost_usd": None,
        "claude_calls": 0,
        "budget_limit_usd": None, "budget_remaining_usd": None, "cost_observable": True,
        "budget_status": "OK",
        "cost_per_discovered_candidate": None, "cost_per_verified_candidate": None,
        "cost_per_saved_candidate": None,
    }


def call_print(args, timeout=30):
    """Identical contract to acquisition_worker.py's own call_print --
    duplicated rather than imported so this module never depends on any
    acquisition_worker.py function that touches Claude/a research stage,
    keeping the static import guard simple and honest."""
    proc = subprocess.run([sys.executable] + args, cwd=SCRIPTS, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(
            proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else f"{args[0]} exited {proc.returncode}"
        )
    return proc.stdout


def call_save(args, result_obj, timeout=30):
    proc = subprocess.run(
        [sys.executable] + args + ["--save", "-"], cwd=SCRIPTS,
        input=json.dumps(result_obj), capture_output=True, text=True, timeout=timeout,
    )
    return proc


def get_prospect(pid, discovered=None):
    discovered = discovered if discovered is not None else read_jsonl(PROSPECTS / "discovered.jsonl")
    return next((r for r in discovered if r["id"] == pid), None)


def _expected_id(niche, city, state, business_name):
    return slugify(niche, city, state, business_name)


def match_raw_candidate(raw_candidates, niche, city, state, pid):
    """Pure: finds the raw discovery-candidate dict (with `phone`, etc. --
    fields the shared prospect record does not carry forward) corresponding
    to an already-persisted prospect id, by recomputing the same
    deterministic slug discover_prospects.py: to_prospect_record() used.
    Returns None if genuinely not found (defensive -- never raises)."""
    for c in raw_candidates:
        if _expected_id(niche, city, state, c.get("business_name")) == pid:
            return c
    return None


def parse_save_output(stdout):
    """Pure: discover_prospects.py --save's stdout -> (new_ids, duplicate_count).
    '  + <id>' lines are newly-added prospects; '  - <name>: already present
    in the pipeline...' lines are dedupe drops -- every other drop reason
    (non-independent, weak commercial value, no Google-dependency evidence)
    is NOT a duplicate, so only the specific dedupe-reason string is
    counted here."""
    new_ids = [line.strip()[2:] for line in stdout.splitlines() if line.startswith("  + ")]
    dup_count = sum(1 for line in stdout.splitlines()
                    if line.startswith("  - ") and "already present in the pipeline" in line)
    return new_ids, dup_count


class CostGuard:
    """
    V3.8.2 -- attempt-based cost governor, in priority order (budget/call-
    cap/time ALWAYS win over the candidate-count goal). Wraps
    scripts/cost_ledger.py so budget is shared across separate process
    invocations (scheduled run + same-day catch-up + manual retry) via one
    durable, date-keyed file -- never a fresh allowance per invocation.

    Fixes the two real defects the 2026-09-03 live validation exposed:
      1. reserve_attempt() reserves a call-budget slot and writes a PENDING
         ledger entry BEFORE the real subprocess is spawned -- a crash,
         kill, or timeout that never reaches record_attempt_result() still
         counts against calls_attempted and permanently marks that
         attempt's cost unknown, never silently forgotten.
      2. record_attempt_result() is called on EVERY outcome (success or
         failure) with whatever real cost/usage `meta` the failed call's
         own output exposed (see claude_invoke.py's ClaudeCallError.meta)
         -- a failed-but-billable call's real spend now reaches the daily
         ledger exactly like a successful one's.
    """

    def __init__(self, cfg, date_key=None):
        self.cfg = cfg
        self.date_key = date_key or cost_ledger.today_key()
        self.calls_attempted = 0
        self.calls_succeeded = 0
        self.calls_failed = 0
        self.observed_successful_cost = 0.0
        self.observed_failed_cost = 0.0
        self.unknown_cost_attempts = 0
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.tokens_observable_this_run = True

    @property
    def cost_accounting_complete(self):
        """True only if EVERY attempt this run had an observable cost --
        false the instant even one attempt's cost is unknown (a failure
        with no recoverable cost, or a PENDING/crashed attempt)."""
        return self.unknown_cost_attempts == 0

    def check_before_call(self):
        """Returns (ok: bool, reason: str_or_None). Checked before every
        single Claude call attempt -- never only at the end of a batch.
        Uses calls_attempted (not calls_succeeded) so a failed attempt
        still counts toward the cap -- never refunded."""
        if self.calls_attempted >= self.cfg["max_claude_calls_per_run"]:
            return False, "CALL_CAP_REACHED"
        remaining, spent, observable = cost_ledger.remaining_budget(self.cfg["daily_claude_budget_usd"], self.date_key)
        if observable and remaining is not None and remaining <= 0:
            return False, "EXHAUSTED"
        return True, None

    def reserve_attempt(self, run_id=None, label=None):
        """MUST be called immediately before spawning the real `claude -p`
        subprocess -- increments calls_attempted (never refunded, no
        matter what happens next) and durably reserves this attempt in
        today's ledger before any real work starts."""
        self.calls_attempted += 1
        return cost_ledger.start_attempt(run_id=run_id, label=label, date_key=self.date_key)

    def record_attempt_result(self, attempt_id, success, meta=None, error_category=None):
        """Enriches a reserved attempt with its real outcome. `meta` is a
        claude_invoke cost/usage dict -- present (with cost_observable
        possibly True) even for a FAILED call whose own error payload
        exposed a real cost (see ClaudeCallError.meta). `error_category`
        is one of CALL_TIMEOUT / WORKER_DEADLINE_TIMEOUT /
        INVOCATION_ERROR / None (success)."""
        meta = meta or {}
        cost_ledger.finish_attempt(
            attempt_id, date_key=self.date_key, success=success,
            cost_usd=meta.get("total_cost_usd"), cost_observable=bool(meta.get("cost_observable")),
            input_tokens=meta.get("input_tokens"), output_tokens=meta.get("output_tokens"),
            tokens_observable=bool(meta.get("tokens_observable")),
            duration_ms=meta.get("duration_ms"), error_category=error_category,
        )
        if success:
            self.calls_succeeded += 1
        else:
            self.calls_failed += 1

        if meta.get("cost_observable"):
            cost = meta["total_cost_usd"]
            if success:
                self.observed_successful_cost = round(self.observed_successful_cost + cost, 6)
            else:
                self.observed_failed_cost = round(self.observed_failed_cost + cost, 6)
        else:
            self.unknown_cost_attempts += 1

        if meta.get("tokens_observable") and self.tokens_observable_this_run:
            self.total_input_tokens += meta["input_tokens"]
            self.total_output_tokens += meta["output_tokens"]
        else:
            self.tokens_observable_this_run = False

    def summary(self, counters):
        remaining, spent, day_observable = cost_ledger.remaining_budget(self.cfg["daily_claude_budget_usd"], self.date_key)
        observed_total = round(self.observed_successful_cost + self.observed_failed_cost, 6)
        accounting_complete = self.cost_accounting_complete
        budget_accounting_status = "COMPLETE" if accounting_complete else "INCOMPLETE_UNKNOWN_CALL_COST"

        out = {
            "claude_calls_attempted": self.calls_attempted,
            "claude_calls_succeeded": self.calls_succeeded,
            "claude_calls_failed": self.calls_failed,
            "observed_successful_call_cost_usd": self.observed_successful_cost,
            "observed_failed_call_cost_usd": self.observed_failed_cost,
            # V3.8.2: total OBSERVED cost -- the sum of every KNOWN cost
            # (successful AND failed calls). Never suppressed to None just
            # because some OTHER attempt's cost is unknown: a known partial
            # sum is more honest than reporting nothing, as long as it is
            # clearly labeled incomplete via budget_accounting_status
            # below -- never presented as if it were total true spend.
            "observed_total_cost_usd": observed_total,
            "unknown_cost_attempts": self.unknown_cost_attempts,
            "budget_accounting_status": budget_accounting_status,
            # Backward-compatible aliases (V3.8.1 shape / report consumers).
            "actual_cost_usd": observed_total,
            "estimated_cost_usd": None,  # never invented, regardless of accounting completeness
            "cost_observable": accounting_complete,
            "claude_calls": self.calls_attempted,
            "budget_limit_usd": self.cfg["daily_claude_budget_usd"],
        }
        # V3.8.2 Sec.3: a known billable attempt with unknown cost creates
        # real uncertainty in the DAILY ledger -- never report an exact
        # remaining-dollar figure as authoritative in that case. This is
        # cost_ledger.remaining_budget()'s own day-level observability
        # (which already accounts for every PRIOR invocation today, not
        # just this run), so it can be False even when this run's own
        # accounting is otherwise COMPLETE.
        out["budget_remaining_usd"] = remaining if day_observable else None

        if self.tokens_observable_this_run:
            out["input_tokens"] = self.total_input_tokens
            out["output_tokens"] = self.total_output_tokens
            out["total_tokens"] = self.total_input_tokens + self.total_output_tokens
        else:
            out["input_tokens"] = None
            out["output_tokens"] = None
            out["total_tokens"] = None

        def _per(n):
            # cost_per_verified_candidate etc. use observed_total_cost_usd
            # (successful + failed known costs) -- a failed call's real
            # spend is part of the true cost of running this cycle, not
            # something to quietly exclude from the denominator's numerator.
            if not n:
                return None
            return round(observed_total / n, 6)

        out["cost_per_discovered_candidate"] = _per(counters["candidates_discovered"])
        out["cost_per_verified_candidate"] = _per(counters["candidates_verified"])
        out["cost_per_saved_candidate"] = _per(counters["candidates_saved"])
        return out


def build_cell_context(acq_cfg, max_cells):
    """Tiny shim exposing just the `.cfg` attribute
    acquisition_worker.pick_discovery_cells()/niche_tier() need -- reuses
    that function's tested, unmodified tier-weighted market rotation
    without importing or calling anything Claude-driven from that module."""
    class _Ctx:
        pass
    ctx = _Ctx()
    ctx.cfg = {
        "discovery_markets": acq_cfg["discovery_markets"],
        "max_fresh_market_cells_per_run": max_cells,
    }
    return ctx


def pick_cells(acq_cfg, max_cells, day_ordinal):
    ctx = build_cell_context(acq_cfg, max_cells)
    return aw.pick_discovery_cells(ctx, day_ordinal)


def acquire_lock():
    """Non-blocking flock on data/runtime/discovery.lock -- a SEPARATE
    lock from acquisition_worker.py's acquisition.lock, since discovery-
    only mode and full_pipeline mode are independent workers that must
    never be conflated (discovery-only never calls acquisition_worker.run()
    and vice versa)."""
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


# V3.8.2 -- attempt_claude_discovery()'s outcome vocabulary. Each is a
# distinct, honestly-labeled reason the market-cell loop uses to decide
# whether to continue to the next cell or stop the whole run.
OUTCOME_SUCCESS = "success"
OUTCOME_CALL_CAP_REACHED = "CALL_CAP_REACHED"
OUTCOME_EXHAUSTED = "EXHAUSTED"
OUTCOME_RUNTIME_INSUFFICIENT = "RUNTIME_INSUFFICIENT"
OUTCOME_WORKER_DEADLINE_TIMEOUT = "WORKER_DEADLINE_TIMEOUT"
OUTCOME_FAILED = "failed"

# Governor outcomes that mean "stop the ENTIRE run now" (never just skip to
# the next market cell) -- every one of these represents either a hard
# budget/call/time ceiling, or the worker deadline having been reached
# mid-call, per V3.8.2 Sec.6: "No replacement/retry call."
STOP_RUN_OUTCOMES = {OUTCOME_CALL_CAP_REACHED, OUTCOME_EXHAUSTED, OUTCOME_RUNTIME_INSUFFICIENT, OUTCOME_WORKER_DEADLINE_TIMEOUT}


def attempt_claude_discovery(cost, deadline, cfg, acq_cfg, prompt, trigger_type, market_cell, max_retries, log):
    """
    V3.8.2 -- attempts ONE discover_prospects.py Claude call for one market
    cell, with bounded retry, under three governors checked before EVERY
    real subprocess spawn (including each retry -- a retry is a brand-new
    `claude -p` process and consumes its own attempt slot, never free):
      1. call cap / daily $ budget (cost.check_before_call())
      2. remaining worker runtime vs. min_seconds_to_start_claude_call
    The subprocess's own timeout is ALWAYS clamped to the lesser of its
    configured per-call timeout and the worker's remaining runtime, so an
    in-flight call can never itself blow past the worker deadline -- when
    it's the runtime, not the call, that ultimately kills the subprocess,
    Python's own subprocess timeout mechanism handles the "terminate it"
    part, and the resulting ClaudeTimeout is classified
    WORKER_DEADLINE_TIMEOUT (never retried, callers must stop the run) vs.
    a plain CALL_TIMEOUT (isolated to this market cell, retried up to
    max_retries times, exactly like V3.8.1).

    Returns (outcome, result_or_None, meta_or_None) -- outcome is one of
    the OUTCOME_* constants above.
    """
    retry_count = 0
    while True:
        ok, reason = cost.check_before_call()
        if not ok:
            return reason, None, None  # "CALL_CAP_REACHED" / "EXHAUSTED"

        remaining = deadline.remaining_seconds()
        if remaining < cfg["min_seconds_to_start_claude_call"]:
            log(f"  ~ only {remaining:.1f}s of worker runtime remain (below "
                f"min_seconds_to_start_claude_call={cfg['min_seconds_to_start_claude_call']}s) "
                f"-- not starting a new Claude call.")
            return OUTCOME_RUNTIME_INSUFFICIENT, None, None

        effective_timeout = min(acq_cfg["max_claude_call_seconds_research"], remaining)
        attempt_id = cost.reserve_attempt(run_id=trigger_type, label=market_cell)
        try:
            result, meta = run_claude_with_meta(
                prompt, json_schema=load_schema("discovery_candidate.schema.json"),
                timeout_s=effective_timeout, max_budget_usd=cfg["max_budget_usd_per_call"],
            )
            cost.record_attempt_result(attempt_id, success=True, meta=meta)
            return OUTCOME_SUCCESS, result, meta
        except ClaudeTimeout as e:
            deadline_hit = deadline.exceeded()
            error_category = "WORKER_DEADLINE_TIMEOUT" if deadline_hit else "CALL_TIMEOUT"
            cost.record_attempt_result(attempt_id, success=False, meta=e.meta, error_category=error_category)
            if deadline_hit:
                log(f"  ! {market_cell}: Claude call hit the worker deadline mid-flight -- terminated, "
                    f"no replacement/retry call. {e}")
                return OUTCOME_WORKER_DEADLINE_TIMEOUT, None, None
            if retry_count >= max_retries:
                log(f"  ! {market_cell}: Claude call timed out, retries exhausted -- {e}")
                return OUTCOME_FAILED, None, None
            retry_count += 1
            log(f"  ~ claude -p timed out -- retrying (attempt {retry_count + 1}/{max_retries + 1})")
            continue
        except ClaudeInvocationError as e:
            cost.record_attempt_result(attempt_id, success=False, meta=e.meta, error_category="INVOCATION_ERROR")
            log(f"  ! {market_cell}: Claude call failed -- {e}")
            return OUTCOME_FAILED, None, None
        # ClaudeAuthRequired is deliberately NOT caught here -- mid-run auth
        # loss must stop the WHOLE run, not be treated as one market cell's
        # failure (same rule acquisition_worker.py's claude_research()
        # documents for full_pipeline mode). The attempt this call already
        # reserved stays PENDING in today's ledger, which is exactly the
        # crash-safety behavior V3.8.2 Sec.4 requires -- an interrupted run
        # still shows an honest, unknown-cost attempt rather than losing it.


def write_candidate_facts(pid, discovery_source, phone):
    """The one small per-lead artifact this module writes beyond the
    shared prospect record -- carries `phone`/`discovery_source` forward
    for scripts/sync_handoff.py: build_candidate_rows() to read at sync
    time (see schemas/candidate_record.schema.json's note on why phone
    isn't on the shared prospect record itself)."""
    write_json(LEADS / pid / "candidate_facts.json", {
        "discovery_source": discovery_source, "phone": phone, "written_at": now_iso(),
    })


def run(trigger_type="NORMAL_SCHEDULE", log=print):
    """
    The full V3.8.1 discovery-only cycle. Returns a stats dict (see
    empty_stats() plus claude_auth_status/run_already_active/
    claude_worker_started/claude_worker_completed/discovery_run_completed/
    worker_timeout). Never raises for a per-candidate/per-market-cell
    problem -- only lock contention or an auth failure short-circuits
    before any state is touched, exactly like acquisition_worker.py's own
    fail-closed model.
    """
    stats = {
        "production_mode": "discovery_only", "trigger_type": trigger_type,
        "claude_worker_started": now_iso(), "claude_auth_status": None,
        "run_already_active": False, "claude_worker_completed": None,
        "discovery_run_completed": False, "worker_timeout": False,
    }

    lock_fh = acquire_lock()
    if lock_fh is None:
        log("RUN_ALREADY_ACTIVE -- another discovery worker holds the lock, exiting without touching state.")
        stats["run_already_active"] = True
        stats["claude_worker_completed"] = now_iso()
        stats.update(empty_stats())
        return stats

    try:
        # V3.8.1 Sec.12 -- auth failure fails closed, exactly once, no
        # retry loop, no repeated spawn, no acquisition cycle triggered.
        ok, status, detail = claude_preflight.check()
        stats["claude_auth_status"] = status
        if not ok:
            log(f"{status}: {detail} -- failing closed, no discovery performed.")
            stats["claude_worker_completed"] = now_iso()
            stats.update(empty_stats())
            return stats
        log("Claude auth preflight: AUTH_OK.")

        cfg = load_yaml("discovery_only.yaml")
        acq_cfg = load_yaml("acquisition.yaml")
        deadline = Deadline(cfg["max_worker_runtime_seconds"])
        cost = CostGuard(cfg)
        counters = {k: 0 for k in ("candidates_discovered", "candidates_verified", "candidates_saved",
                                    "duplicates_skipped", "verification_failures", "market_cells_explored")}
        markets_explored, failures = [], []
        saved_ids = []

        day_ordinal = int(now_iso()[:10].replace("-", ""))
        cells = pick_cells(acq_cfg, cfg["max_market_cells_per_run"], day_ordinal)

        reliability = cfg.get("reliability", {})
        max_retries = reliability.get("max_timeout_retries", 0) if reliability.get("retry_on_timeout") else 0

        budget_status = "OK"
        for niche, city, state in cells:
            if deadline.exceeded():
                stats["worker_timeout"] = True
                log("Worker runtime ceiling reached -- completed candidate work preserved, stopping gracefully.")
                break
            if len(saved_ids) >= cfg["max_candidates_target"]:
                log(f"Candidate target ({cfg['max_candidates_target']}) reached -- goal met, stopping "
                    f"(never a reason to exceed a budget/call/time governor, but also never exceeded once met).")
                break

            market_cell = f"{niche} / {city}, {state}"
            try:
                prompt = call_print(["discover_prospects.py", "--niche", niche, "--city", city, "--state", state, "--print-prompt"])
            except (RuntimeError, subprocess.TimeoutExpired) as e:
                # No Claude subprocess was ever spawned for this cell -- not
                # a billable attempt, nothing to reserve/record in the ledger.
                failures.append({"market_cell": market_cell, "reason": str(e)[:400]})
                log(f"  ! {market_cell}: print-prompt failed -- {e}")
                continue

            outcome, result, meta = attempt_claude_discovery(
                cost, deadline, cfg, acq_cfg, prompt, trigger_type, market_cell, max_retries, log,
            )

            if outcome in STOP_RUN_OUTCOMES:
                if outcome == OUTCOME_WORKER_DEADLINE_TIMEOUT:
                    failures.append({"market_cell": market_cell, "reason": "WORKER_DEADLINE_TIMEOUT"})
                    stats["worker_timeout"] = True
                elif outcome == OUTCOME_RUNTIME_INSUFFICIENT:
                    stats["worker_timeout"] = True
                else:
                    budget_status = outcome
                log(f"{outcome} -- no further Claude calls this run. Preserving completed candidate work, "
                    f"will sync and write the report, then stop. (Never retried, never another market cell.)")
                break

            if outcome == OUTCOME_FAILED:
                failures.append({"market_cell": market_cell, "reason": "Claude discovery call failed -- see log"})
                continue  # one market cell's failure never blocks the next

            counters["market_cells_explored"] += 1
            markets_explored.append(market_cell)
            try:
                proc = call_save(["discover_prospects.py", "--niche", niche, "--city", city, "--state", state], result)
            except subprocess.TimeoutExpired as e:
                # The Claude call already succeeded (and was already billed/
                # recorded above) -- only the deterministic save step hung.
                # Isolated to this cell, never blocks the next one.
                failures.append({"market_cell": market_cell, "reason": f"discover_prospects.py --save timed out: {e}"[:400]})
                log(f"  ! {market_cell}: --save timed out -- {e}")
                continue

            new_ids, dup_count = parse_save_output(proc.stdout)
            counters["candidates_discovered"] += len(new_ids)
            counters["duplicates_skipped"] += dup_count
            raw_candidates = result.get("candidates", [])

            for pid in new_ids:
                if len(saved_ids) >= cfg["max_candidates_target"]:
                    break
                try:
                    prospect = get_prospect(pid)
                    if not prospect:
                        continue
                    verified, vreason = verify_candidate_basic(prospect)
                    if not verified:
                        counters["verification_failures"] += 1
                        failures.append({"prospect_id": pid, "reason": vreason})
                        set_status_everywhere(pid, CANDIDATE_REJECTED, extra_fields={"reject_reason": vreason})
                        log(f"  ! {pid}: CANDIDATE_REJECTED -- {vreason}")
                        continue
                    counters["candidates_verified"] += 1
                    raw = match_raw_candidate(raw_candidates, niche, city, state, pid)
                    write_candidate_facts(pid, discovery_source=market_cell, phone=(raw or {}).get("phone"))
                    set_status_everywhere(pid, CANDIDATE_VERIFIED)
                    counters["candidates_saved"] += 1
                    saved_ids.append(pid)
                    log(f"  + {pid}: CANDIDATE_VERIFIED")
                except Exception as e:
                    # One candidate's failure -- record and continue, never an
                    # expensive retry cascade (V3.8.1 Sec.5).
                    counters["verification_failures"] += 1
                    failures.append({"prospect_id": pid, "reason": str(e)[:400]})
                    log(f"  ! {pid}: verification failed -- {e}")
                    continue

        stats.update(counters)
        stats["markets_explored"] = markets_explored
        stats["failures"] = failures
        stats.update(cost.summary(counters))
        stats["budget_status"] = budget_status
        stats["discovery_run_completed"] = True
        stats["claude_worker_completed"] = now_iso()
        log(f"Discovery-only cycle complete. {json.dumps(counters)}")
        return stats
    finally:
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trigger-type", default="NORMAL_SCHEDULE")
    args = ap.parse_args()
    stats = run(trigger_type=args.trigger_type)
    print(json.dumps(stats, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
