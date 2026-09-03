#!/usr/bin/env python3
"""
V3.8.1/V3.8.2 -- the daily Claude cost/usage ledger. Makes "one shared
daily budget" real ACROSS SEPARATE PROCESS INVOCATIONS: the normal
scheduled run, a same-day catch-up run, and any manual/validation
invocation all read and write the SAME date-keyed file, so a 12:00 run
that spends $2.40 leaves only $0.60 for a later catch-up that same day --
never a fresh $daily budget per invocation. See
config/discovery_only.yaml: daily_claude_budget_usd.

Dated by the SAME civil day the scheduled timer and catch-up window already
use (Asia/Karachi -- see scripts/catchup.py), not UTC or the machine's local
time, so "today's budget" means the same thing everywhere in this pipeline.

File: data/runtime/cost/<YYYY-MM-DD>.json -- gitignored (data/runtime/ is
already a full-directory .gitignore entry; nothing new needed there).

V3.8.2 -- ATTEMPT-BASED, crash-safe accounting. The 2026-09-03 live
validation exposed two real defects this module now closes:
  1. A failed Claude call can still be billable (observed: a call that hit
     its own --max-budget-usd circuit breaker mid-research still cost a
     real $0.5358346) -- the old record_usage() was only ever invoked
     AFTER a successful call, so that real spend never reached the ledger.
  2. A crash/kill/interruption between "we decided to spawn Claude" and
     "the call returned" left NO record at all of the attempt -- a call
     slot could be silently lost from accounting.
start_attempt()/finish_attempt() replace record_usage(): start_attempt()
writes a PENDING entry IMMEDIATELY, before the real subprocess is even
spawned, so total_calls and the day's cost_observable flag already reflect
that attempt even if the process is killed before finish_attempt() is ever
called. finish_attempt() enriches that same entry with its real outcome
(status/cost/tokens/duration/error_category) once known.

Never fabricates cost. If a `claude -p` call's real envelope didn't report
`total_cost_usd` (see claude_invoke.py: _extract_meta), or an attempt was
left PENDING (crash/kill before completion), this ledger records that
attempt's cost as unobservable and the daily total's `cost_observable`
flag goes false for the rest of that day -- the $ budget can then no
longer be honestly enforced, and callers must fall back to the call-count/
time governors instead of guessing a dollar figure.
"""
import fcntl
import json
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from _lib import DATA, now_iso

SCHEDULE_TZ = ZoneInfo("Asia/Karachi")
COST_DIR = DATA / "runtime" / "cost"


def today_key(tz=SCHEDULE_TZ):
    """The same civil-day convention run_daily.py's today_key()/
    scripts/catchup.py already use -- Asia/Karachi, not UTC/machine-local."""
    return datetime.now(tz).date().isoformat()


def ledger_path(date_key=None):
    return COST_DIR / f"{(date_key or today_key())}.json"


def _empty_ledger(date_key):
    return {
        "date": date_key,
        "total_cost_usd": 0.0,
        "cost_observable": True,  # flips false the moment any attempt's cost is unobservable
        "total_calls": 0,  # V3.8.2: every ATTEMPT (spawned or reserved), not just successes
        "total_calls_succeeded": 0,
        "total_calls_failed": 0,
        "unknown_cost_attempts": 0,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "tokens_observable": True,
        "entries": [],
    }


def load_ledger(date_key=None):
    path = ledger_path(date_key)
    if not path.exists():
        return _empty_ledger(date_key or today_key())
    with open(path) as f:
        return json.load(f)


def _write_locked(f, ledger):
    f.seek(0)
    f.truncate()
    f.write(json.dumps(ledger, indent=2, ensure_ascii=False) + "\n")
    f.flush()


def _read_locked(f, date_key):
    f.seek(0)
    raw = f.read()
    return json.loads(raw) if raw.strip() else _empty_ledger(date_key)


def _recompute_aggregates(ledger):
    """Pure (mutates `ledger` in place): recomputes every aggregate field
    from the full `entries` list rather than incrementally drifting --
    finish_attempt() can be called out of order or (defensively) more than
    once for the same attempt_id without ever corrupting the totals. A
    PENDING entry (never finished -- a crash/kill left it that way) counts
    toward total_calls and toward unknown_cost_attempts, but not toward
    either succeeded/failed."""
    entries = ledger.get("entries", [])
    total_cost = 0.0
    total_in = total_out = 0
    succeeded = failed = unknown = 0
    cost_observable_day = True
    tokens_observable_day = True
    for e in entries:
        status = e.get("status")
        if status == "SUCCESS":
            succeeded += 1
        elif status == "FAILED":
            failed += 1
        if e.get("cost_observable"):
            total_cost += e.get("cost_usd") or 0.0
        else:
            unknown += 1
            cost_observable_day = False
        if e.get("tokens_observable"):
            total_in += e.get("input_tokens") or 0
            total_out += e.get("output_tokens") or 0
        else:
            tokens_observable_day = False

    ledger["total_cost_usd"] = round(total_cost, 6)
    ledger["cost_observable"] = cost_observable_day
    ledger["total_calls"] = len(entries)
    ledger["total_calls_succeeded"] = succeeded
    ledger["total_calls_failed"] = failed
    ledger["unknown_cost_attempts"] = unknown
    ledger["total_input_tokens"] = total_in
    ledger["total_output_tokens"] = total_out
    ledger["tokens_observable"] = tokens_observable_day


def start_attempt(run_id=None, label=None, date_key=None):
    """
    V3.8.2 -- reserves one Claude-call attempt IMMEDIATELY, before the real
    `claude -p` subprocess is spawned. Returns an `attempt_id` the caller
    must pass to finish_attempt() once the call resolves.

    This attempt is counted in total_calls/unknown_cost_attempts THE
    INSTANT this function returns -- a crash/kill/timeout before
    finish_attempt() is ever called still leaves this PENDING entry in the
    ledger, honestly representing "we don't know what this attempt cost"
    rather than silently forgetting it happened. Never refunded.
    """
    date_key = date_key or today_key()
    attempt_id = uuid.uuid4().hex[:12]
    path = ledger_path(date_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            ledger = _read_locked(f, date_key)
            ledger["entries"].append({
                "attempt_id": attempt_id, "run_id": run_id, "label": label,
                "started_at": now_iso(), "completed_at": None, "status": "PENDING",
                "success": None, "cost_usd": None, "cost_observable": False,
                "input_tokens": None, "output_tokens": None, "tokens_observable": False,
                "duration_ms": None, "error_category": None,
            })
            _recompute_aggregates(ledger)
            _write_locked(f, ledger)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    return attempt_id


def finish_attempt(attempt_id, date_key=None, success=False, cost_usd=None, cost_observable=False,
                    input_tokens=None, output_tokens=None, tokens_observable=False,
                    duration_ms=None, error_category=None):
    """
    Enriches an attempt already reserved by start_attempt() with its real
    outcome. `cost_usd`/`input_tokens`/`output_tokens` are only trusted
    when their matching `_observable` flag is True (mirrors
    claude_invoke.py's own _extract_meta contract) -- an unobservable value
    passed here is stored as None regardless of what was passed, so a
    caller can never accidentally smuggle a guessed number into the ledger.

    Defensive: if `attempt_id` isn't found (should never happen -- every
    real caller always calls start_attempt() first), a new entry is
    appended rather than raising, so a bookkeeping inconsistency never
    crashes the caller's own error handling.
    """
    date_key = date_key or today_key()
    path = ledger_path(date_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            ledger = _read_locked(f, date_key)
            entry = next((e for e in ledger["entries"] if e.get("attempt_id") == attempt_id), None)
            if entry is None:
                entry = {"attempt_id": attempt_id, "run_id": None, "label": None, "started_at": None}
                ledger["entries"].append(entry)
            entry["completed_at"] = now_iso()
            entry["status"] = "SUCCESS" if success else "FAILED"
            entry["success"] = bool(success)
            entry["cost_observable"] = bool(cost_observable)
            entry["cost_usd"] = cost_usd if cost_observable else None
            entry["tokens_observable"] = bool(tokens_observable)
            entry["input_tokens"] = input_tokens if tokens_observable else None
            entry["output_tokens"] = output_tokens if tokens_observable else None
            entry["duration_ms"] = duration_ms
            entry["error_category"] = error_category
            _recompute_aggregates(ledger)
            _write_locked(f, ledger)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def remaining_budget(daily_budget_usd, date_key=None):
    """
    Returns (remaining_usd_or_None, spent_usd_or_None, cost_observable).
    remaining/spent are None when cost_observable is False for today --
    callers must never coerce None into 0 or into the full budget; either
    would misrepresent unknown spend as a real number. The call-count
    governor (config/discovery_only.yaml: max_claude_calls_per_run) is the
    correct fallback whenever cost_observable is False.
    """
    ledger = load_ledger(date_key)
    if not ledger.get("cost_observable", False):
        return None, None, False
    spent = ledger.get("total_cost_usd", 0.0)
    return round(max(0.0, daily_budget_usd - spent), 6), spent, True


def calls_today(date_key=None):
    return load_ledger(date_key).get("total_calls", 0)
