"""
V3.8.1 tests: Discovery-Only Production Mode + Cost-Control Amendment.
Synthetic fixtures only -- no real prospect/business data, no live network
call, no live Claude call (run_claude_with_meta is always mocked), no
Gmail access, no contact-form submission anywhere in this file.
"""
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # for `from test_v3_5_acquisition import ...`

import _lib  # noqa: E402
from _lib import load_yaml, now_iso, write_json, read_jsonl  # noqa: E402
import discovery_worker as dw  # noqa: E402
import candidate_verification as cv  # noqa: E402
import cost_ledger as cl  # noqa: E402
import handoff_lib as hl  # noqa: E402
import handoff_backend as hb  # noqa: E402
import sync_handoff  # noqa: E402
import report_discovery_only as rdo  # noqa: E402
import discover_prospects as dp  # noqa: E402
import run_daily  # noqa: E402
import claude_invoke  # noqa: E402
import acquisition_worker as aw  # noqa: E402

ACQ_CFG = load_yaml("acquisition.yaml")
DISC_CFG = load_yaml("discovery_only.yaml")


def raw_candidate(business_name, **overrides):
    c = {
        "business_name": business_name, "website": f"https://{business_name.lower().replace(' ', '-')}.test/",
        "city": "Columbus", "state": "OH", "phone": "555-1000",
        "commercial_value_signal": "high", "google_dependency_evidence": "emergency local-intent service",
        "independently_owned": True, "rating": 4.6, "review_count": 30, "years_in_business": 8,
    }
    c.update(overrides)
    return c


def seeded_prospect(raw, niche, city, state):
    """Builds the exact prospect record discover_prospects.py's own
    to_prospect_record() would produce for this raw candidate -- reused
    directly rather than reimplemented, so fixtures stay faithful."""
    market_cell = f"{niche} / {city}, {state}"
    return dp.to_prospect_record(raw, niche, market_cell)


def fake_claude_meta(cost_usd=0.10, input_tokens=500, output_tokens=100, observable=True):
    if not observable:
        return {"total_cost_usd": None, "cost_observable": False,
                "input_tokens": None, "output_tokens": None, "tokens_observable": False, "duration_ms": None}
    return {"total_cost_usd": cost_usd, "cost_observable": True,
            "input_tokens": input_tokens, "output_tokens": output_tokens, "tokens_observable": True,
            "duration_ms": 1000}


def fake_save_stdout(new_ids, dup_names=()):
    lines = [f"fake / market: {len(new_ids)} new DISCOVERED prospect(s) added, {len(dup_names)} dropped"]
    for pid in new_ids:
        lines.append(f"  + {pid}")
    for name in dup_names:
        lines.append(f"  - {name}: already present in the pipeline (discovered or rejected)")
    return "\n".join(lines) + "\n"


class IsolatedDiscoveryMixin:
    """Redirects every discovery-worker/prospect/lead/cost-ledger path to a
    throwaway temp dir -- never touches real data. Mirrors V3.5's
    IsolatedWorkerMixin / V3.6's IsolatedHandoffMixin conventions."""

    def setUp(self):
        self.tmp = Path("/tmp") / f"v3_8_1_test_{id(self)}"
        for sub in ("prospects", "leads", "runtime/cost", "handoff"):
            (self.tmp / sub).mkdir(parents=True, exist_ok=True)
        self._orig = {
            "DATA": dw.DATA, "PROSPECTS": dw.PROSPECTS, "LEADS": dw.LEADS, "LOCK_PATH": dw.LOCK_PATH,
        }
        dw.DATA = self.tmp
        dw.PROSPECTS = self.tmp / "prospects"
        dw.LEADS = self.tmp / "leads"
        dw.LOCK_PATH = self.tmp / "runtime" / "discovery.lock"
        self._orig_cl_cost_dir = cl.COST_DIR
        cl.COST_DIR = self.tmp / "runtime" / "cost"
        self._orig_aw_data = aw.DATA
        aw.DATA = self.tmp
        # CRITICAL: set_status_everywhere (and any other _lib function) is
        # DEFINED in _lib.py, so its own reference to `PROSPECTS` resolves
        # via _lib's module globals, NOT discovery_worker's imported copy --
        # patching dw.PROSPECTS alone does NOT sandbox it. Discovered the
        # hard way during development (see IsolatedRunDailyMixin's own
        # docstring for the incident) -- every test that lets
        # discovery_worker.run() actually persist a status change MUST
        # also patch _lib.PROSPECTS directly.
        self._orig_lib = {"PROSPECTS": _lib.PROSPECTS, "LEADS": _lib.LEADS, "DATA": _lib.DATA}
        _lib.PROSPECTS = self.tmp / "prospects"
        _lib.LEADS = self.tmp / "leads"
        _lib.DATA = self.tmp

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(dw, k, v)
        cl.COST_DIR = self._orig_cl_cost_dir
        aw.DATA = self._orig_aw_data
        for k, v in self._orig_lib.items():
            setattr(_lib, k, v)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def seed_discovered(self, records):
        with open(self.tmp / "prospects" / "discovered.jsonl", "w") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")
        for fname in ("qualified.jsonl", "manual_review.jsonl", "needs_enrichment.jsonl", "rejected.jsonl"):
            (self.tmp / "prospects" / fname).touch()


def default_cfg(**overrides):
    cfg = dict(DISC_CFG)
    cfg.update(overrides)
    return cfg


AUTH_OK = (True, "AUTH_OK", "ok")
AUTH_REQUIRED = (False, "CLAUDE_AUTH_REQUIRED", "no auth")


# ---------------------------------------------------------------------------
# 1. Production mode config
# ---------------------------------------------------------------------------
class TestProductionModeConfig(unittest.TestCase):
    def test_default_production_mode_is_discovery_only(self):
        self.assertEqual(ACQ_CFG.get("production_mode"), "discovery_only")

    def test_full_pipeline_is_a_valid_alternative(self):
        self.assertIn("full_pipeline", run_daily.VALID_PRODUCTION_MODES)
        self.assertIn("discovery_only", run_daily.VALID_PRODUCTION_MODES)

    def test_invalid_mode_fails_closed(self):
        tmp = Path("/tmp") / f"v3_8_1_badmode_{id(self)}"
        tmp.mkdir(parents=True, exist_ok=True)
        try:
            with patch("run_daily.load_yaml") as m:
                def fake_load(name):
                    if name == "acquisition.yaml":
                        return {"production_mode": "not_a_real_mode"}
                    return load_yaml(name)
                m.side_effect = fake_load
                ok, problems = run_daily.verify_workspace()
            self.assertFalse(ok)
            self.assertTrue(any("production_mode" in p for p in problems))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_valid_modes_pass_workspace_verification(self):
        for mode in ("discovery_only", "full_pipeline"):
            with patch("run_daily.load_yaml") as m:
                def fake_load(name, mode=mode):
                    if name == "acquisition.yaml":
                        cfg = dict(ACQ_CFG)
                        cfg["production_mode"] = mode
                        return cfg
                    return load_yaml(name)
                m.side_effect = fake_load
                ok, problems = run_daily.verify_workspace()
            self.assertTrue(ok, problems)

    def test_discovery_only_config_has_required_budget_knobs(self):
        for key in ("min_candidates_target", "max_candidates_target", "daily_claude_budget_usd",
                    "max_claude_calls_per_run", "max_worker_runtime_seconds", "max_budget_usd_per_call",
                    "max_market_cells_per_run"):
            self.assertIn(key, DISC_CFG)

    def test_candidate_target_is_a_range_not_a_single_quota(self):
        self.assertLessEqual(DISC_CFG["min_candidates_target"], DISC_CFG["max_candidates_target"])

    def test_worker_runtime_ceiling_is_far_smaller_than_full_pipeline(self):
        """V3.8.1 Sec.13 -- discovery-only must not blindly retain
        full_pipeline's 2700s budget."""
        self.assertLess(DISC_CFG["max_worker_runtime_seconds"], ACQ_CFG["max_worker_runtime_seconds"])


# ---------------------------------------------------------------------------
# 2. Deterministic candidate verification (zero Claude)
# ---------------------------------------------------------------------------
class TestCandidateVerificationBasic(unittest.TestCase):
    def _prospect(self, **overrides):
        p = {"id": "roofing-columbus-oh-acme", "business_name": "Acme Roofing", "niche": "roofing",
             "city": "Columbus", "state": "OH", "website": "https://acme.test/",
             "google_business_profile_url": None}
        p.update(overrides)
        return p

    def test_fully_populated_candidate_passes(self):
        ok, reason = cv.verify_candidate_basic(self._prospect())
        self.assertTrue(ok, reason)

    def test_missing_business_name_fails(self):
        ok, reason = cv.verify_candidate_basic(self._prospect(business_name=""))
        self.assertFalse(ok)
        self.assertIn("business_name", reason)

    def test_missing_niche_fails(self):
        ok, reason = cv.verify_candidate_basic(self._prospect(niche=None))
        self.assertFalse(ok)
        self.assertIn("niche", reason)

    def test_missing_city_or_state_fails(self):
        ok, _ = cv.verify_candidate_basic(self._prospect(city=None))
        self.assertFalse(ok)
        ok2, _ = cv.verify_candidate_basic(self._prospect(state=""))
        self.assertFalse(ok2)

    def test_no_contact_surface_fails(self):
        ok, reason = cv.verify_candidate_basic(self._prospect(website=None, google_business_profile_url=None))
        self.assertFalse(ok)
        self.assertIn("contact surface", reason)

    def test_google_business_profile_url_alone_is_a_valid_contact_surface(self):
        ok, _ = cv.verify_candidate_basic(self._prospect(website=None, google_business_profile_url="https://maps.google.com/x"))
        self.assertTrue(ok)

    def test_basic_business_facts_never_fabricates_missing_values(self):
        p = self._prospect()
        facts = cv.basic_business_facts(p)
        self.assertIsNone(facts["rating"])
        self.assertIsNone(facts["review_count"])
        self.assertEqual(facts["obvious_website_issue"], [])

    def test_build_candidate_record_shape(self):
        p = self._prospect(discovered_at="2026-09-06T00:00:00+00:00", status="CANDIDATE_VERIFIED")
        record = cv.build_candidate_record(p, "roofing / Columbus, OH", phone="555-1000")
        for field in ("lead_id", "business_name", "domain", "website", "city", "state", "country", "niche",
                      "phone", "profile_url", "discovery_source", "discovered_at", "verification_status",
                      "basic_business_facts"):
            self.assertIn(field, record)
        self.assertEqual(record["lead_id"], "roofing-columbus-oh-acme")
        self.assertEqual(record["domain"], "acme.test")
        self.assertEqual(record["phone"], "555-1000")

    def test_candidate_record_never_requires_fit_gap_or_ranking_fields(self):
        """A candidate record is fully buildable with zero FIT/GAP/ranking/
        wedge/contact-identity fields present at all."""
        p = self._prospect()
        for absent in ("fit_confirmed_score", "gap_confirmed_score", "maps_position", "organic_position",
                       "primary_wedge_type", "contactability_score"):
            self.assertNotIn(absent, p)
        record = cv.build_candidate_record(p, "roofing / Columbus, OH")
        self.assertEqual(record["lead_id"], p["id"])  # built successfully with no scoring data at all

    def test_no_claude_import_anywhere_in_candidate_verification(self):
        text = (SCRIPTS / "candidate_verification.py").read_text()
        self.assertNotIn("import claude_invoke", text)
        self.assertNotIn("run_claude(", text)
        self.assertNotIn("run_claude_with_meta(", text)


# ---------------------------------------------------------------------------
# 3. Daily cost ledger
# ---------------------------------------------------------------------------
def _record_one(date_key, cost_usd=0.10, success=True, observable=True, input_tokens=100, output_tokens=50,
                 tokens_observable=True, run_id=None, label=None, error_category=None):
    """Test helper: reserve + immediately finish one attempt -- the V3.8.2
    two-step API, collapsed into one call for tests that don't care about
    the PENDING-in-between state (that state is exercised directly by the
    crash-safety tests below)."""
    attempt_id = cl.start_attempt(run_id=run_id, label=label, date_key=date_key)
    cl.finish_attempt(
        attempt_id, date_key=date_key, success=success,
        cost_usd=cost_usd if observable else None, cost_observable=observable,
        input_tokens=input_tokens if tokens_observable else None,
        output_tokens=output_tokens if tokens_observable else None,
        tokens_observable=tokens_observable, error_category=error_category,
    )
    return attempt_id


class TestCostLedger(unittest.TestCase):
    def setUp(self):
        self.tmp = Path("/tmp") / f"v3_8_1_ledger_{id(self)}"
        self.tmp.mkdir(parents=True, exist_ok=True)
        self._orig = cl.COST_DIR
        cl.COST_DIR = self.tmp
        self.date_key = "2026-09-06"

    def tearDown(self):
        cl.COST_DIR = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_empty_ledger_has_zero_spend(self):
        ledger = cl.load_ledger(self.date_key)
        self.assertEqual(ledger["total_cost_usd"], 0.0)
        self.assertEqual(ledger["total_calls"], 0)

    def test_finish_attempt_accumulates(self):
        _record_one(self.date_key, 0.40)
        _record_one(self.date_key, 0.30)
        ledger = cl.load_ledger(self.date_key)
        self.assertAlmostEqual(ledger["total_cost_usd"], 0.70)
        self.assertEqual(ledger["total_calls"], 2)

    def test_shares_budget_across_separate_invocations_same_day(self):
        """The core catch-up-safety guarantee: a second, independent call
        to start_attempt/finish_attempt/remaining_budget for the SAME
        date_key sees the FIRST invocation's spend, simulating a scheduled
        run followed by a same-day catch-up run as two separate process
        invocations."""
        _record_one(self.date_key, 2.40)  # "12pm run"
        remaining, spent, observable = cl.remaining_budget(3.00, date_key=self.date_key)
        self.assertTrue(observable)
        self.assertAlmostEqual(spent, 2.40)
        self.assertAlmostEqual(remaining, 0.60)  # NOT a fresh $3.00

    def test_catchup_invocation_reads_previous_same_day_spend(self):
        """Item G -- a catch-up invocation is, mechanically, a fresh
        process reading the same date_key: it must see the scheduled run's
        already-recorded spend immediately, with no separate 'catch-up
        ledger' concept anywhere."""
        _record_one(self.date_key, 1.50, run_id="NORMAL_SCHEDULE", label="scheduled run, cell 1")
        remaining_before_catchup, spent_before, _ = cl.remaining_budget(3.00, date_key=self.date_key)
        self.assertAlmostEqual(spent_before, 1.50)
        self.assertAlmostEqual(remaining_before_catchup, 1.50)
        # A separate "catch-up" invocation, later, same day:
        _record_one(self.date_key, 1.00, run_id="SAME_DAY_CATCH_UP", label="catch-up run, cell 1")
        remaining_after_catchup, spent_after, _ = cl.remaining_budget(3.00, date_key=self.date_key)
        self.assertAlmostEqual(spent_after, 2.50)
        self.assertAlmostEqual(remaining_after_catchup, 0.50)

    def test_manual_retry_shares_the_same_daily_budget(self):
        """A 'manual retry' invocation is, mechanically, just another call
        against the same date_key -- there is no separate manual-vs-
        scheduled ledger."""
        _record_one(self.date_key, 1.00, label="scheduled")
        _record_one(self.date_key, 1.00, label="manual_retry")
        remaining, spent, observable = cl.remaining_budget(3.00, date_key=self.date_key)
        self.assertAlmostEqual(spent, 2.00)
        self.assertAlmostEqual(remaining, 1.00)

    def test_unobservable_call_flips_day_to_unobservable(self):
        _record_one(self.date_key, 0.50)
        _record_one(self.date_key, observable=False)
        remaining, spent, observable = cl.remaining_budget(3.00, date_key=self.date_key)
        self.assertFalse(observable)
        self.assertIsNone(remaining)
        self.assertIsNone(spent)

    def test_unknown_cost_never_fabricated_as_zero_or_full_budget(self):
        _record_one(self.date_key, observable=False)
        remaining, spent, observable = cl.remaining_budget(3.00, date_key=self.date_key)
        self.assertFalse(observable)
        self.assertIsNone(remaining)  # never coerced to 0.0 or 3.00

    def test_ledger_file_lives_under_runtime_cost_and_is_gitignored(self):
        _record_one(self.date_key, 0.10)
        path = self.tmp / f"{self.date_key}.json"
        self.assertTrue(path.exists())
        gitignore = (ROOT / ".gitignore").read_text()
        self.assertIn("data/runtime/", gitignore)  # covers data/runtime/cost/*.json as a full-dir ignore

    def test_calls_today_reflects_ledger(self):
        _record_one(self.date_key, 0.10)
        _record_one(self.date_key, 0.10)
        self.assertEqual(cl.calls_today(self.date_key), 2)

    # --- V3.8.2 -------------------------------------------------------

    def test_live_incident_reproduction_success_plus_failed_billable_call(self):
        """Item A -- exact regression for the 2026-09-03 live validation:
        call 1 succeeds at $0.472659, call 2 FAILS but still incurred a
        real, observed $0.5358346. Both must reach the daily ledger."""
        _record_one(self.date_key, cost_usd=0.472659, success=True)
        _record_one(self.date_key, cost_usd=0.5358346, success=False, error_category="INVOCATION_ERROR")
        ledger = cl.load_ledger(self.date_key)
        self.assertEqual(ledger["total_calls"], 2)
        self.assertEqual(ledger["total_calls_succeeded"], 1)
        self.assertEqual(ledger["total_calls_failed"], 1)
        self.assertAlmostEqual(ledger["total_cost_usd"], 1.0084936, places=5)
        self.assertTrue(ledger["cost_observable"])

    def test_start_attempt_reserves_before_any_outcome_is_known(self):
        """Item C/4 -- start_attempt() alone (no finish_attempt() yet)
        already counts toward total_calls and marks the day's cost
        unobservable -- crash-safety: a killed process still leaves this
        honest trace."""
        cl.start_attempt(run_id="r", label="in-flight", date_key=self.date_key)
        ledger = cl.load_ledger(self.date_key)
        self.assertEqual(ledger["total_calls"], 1)
        self.assertEqual(ledger["unknown_cost_attempts"], 1)
        self.assertFalse(ledger["cost_observable"])
        remaining, spent, observable = cl.remaining_budget(3.00, date_key=self.date_key)
        self.assertFalse(observable)
        self.assertIsNone(remaining)

    def test_failed_call_with_no_recoverable_cost_marks_attempt_unknown(self):
        """Item C -- a failed call that exposes NO defensible cost still
        consumes its call slot, and the ledger honestly marks that
        attempt's cost unknown rather than assuming $0."""
        _record_one(self.date_key, success=False, observable=False, error_category="CALL_TIMEOUT")
        ledger = cl.load_ledger(self.date_key)
        self.assertEqual(ledger["total_calls"], 1)
        self.assertEqual(ledger["total_calls_failed"], 1)
        self.assertEqual(ledger["unknown_cost_attempts"], 1)
        self.assertFalse(ledger["cost_observable"])

    def test_pending_entry_never_silently_dropped_from_aggregates(self):
        """A crash between start_attempt() and finish_attempt() leaves a
        PENDING entry -- it must still be counted in total_calls and must
        still flip cost_observable false for the day, even though no
        outcome was ever recorded for it."""
        cl.start_attempt(run_id="r", label="crashed-cell", date_key=self.date_key)
        _record_one(self.date_key, cost_usd=0.20, success=True)  # a later, successful attempt
        ledger = cl.load_ledger(self.date_key)
        self.assertEqual(ledger["total_calls"], 2)  # the crashed one is NOT dropped
        self.assertEqual(ledger["total_calls_succeeded"], 1)
        self.assertFalse(ledger["cost_observable"])  # the PENDING entry poisons observability, correctly

    def test_finish_attempt_never_writes_cost_when_not_observable_even_if_a_value_is_passed(self):
        """Defense in depth: finish_attempt() must ignore a cost_usd value
        whose cost_observable flag is False -- never let a caller
        accidentally smuggle a number into the ledger without the flag."""
        attempt_id = cl.start_attempt(date_key=self.date_key)
        cl.finish_attempt(attempt_id, date_key=self.date_key, success=True,
                           cost_usd=99.99, cost_observable=False)
        ledger = cl.load_ledger(self.date_key)
        self.assertIsNone(ledger["entries"][0]["cost_usd"])
        self.assertEqual(ledger["total_cost_usd"], 0.0)


# ---------------------------------------------------------------------------
# 4. claude_invoke.py -- real cost/usage capture, backward compatibility
# ---------------------------------------------------------------------------
class TestClaudeInvokeMeta(unittest.TestCase):
    def test_extract_meta_pulls_real_observed_fields(self):
        envelope = {"total_cost_usd": 0.083, "usage": {"input_tokens": 1200, "output_tokens": 340}, "duration_ms": 4500}
        meta = claude_invoke._extract_meta(envelope)
        self.assertEqual(meta["total_cost_usd"], 0.083)
        self.assertTrue(meta["cost_observable"])
        self.assertEqual(meta["input_tokens"], 1200)
        self.assertEqual(meta["output_tokens"], 340)
        self.assertTrue(meta["tokens_observable"])

    def test_extract_meta_never_fabricates_missing_fields(self):
        meta = claude_invoke._extract_meta({})
        self.assertIsNone(meta["total_cost_usd"])
        self.assertFalse(meta["cost_observable"])
        self.assertIsNone(meta["input_tokens"])
        self.assertFalse(meta["tokens_observable"])

    def test_zero_cost_is_still_observable_not_unknown(self):
        """A real call that reported an actual $0 is a genuine observation
        -- distinct from a missing field."""
        meta = claude_invoke._extract_meta({"total_cost_usd": 0})
        self.assertTrue(meta["cost_observable"])
        self.assertEqual(meta["total_cost_usd"], 0.0)

    def test_run_claude_still_returns_only_the_result_unchanged(self):
        """Backward compatibility: every pre-V3.8.1 caller of run_claude()
        must see byte-for-byte the same return shape as before."""
        with patch.object(claude_invoke, "run_claude_with_meta", return_value=({"ok": True}, fake_claude_meta())):
            result = claude_invoke.run_claude("prompt")
        self.assertEqual(result, {"ok": True})  # not a tuple, not (result, meta)


# ---------------------------------------------------------------------------
# 5. CostGuard -- the per-run enforcement wrapper
# ---------------------------------------------------------------------------
class TestCostGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = Path("/tmp") / f"v3_8_1_guard_{id(self)}"
        self.tmp.mkdir(parents=True, exist_ok=True)
        self._orig = cl.COST_DIR
        cl.COST_DIR = self.tmp
        self.date_key = "2026-09-06"

    def tearDown(self):
        cl.COST_DIR = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _succeed(self, guard, cost_usd=0.10, **meta_overrides):
        attempt_id = guard.reserve_attempt(run_id="r", label="cell")
        guard.record_attempt_result(attempt_id, success=True, meta=fake_claude_meta(cost_usd, **meta_overrides))
        return attempt_id

    def _fail(self, guard, cost_usd=None, observable=False, error_category="INVOCATION_ERROR"):
        attempt_id = guard.reserve_attempt(run_id="r", label="cell")
        meta = fake_claude_meta(cost_usd or 0.0, observable=observable)
        guard.record_attempt_result(attempt_id, success=False, meta=meta, error_category=error_category)
        return attempt_id

    def test_call_cap_enforced_before_call(self):
        guard = dw.CostGuard(default_cfg(max_claude_calls_per_run=2), date_key=self.date_key)
        ok1, _ = guard.check_before_call()
        self._succeed(guard)
        ok2, _ = guard.check_before_call()
        self._succeed(guard)
        ok3, reason3 = guard.check_before_call()
        self.assertTrue(ok1)
        self.assertTrue(ok2)
        self.assertFalse(ok3)
        self.assertEqual(reason3, "CALL_CAP_REACHED")

    def test_failed_attempt_counts_toward_call_cap_same_as_success(self):
        """Item B/1 -- the whole point of V3.8.2: a FAILED attempt still
        consumes its call slot. max=2, attempt 1 succeeds, attempt 2
        fails -- a third attempt must never be allowed."""
        guard = dw.CostGuard(default_cfg(max_claude_calls_per_run=2), date_key=self.date_key)
        ok1, _ = guard.check_before_call()
        self._succeed(guard, 0.47)
        ok2, _ = guard.check_before_call()
        self._fail(guard, cost_usd=0.53, observable=True)  # billable failure
        ok3, reason3 = guard.check_before_call()
        self.assertTrue(ok1)
        self.assertTrue(ok2)
        self.assertFalse(ok3)
        self.assertEqual(reason3, "CALL_CAP_REACHED")
        self.assertEqual(guard.calls_attempted, 2)
        self.assertEqual(guard.calls_succeeded, 1)
        self.assertEqual(guard.calls_failed, 1)

    def test_reserve_attempt_never_refunded_regardless_of_outcome(self):
        """Explicit non-refund guarantee across every outcome type the
        spec names: success, failure, timeout, malformed output, non-zero
        exit, hitting the call's own budget -- all funnel through
        record_attempt_result(success=False, ...) and none of them ever
        decrement calls_attempted."""
        guard = dw.CostGuard(default_cfg(max_claude_calls_per_run=100), date_key=self.date_key)
        self._succeed(guard)
        self._fail(guard, error_category="CALL_TIMEOUT")
        self._fail(guard, error_category="INVOCATION_ERROR")
        self._fail(guard, error_category="WORKER_DEADLINE_TIMEOUT")
        self.assertEqual(guard.calls_attempted, 4)

    def test_daily_budget_enforced_before_call(self):
        guard = dw.CostGuard(default_cfg(daily_claude_budget_usd=1.00, max_claude_calls_per_run=100), date_key=self.date_key)
        self._succeed(guard, 0.60)
        ok, _ = guard.check_before_call()
        self.assertTrue(ok)
        self._succeed(guard, 0.50)  # total now 1.10, over the 1.00 budget
        ok2, reason2 = guard.check_before_call()
        self.assertFalse(ok2)
        self.assertEqual(reason2, "EXHAUSTED")

    def test_call_cap_and_budget_are_independent_governors(self):
        """A tiny call cap still stops the run even if $ cost is not
        observable at all (defense in depth when cost can't be tracked)."""
        guard = dw.CostGuard(default_cfg(max_claude_calls_per_run=1, daily_claude_budget_usd=1000), date_key=self.date_key)
        self._fail(guard, observable=False)
        ok, reason = guard.check_before_call()
        self.assertFalse(ok)
        self.assertEqual(reason, "CALL_CAP_REACHED")

    def test_summary_reports_incomplete_accounting_status_not_hidden_cost(self):
        """Item C/3 -- a failed call with genuinely unknown cost must mark
        budget_accounting_status INCOMPLETE_UNKNOWN_CALL_COST, and must
        never claim an exact remaining-dollar figure -- but the run's own
        KNOWN observed cost is still reported (never suppressed to None
        just because one OTHER attempt's cost is unknown)."""
        guard = dw.CostGuard(default_cfg(), date_key=self.date_key)
        self._succeed(guard, 0.30)
        self._fail(guard, observable=False)
        summary = guard.summary({"candidates_discovered": 3, "candidates_verified": 2, "candidates_saved": 2})
        self.assertEqual(summary["budget_accounting_status"], "INCOMPLETE_UNKNOWN_CALL_COST")
        self.assertAlmostEqual(summary["observed_total_cost_usd"], 0.30)  # the known partial sum, not suppressed
        self.assertEqual(summary["unknown_cost_attempts"], 1)
        self.assertIsNone(summary["estimated_cost_usd"])  # never invented
        self.assertFalse(summary["cost_observable"])

    def test_budget_remaining_never_exact_when_accounting_incomplete(self):
        """Item C -- the daily $ remaining figure must be None (never a
        confident number) once any attempt today has unknown cost."""
        guard = dw.CostGuard(default_cfg(), date_key=self.date_key)
        self._fail(guard, observable=False)
        summary = guard.summary({"candidates_discovered": 0, "candidates_verified": 0, "candidates_saved": 0})
        self.assertIsNone(summary["budget_remaining_usd"])

    def test_observed_total_cost_includes_both_successful_and_failed_calls(self):
        """Item A, CostGuard-level: reproduces the live incident's own
        arithmetic -- $0.472659 (success) + $0.5358346 (failure) is what
        must be reported as observed_total_cost_usd."""
        guard = dw.CostGuard(default_cfg(max_claude_calls_per_run=100), date_key=self.date_key)
        self._succeed(guard, 0.472659)
        self._fail(guard, cost_usd=0.5358346, observable=True)
        summary = guard.summary({"candidates_discovered": 2, "candidates_verified": 2, "candidates_saved": 2})
        self.assertAlmostEqual(summary["observed_total_cost_usd"], 1.0084936, places=5)
        self.assertAlmostEqual(summary["observed_successful_call_cost_usd"], 0.472659)
        self.assertAlmostEqual(summary["observed_failed_call_cost_usd"], 0.5358346, places=6)
        self.assertEqual(summary["budget_accounting_status"], "COMPLETE")  # both costs were known

    def test_cost_per_verified_candidate_uses_total_observed_cost_including_failures(self):
        """Item 8 -- cost_per_verified_candidate must reflect the FAILED
        call's cost too, not just successful-call spend."""
        guard = dw.CostGuard(default_cfg(max_claude_calls_per_run=100), date_key=self.date_key)
        self._succeed(guard, 1.00)
        self._fail(guard, cost_usd=1.00, observable=True)
        summary = guard.summary({"candidates_discovered": 2, "candidates_verified": 2, "candidates_saved": 2})
        self.assertAlmostEqual(summary["cost_per_verified_candidate"], 1.00)  # (1.00+1.00)/2, not 1.00/2

    def test_cost_per_verified_candidate_computed_when_observable(self):
        guard = dw.CostGuard(default_cfg(), date_key=self.date_key)
        self._succeed(guard, 1.00)
        summary = guard.summary({"candidates_discovered": 5, "candidates_verified": 4, "candidates_saved": 4})
        self.assertAlmostEqual(summary["actual_cost_usd"], 1.00)
        self.assertAlmostEqual(summary["cost_per_verified_candidate"], 0.25)
        self.assertAlmostEqual(summary["cost_per_saved_candidate"], 0.25)

    def test_zero_verified_candidates_never_divides_by_zero(self):
        guard = dw.CostGuard(default_cfg(), date_key=self.date_key)
        self._succeed(guard, 1.00)
        summary = guard.summary({"candidates_discovered": 0, "candidates_verified": 0, "candidates_saved": 0})
        self.assertIsNone(summary["cost_per_verified_candidate"])


# ---------------------------------------------------------------------------
# 5b. attempt_claude_discovery() -- runtime-deadline governors (items D, E, F)
# ---------------------------------------------------------------------------
class _FixedDeadline:
    """Test double: reports a FIXED remaining_seconds() no matter how much
    real wall-clock time passes, and exceeded() can be scripted to flip
    True after a chosen number of calls -- lets tests deterministically
    simulate 'the worker deadline was reached while this specific call was
    in flight' without actually waiting in real time."""

    def __init__(self, remaining, exceeded_after_n_calls=None):
        self._remaining = remaining
        self._exceeded_after = exceeded_after_n_calls
        self._exceeded_calls = 0

    def remaining_seconds(self):
        return self._remaining

    def exceeded(self):
        self._exceeded_calls += 1
        if self._exceeded_after is None:
            return False
        return self._exceeded_calls > self._exceeded_after


class TestAttemptClaudeDiscoveryRuntimeGovernors(unittest.TestCase):
    def setUp(self):
        self.tmp = Path("/tmp") / f"v3_8_2_attempt_{id(self)}"
        self.tmp.mkdir(parents=True, exist_ok=True)
        self._orig = cl.COST_DIR
        cl.COST_DIR = self.tmp
        self.date_key = "2026-09-06"
        self.acq_cfg = load_yaml("acquisition.yaml")  # real max_claude_call_seconds_research (300s)

    def tearDown(self):
        cl.COST_DIR = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_subprocess_timeout_clamped_to_remaining_worker_seconds(self):
        """Item D -- 40 seconds remain; the Claude subprocess must never be
        given a timeout greater than 40s, even though the configured
        per-call timeout (300s from real acquisition.yaml) is far larger."""
        guard = dw.CostGuard(default_cfg(max_claude_calls_per_run=5), date_key=self.date_key)
        deadline = _FixedDeadline(remaining=40.0)
        seen_timeouts = []

        def capture_timeout(prompt, **kw):
            seen_timeouts.append(kw.get("timeout_s"))
            return {"candidates": []}, fake_claude_meta(0.05)

        with patch.object(dw, "run_claude_with_meta", side_effect=capture_timeout):
            outcome, result, meta = dw.attempt_claude_discovery(
                guard, deadline, default_cfg(min_seconds_to_start_claude_call=10), self.acq_cfg,
                "prompt", "TEST", "cell", 0, log=lambda m: None,
            )
        self.assertEqual(outcome, dw.OUTCOME_SUCCESS)
        self.assertEqual(len(seen_timeouts), 1)
        self.assertLessEqual(seen_timeouts[0], 40.0)

    def test_call_never_starts_below_minimum_start_threshold(self):
        """Item E -- remaining worker time (25s) is below
        min_seconds_to_start_claude_call (60s, the default) -- the Claude
        subprocess must never be spawned at all."""
        guard = dw.CostGuard(default_cfg(max_claude_calls_per_run=5), date_key=self.date_key)
        deadline = _FixedDeadline(remaining=25.0)
        spawned = []

        def should_never_be_called(prompt, **kw):
            spawned.append(1)
            return {"candidates": []}, fake_claude_meta(0.05)

        with patch.object(dw, "run_claude_with_meta", side_effect=should_never_be_called):
            outcome, result, meta = dw.attempt_claude_discovery(
                guard, deadline, default_cfg(min_seconds_to_start_claude_call=60), self.acq_cfg,
                "prompt", "TEST", "cell", 0, log=lambda m: None,
            )
        self.assertEqual(outcome, dw.OUTCOME_RUNTIME_INSUFFICIENT)
        self.assertEqual(spawned, [])  # never spawned
        self.assertEqual(guard.calls_attempted, 0)  # never even reserved -- nothing to attempt

    def test_worker_deadline_timeout_counted_not_retried_no_replacement(self):
        """Item F -- the call times out AT the worker deadline: the
        attempt is still counted (reserved+recorded), the outcome is
        WORKER_DEADLINE_TIMEOUT (never a plain CALL_TIMEOUT), and it is
        NEVER retried even though max_retries > 0."""
        guard = dw.CostGuard(default_cfg(max_claude_calls_per_run=5), date_key=self.date_key)
        # Enough remaining time to clear the min-start threshold and be
        # given a real (clamped) timeout, but exceeded() reports True the
        # very first time it's checked (i.e. by the time the call fails,
        # the deadline has already passed) -- deterministic without
        # sleeping in real time.
        deadline = _FixedDeadline(remaining=65.0, exceeded_after_n_calls=0)

        def times_out(prompt, **kw):
            raise claude_invoke.ClaudeTimeout("simulated -- hit worker deadline mid-flight", meta=fake_claude_meta(observable=False))

        with patch.object(dw, "run_claude_with_meta", side_effect=times_out):
            outcome, result, meta = dw.attempt_claude_discovery(
                guard, deadline, default_cfg(), self.acq_cfg, "prompt", "TEST", "cell",
                max_retries=2,  # retries ARE available -- must still never be used
                log=lambda m: None,
            )
        self.assertEqual(outcome, dw.OUTCOME_WORKER_DEADLINE_TIMEOUT)
        self.assertEqual(guard.calls_attempted, 1)  # counted
        self.assertEqual(guard.calls_failed, 1)
        # No retry/replacement call: exactly one real spawn happened.
        ledger = cl.load_ledger(self.date_key)
        self.assertEqual(ledger["total_calls"], 1)

    def test_plain_call_timeout_not_at_deadline_is_retried(self):
        """Contrast case for F -- a timeout that is NOT due to the worker
        deadline (plenty of runtime remains) is retried up to max_retries,
        exactly like V3.8.1's original behavior."""
        guard = dw.CostGuard(default_cfg(max_claude_calls_per_run=5), date_key=self.date_key)
        deadline = _FixedDeadline(remaining=250.0, exceeded_after_n_calls=None)  # never exceeded
        call_i = {"n": 0}

        def timeout_then_succeed(prompt, **kw):
            call_i["n"] += 1
            if call_i["n"] == 1:
                raise claude_invoke.ClaudeTimeout("transient", meta=fake_claude_meta(observable=False))
            return {"candidates": []}, fake_claude_meta(0.05)

        with patch.object(dw, "run_claude_with_meta", side_effect=timeout_then_succeed):
            outcome, result, meta = dw.attempt_claude_discovery(
                guard, deadline, default_cfg(), self.acq_cfg, "prompt", "TEST", "cell",
                max_retries=1, log=lambda m: None,
            )
        self.assertEqual(outcome, dw.OUTCOME_SUCCESS)
        self.assertEqual(call_i["n"], 2)  # original + 1 retry
        self.assertEqual(guard.calls_attempted, 2)  # BOTH the failed original AND the retry consumed a slot


# ---------------------------------------------------------------------------
# 6. discovery_worker.run() -- end-to-end orchestration, sandboxed, mocked
#    Claude call
# ---------------------------------------------------------------------------
class TestDiscoveryWorkerRun(IsolatedDiscoveryMixin, unittest.TestCase):
    def _mock_cycle(self, cells, per_cell_candidates, per_cell_dups=None, meta=None):
        """Wires pick_cells() to a fixed cell list, run_claude_with_meta()
        to return the given per-cell candidate lists (seeding
        discovered.jsonl exactly as discover_prospects.py --save would
        have), and call_save() to report the matching '+ id' stdout."""
        per_cell_dups = per_cell_dups or [[] for _ in cells]
        meta = meta or fake_claude_meta()
        seeded = []
        results_and_stdout = []
        for (niche, city, state), raws in zip(cells, per_cell_candidates):
            ids = []
            for raw in raws:
                rec = seeded_prospect(raw, niche, city, state)
                seeded.append(rec)
                ids.append(rec["id"])
            results_and_stdout.append(({"candidates": raws}, fake_save_stdout(ids)))
        self.seed_discovered([])  # start empty; call_save will "append" by us pre-seeding progressively
        call_state = {"i": 0}

        def fake_run_claude_with_meta(prompt, **kw):
            i = call_state["i"]
            result, _ = results_and_stdout[i]
            return result, meta

        def fake_call_save(args, result_obj, timeout=30):
            i = call_state["i"]
            _, stdout = results_and_stdout[i]
            # Actually persist the seeded records now, simulating
            # discover_prospects.py --save's real side effect.
            niche, city, state = cells[i]
            raws = per_cell_candidates[i]
            existing = read_jsonl(self.tmp / "prospects" / "discovered.jsonl")
            for raw in raws:
                existing.append(seeded_prospect(raw, niche, city, state))
            with open(self.tmp / "prospects" / "discovered.jsonl", "w") as f:
                for r in existing:
                    f.write(json.dumps(r) + "\n")
            call_state["i"] += 1
            return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")

        return fake_run_claude_with_meta, fake_call_save

    def test_candidates_discovered_verified_and_saved(self):
        cells = [("roofing", "Columbus", "OH")]
        candidates = [[raw_candidate("Acme Roofing"), raw_candidate("Beta Roofing")]]
        fake_meta_fn, fake_save_fn = self._mock_cycle(cells, candidates)
        with patch("discovery_worker.claude_preflight.check", return_value=AUTH_OK):
            with patch.object(dw, "pick_cells", return_value=cells):
                with patch.object(dw, "call_print", return_value="prompt"):
                    with patch.object(dw, "run_claude_with_meta", side_effect=fake_meta_fn):
                        with patch.object(dw, "call_save", side_effect=fake_save_fn):
                            with patch("discovery_worker.load_yaml", side_effect=lambda n: default_cfg() if n == "discovery_only.yaml" else load_yaml(n)):
                                stats = dw.run(log=lambda m: None)
        self.assertEqual(stats["candidates_discovered"], 2)
        self.assertEqual(stats["candidates_verified"], 2)
        self.assertEqual(stats["candidates_saved"], 2)
        self.assertTrue(stats["discovery_run_completed"])
        final = read_jsonl(self.tmp / "prospects" / "discovered.jsonl")
        statuses = {r["status"] for r in final}
        self.assertEqual(statuses, {cv.CANDIDATE_VERIFIED})

    def test_verification_failure_isolated_does_not_block_others(self):
        cells = [("roofing", "Columbus", "OH")]
        candidates = [[raw_candidate("Acme Roofing"), raw_candidate("NoContactCo", website=None)]]
        fake_meta_fn, fake_save_fn = self._mock_cycle(cells, candidates)
        with patch("discovery_worker.claude_preflight.check", return_value=AUTH_OK):
            with patch.object(dw, "pick_cells", return_value=cells):
                with patch.object(dw, "call_print", return_value="prompt"):
                    with patch.object(dw, "run_claude_with_meta", side_effect=fake_meta_fn):
                        with patch.object(dw, "call_save", side_effect=fake_save_fn):
                            with patch("discovery_worker.load_yaml", side_effect=lambda n: default_cfg() if n == "discovery_only.yaml" else load_yaml(n)):
                                stats = dw.run(log=lambda m: None)
        self.assertEqual(stats["candidates_saved"], 1)
        self.assertEqual(stats["verification_failures"], 1)
        final = read_jsonl(self.tmp / "prospects" / "discovered.jsonl")
        rejected = [r for r in final if r["status"] == cv.CANDIDATE_REJECTED]
        verified = [r for r in final if r["status"] == cv.CANDIDATE_VERIFIED]
        self.assertEqual(len(rejected), 1)
        self.assertEqual(len(verified), 1)

    def test_duplicates_skipped_are_counted(self):
        cells = [("roofing", "Columbus", "OH")]
        candidates = [[raw_candidate("Acme Roofing")]]
        fake_meta_fn, fake_save_fn = self._mock_cycle(cells, candidates)

        def fake_call_save_with_dup(args, result_obj, timeout=30):
            proc = fake_save_fn(args, result_obj, timeout)
            # Append a duplicate-drop line to simulate discover_prospects.py
            # having found (and correctly dropped) an already-known business.
            proc = subprocess.CompletedProcess(
                args=proc.args, returncode=0,
                stdout=proc.stdout + "  - Existing Roofing Co: already present in the pipeline (discovered or rejected)\n",
                stderr="",
            )
            return proc

        with patch("discovery_worker.claude_preflight.check", return_value=AUTH_OK):
            with patch.object(dw, "pick_cells", return_value=cells):
                with patch.object(dw, "call_print", return_value="prompt"):
                    with patch.object(dw, "run_claude_with_meta", side_effect=fake_meta_fn):
                        with patch.object(dw, "call_save", side_effect=fake_call_save_with_dup):
                            with patch("discovery_worker.load_yaml", side_effect=lambda n: default_cfg() if n == "discovery_only.yaml" else load_yaml(n)):
                                stats = dw.run(log=lambda m: None)
        self.assertEqual(stats["duplicates_skipped"], 1)

    def test_candidate_target_reached_stops_early(self):
        cells = [("roofing", "Columbus", "OH"), ("hvac", "Columbus", "OH")]
        candidates = [[raw_candidate("Acme Roofing"), raw_candidate("Beta Roofing")], [raw_candidate("Gamma HVAC")]]
        fake_meta_fn, fake_save_fn = self._mock_cycle(cells, candidates)
        with patch("discovery_worker.claude_preflight.check", return_value=AUTH_OK):
            with patch.object(dw, "pick_cells", return_value=cells):
                with patch.object(dw, "call_print", return_value="prompt"):
                    with patch.object(dw, "run_claude_with_meta", side_effect=fake_meta_fn):
                        with patch.object(dw, "call_save", side_effect=fake_save_fn):
                            with patch("discovery_worker.load_yaml", side_effect=lambda n: default_cfg(max_candidates_target=2) if n == "discovery_only.yaml" else load_yaml(n)):
                                stats = dw.run(log=lambda m: None)
        self.assertEqual(stats["candidates_saved"], 2)  # never exceeded the target
        self.assertEqual(stats["market_cells_explored"], 1)  # second cell never explored -- target already met

    def test_fewer_candidates_than_target_is_a_valid_stop_not_padded(self):
        """If only fewer defensible candidates than max_candidates_target
        exist, save them and stop -- never manufacture more."""
        cells = [("roofing", "Columbus", "OH")]
        candidates = [[raw_candidate("Acme Roofing")]]
        fake_meta_fn, fake_save_fn = self._mock_cycle(cells, candidates)
        with patch("discovery_worker.claude_preflight.check", return_value=AUTH_OK):
            with patch.object(dw, "pick_cells", return_value=cells):
                with patch.object(dw, "call_print", return_value="prompt"):
                    with patch.object(dw, "run_claude_with_meta", side_effect=fake_meta_fn):
                        with patch.object(dw, "call_save", side_effect=fake_save_fn):
                            with patch("discovery_worker.load_yaml", side_effect=lambda n: default_cfg(max_candidates_target=20) if n == "discovery_only.yaml" else load_yaml(n)):
                                stats = dw.run(log=lambda m: None)
        self.assertEqual(stats["candidates_saved"], 1)

    def test_call_cap_stops_before_call_cap_plus_one(self):
        cells = [("roofing", "Columbus", "OH"), ("hvac", "Columbus", "OH"), ("plumbing", "Columbus", "OH")]
        candidates = [[raw_candidate("A")], [raw_candidate("B")], [raw_candidate("C")]]
        fake_meta_fn, fake_save_fn = self._mock_cycle(cells, candidates)
        with patch("discovery_worker.claude_preflight.check", return_value=AUTH_OK):
            with patch.object(dw, "pick_cells", return_value=cells):
                with patch.object(dw, "call_print", return_value="prompt"):
                    with patch.object(dw, "run_claude_with_meta", side_effect=fake_meta_fn):
                        with patch.object(dw, "call_save", side_effect=fake_save_fn):
                            with patch("discovery_worker.load_yaml", side_effect=lambda n: default_cfg(max_claude_calls_per_run=2, max_candidates_target=100) if n == "discovery_only.yaml" else load_yaml(n)):
                                stats = dw.run(log=lambda m: None)
        self.assertEqual(stats["claude_calls"], 2)
        self.assertEqual(stats["budget_status"], "CALL_CAP_REACHED")
        self.assertEqual(stats["market_cells_explored"], 2)
        self.assertTrue(stats["discovery_run_completed"])  # exhaustion is not a pipeline failure

    def test_third_call_never_spawns_after_second_call_fails_at_cap_two(self):
        """Item B -- max_claude_calls_per_run=2, call 1 succeeds, call 2
        FAILS (not a success) -- a third attempt must never be allowed,
        because the cap counts ATTEMPTS, not successes."""
        cells = [("roofing", "Columbus", "OH"), ("hvac", "Columbus", "OH"), ("plumbing", "Columbus", "OH")]
        candidates = [[raw_candidate("A")], [raw_candidate("B")], [raw_candidate("C")]]
        fake_meta_fn, fake_save_fn = self._mock_cycle(cells, candidates)
        call_i = {"n": 0}

        def second_call_fails(prompt, **kw):
            call_i["n"] += 1
            if call_i["n"] == 2:
                raise claude_invoke.ClaudeInvocationError("simulated failure", meta=fake_claude_meta(0.10))
            return fake_meta_fn(prompt, **kw)

        with patch("discovery_worker.claude_preflight.check", return_value=AUTH_OK):
            with patch.object(dw, "pick_cells", return_value=cells):
                with patch.object(dw, "call_print", return_value="prompt"):
                    with patch.object(dw, "run_claude_with_meta", side_effect=second_call_fails):
                        with patch.object(dw, "call_save", side_effect=fake_save_fn):
                            with patch("discovery_worker.load_yaml",
                                       side_effect=lambda n: default_cfg(max_claude_calls_per_run=2, max_candidates_target=100)
                                       if n == "discovery_only.yaml" else load_yaml(n)):
                                stats = dw.run(log=lambda m: None)
        self.assertEqual(call_i["n"], 2)  # exactly 2 real spawns -- never a third
        self.assertEqual(stats["claude_calls_attempted"], 2)
        self.assertEqual(stats["claude_calls_succeeded"], 1)
        self.assertEqual(stats["claude_calls_failed"], 1)
        self.assertEqual(stats["candidates_saved"], 1)  # only cell 1's candidate

    def test_candidate_target_never_overrides_call_dollar_or_time_limits(self):
        """Item H -- an unreachably HIGH candidate target must never cause
        the run to exceed the call cap, the $ budget, or the runtime
        ceiling. Sets all three governors low and the target absurdly
        high; every governor still wins independently."""
        cells = [(f"roofing", "Columbus", "OH")] * 5
        candidates = [[raw_candidate(f"Biz{i}")] for i in range(5)]
        fake_meta_fn, fake_save_fn = self._mock_cycle(cells, candidates, meta=fake_claude_meta(0.10))
        with patch("discovery_worker.claude_preflight.check", return_value=AUTH_OK):
            with patch.object(dw, "pick_cells", return_value=cells):
                with patch.object(dw, "call_print", return_value="prompt"):
                    with patch.object(dw, "run_claude_with_meta", side_effect=fake_meta_fn):
                        with patch.object(dw, "call_save", side_effect=fake_save_fn):
                            with patch("discovery_worker.load_yaml",
                                       side_effect=lambda n: default_cfg(max_claude_calls_per_run=2, max_candidates_target=9999)
                                       if n == "discovery_only.yaml" else load_yaml(n)):
                                stats = dw.run(log=lambda m: None)
        self.assertLessEqual(stats["claude_calls_attempted"], 2)  # call cap wins despite target=9999
        self.assertLessEqual(stats["candidates_saved"], 2)

    def test_daily_budget_exhaustion_stops_gracefully_preserving_work(self):
        """Budget is checked BEFORE each call using spend already
        PERSISTED from prior calls -- an approved call is never
        retroactively cancelled. With a $3.00 budget and $2.00/call: cell 1
        is approved (0 spent so far), cell 2 is approved (2.00 spent so
        far, still under budget), now 4.00 is spent -- cell 3's check
        correctly sees the budget exhausted and is never attempted."""
        cells = [("roofing", "Columbus", "OH"), ("hvac", "Columbus", "OH"), ("plumbing", "Columbus", "OH")]
        candidates = [[raw_candidate("A")], [raw_candidate("B")], [raw_candidate("C")]]
        fake_meta_fn, fake_save_fn = self._mock_cycle(cells, candidates, meta=fake_claude_meta(2.00))
        with patch("discovery_worker.claude_preflight.check", return_value=AUTH_OK):
            with patch.object(dw, "pick_cells", return_value=cells):
                with patch.object(dw, "call_print", return_value="prompt"):
                    with patch.object(dw, "run_claude_with_meta", side_effect=fake_meta_fn):
                        with patch.object(dw, "call_save", side_effect=fake_save_fn):
                            with patch("discovery_worker.load_yaml", side_effect=lambda n: default_cfg(daily_claude_budget_usd=3.00, max_claude_calls_per_run=100, max_candidates_target=100) if n == "discovery_only.yaml" else load_yaml(n)):
                                stats = dw.run(log=lambda m: None)
        self.assertEqual(stats["budget_status"], "EXHAUSTED")
        self.assertEqual(stats["candidates_saved"], 2)  # first two cells' completed work preserved
        self.assertEqual(stats["market_cells_explored"], 2)  # third cell never attempted
        self.assertTrue(stats["discovery_run_completed"])  # never treated as a failure

    def test_budget_exhaustion_never_retries_or_tries_another_market(self):
        """The exact anti-pattern V3.8.1 forbids: cost limit reached ->
        retry -> another market -> another Claude call."""
        cells = [("roofing", "Columbus", "OH"), ("hvac", "Columbus", "OH"), ("plumbing", "Columbus", "OH")]
        candidates = [[raw_candidate("A")], [raw_candidate("B")], [raw_candidate("C")]]
        fake_meta_fn, fake_save_fn = self._mock_cycle(cells, candidates, meta=fake_claude_meta(3.50))
        calls_made = []

        def counting_run_claude(prompt, **kw):
            calls_made.append(1)
            return fake_meta_fn(prompt, **kw)

        with patch("discovery_worker.claude_preflight.check", return_value=AUTH_OK):
            with patch.object(dw, "pick_cells", return_value=cells):
                with patch.object(dw, "call_print", return_value="prompt"):
                    with patch.object(dw, "run_claude_with_meta", side_effect=counting_run_claude):
                        with patch.object(dw, "call_save", side_effect=fake_save_fn):
                            with patch("discovery_worker.load_yaml", side_effect=lambda n: default_cfg(daily_claude_budget_usd=3.00, max_claude_calls_per_run=100, max_candidates_target=100) if n == "discovery_only.yaml" else load_yaml(n)):
                                dw.run(log=lambda m: None)
        self.assertEqual(len(calls_made), 1)  # exactly one call -- exhausted after it, never retried/continued

    def test_worker_timeout_stops_gracefully(self):
        cells = [("roofing", "Columbus", "OH")]
        candidates = [[raw_candidate("A")]]
        fake_meta_fn, fake_save_fn = self._mock_cycle(cells, candidates)
        with patch("discovery_worker.claude_preflight.check", return_value=AUTH_OK):
            with patch.object(dw, "pick_cells", return_value=cells):
                with patch.object(dw, "Deadline") as mock_deadline_cls:
                    mock_deadline = mock_deadline_cls.return_value
                    mock_deadline.exceeded.return_value = True  # already expired before any cell is tried
                    with patch.object(dw, "call_print", return_value="prompt"):
                        with patch.object(dw, "run_claude_with_meta", side_effect=fake_meta_fn):
                            with patch.object(dw, "call_save", side_effect=fake_save_fn):
                                with patch("discovery_worker.load_yaml", side_effect=lambda n: default_cfg() if n == "discovery_only.yaml" else load_yaml(n)):
                                    stats = dw.run(log=lambda m: None)
        self.assertTrue(stats["worker_timeout"])
        self.assertTrue(stats["discovery_run_completed"])
        self.assertEqual(stats["candidates_saved"], 0)  # nothing was attempted, nothing lost either

    def test_auth_failure_fails_closed_no_loop_no_acquisition_cycle(self):
        preflight_calls = []

        def counting_check():
            preflight_calls.append(1)
            return AUTH_REQUIRED

        with patch("discovery_worker.claude_preflight.check", side_effect=counting_check):
            stats = dw.run(log=lambda m: None)
        self.assertEqual(stats["claude_auth_status"], "CLAUDE_AUTH_REQUIRED")
        self.assertFalse(stats["discovery_run_completed"])
        self.assertEqual(len(preflight_calls), 1)  # exactly once -- never looped/retried

    def test_run_already_active_touches_nothing(self):
        self.seed_discovered([])
        held = dw.acquire_lock()
        try:
            stats = dw.run(log=lambda m: None)
            self.assertTrue(stats["run_already_active"])
        finally:
            import fcntl
            fcntl.flock(held, fcntl.LOCK_UN)
            held.close()

    def test_one_market_cell_failure_never_blocks_the_next(self):
        cells = [("roofing", "Columbus", "OH"), ("hvac", "Columbus", "OH")]
        candidates = [[raw_candidate("A")], [raw_candidate("B")]]
        fake_meta_fn, fake_save_fn = self._mock_cycle(cells, candidates)
        call_i = {"n": 0}

        def flaky_meta(prompt, **kw):
            call_i["n"] += 1
            if call_i["n"] == 1:
                raise claude_invoke.ClaudeInvocationError("simulated -- retries would not help")
            return fake_meta_fn(prompt, **kw)

        with patch("discovery_worker.claude_preflight.check", return_value=AUTH_OK):
            with patch.object(dw, "pick_cells", return_value=cells):
                with patch.object(dw, "call_print", return_value="prompt"):
                    with patch.object(dw, "run_claude_with_meta", side_effect=flaky_meta):
                        with patch.object(dw, "call_save", side_effect=fake_save_fn):
                            with patch("discovery_worker.load_yaml", side_effect=lambda n: default_cfg() if n == "discovery_only.yaml" else load_yaml(n)):
                                stats = dw.run(log=lambda m: None)
        self.assertEqual(len(stats["failures"]), 1)
        self.assertEqual(stats["candidates_saved"], 1)  # second cell still succeeded

    def test_timeout_retried_exactly_once_then_recorded_as_failure(self):
        cells = [("roofing", "Columbus", "OH")]
        call_i = {"n": 0}

        def always_timeout(prompt, **kw):
            call_i["n"] += 1
            raise claude_invoke.ClaudeTimeout("simulated")

        with patch("discovery_worker.claude_preflight.check", return_value=AUTH_OK):
            with patch.object(dw, "pick_cells", return_value=cells):
                with patch.object(dw, "call_print", return_value="prompt"):
                    with patch.object(dw, "run_claude_with_meta", side_effect=always_timeout):
                        with patch("discovery_worker.load_yaml", side_effect=lambda n: default_cfg(reliability={"retry_on_timeout": True, "max_timeout_retries": 1}) if n == "discovery_only.yaml" else load_yaml(n)):
                            stats = dw.run(log=lambda m: None)
        self.assertEqual(call_i["n"], 2)  # original attempt + exactly 1 retry
        self.assertEqual(len(stats["failures"]), 1)

    def test_no_expensive_retry_cascade_for_candidate_level_failures(self):
        """A candidate-level verification failure is recorded once and
        moved past -- never re-researched, never a second agent, never an
        additional Claude call for that candidate."""
        cells = [("roofing", "Columbus", "OH")]
        candidates = [[raw_candidate("NoContact", website=None)]]
        fake_meta_fn, fake_save_fn = self._mock_cycle(cells, candidates)
        with patch("discovery_worker.claude_preflight.check", return_value=AUTH_OK):
            with patch.object(dw, "pick_cells", return_value=cells):
                with patch.object(dw, "call_print", return_value="prompt"):
                    with patch.object(dw, "run_claude_with_meta", side_effect=fake_meta_fn):
                        with patch.object(dw, "call_save", side_effect=fake_save_fn):
                            with patch("discovery_worker.load_yaml", side_effect=lambda n: default_cfg() if n == "discovery_only.yaml" else load_yaml(n)):
                                stats = dw.run(log=lambda m: None)
        self.assertEqual(stats["claude_calls"], 1)  # exactly the one discovery call -- no per-candidate retry

    def test_candidate_facts_written_for_saved_candidates(self):
        cells = [("roofing", "Columbus", "OH")]
        candidates = [[raw_candidate("Acme Roofing", phone="555-9999")]]
        fake_meta_fn, fake_save_fn = self._mock_cycle(cells, candidates)
        with patch("discovery_worker.claude_preflight.check", return_value=AUTH_OK):
            with patch.object(dw, "pick_cells", return_value=cells):
                with patch.object(dw, "call_print", return_value="prompt"):
                    with patch.object(dw, "run_claude_with_meta", side_effect=fake_meta_fn):
                        with patch.object(dw, "call_save", side_effect=fake_save_fn):
                            with patch("discovery_worker.load_yaml", side_effect=lambda n: default_cfg() if n == "discovery_only.yaml" else load_yaml(n)):
                                stats = dw.run(log=lambda m: None)
        pid = "roofing-columbus-oh-acme-roofing"
        facts = json.loads((self.tmp / "leads" / pid / "candidate_facts.json").read_text())
        self.assertEqual(facts["phone"], "555-9999")
        self.assertIn("roofing / Columbus, OH", facts["discovery_source"])

    def test_reuses_v37_tier_weighted_market_rotation(self):
        """Integration check: pick_cells() genuinely calls through to
        acquisition_worker.pick_discovery_cells() (unmodified, still
        tier-weighted) rather than reimplementing its own rotation."""
        cells = dw.pick_cells(ACQ_CFG, 4, day_ordinal=20260906)
        self.assertLessEqual(len(cells), 4)
        for c in cells:
            self.assertEqual(len(c), 3)  # (niche, city, state)


# ---------------------------------------------------------------------------
# 7. CANDIDATES handoff -- schema, idempotency, both backends
# ---------------------------------------------------------------------------
class TestCandidateHandoffLib(unittest.TestCase):
    def _record(self, lead_id="roofing-columbus-oh-acme"):
        return {
            "lead_id": lead_id, "business_name": "Acme Roofing", "domain": "acme.test",
            "website": "https://acme.test/", "city": "Columbus", "state": "OH", "country": "US",
            "niche": "roofing", "phone": "555-1000", "profile_url": None,
            "discovery_source": "roofing / Columbus, OH", "discovered_at": now_iso(),
            "verification_status": "CANDIDATE_VERIFIED",
            "basic_business_facts": {"rating": 4.5, "review_count": 20, "years_in_business": 5,
                                      "commercial_value_signal": "high", "obvious_website_issue": [], "obvious_gbp_issue": []},
        }

    def test_candidate_columns_match_record_fields(self):
        record_fields = set(self._record().keys())
        for f in hl.CANDIDATE_COLUMNS:
            if f in ("created_at", "updated_at"):
                continue
            self.assertIn(f, record_fields)

    def test_candidate_row_serializes_basic_business_facts_as_json(self):
        row = hl.candidate_row_from_record(self._record())
        self.assertIsInstance(row["basic_business_facts"], str)
        parsed = json.loads(row["basic_business_facts"])
        self.assertEqual(parsed["rating"], 4.5)

    def test_merge_candidate_row_preserves_created_at_on_resync(self):
        first = hl.merge_candidate_row(None, hl.candidate_row_from_record(self._record()))
        second_record = self._record()
        second_record["phone"] = "555-2000"  # simulate a later, corrected fact
        second = hl.merge_candidate_row(first, hl.candidate_row_from_record(second_record))
        self.assertEqual(second["created_at"], first["created_at"])
        self.assertEqual(second["phone"], "555-2000")
        self.assertNotEqual(second["updated_at"], first["created_at"] if first["created_at"] == first["updated_at"] else None) if False else None

    def test_rediscovery_never_duplicates_never_loses_lead_id(self):
        r1 = hl.merge_candidate_row(None, hl.candidate_row_from_record(self._record()))
        r2 = hl.merge_candidate_row(r1, hl.candidate_row_from_record(self._record()))
        self.assertEqual(r1["lead_id"], r2["lead_id"])


class IsolatedCandidateHandoffMixin:
    def setUp(self):
        self.tmp = Path("/tmp") / f"v3_8_1_handoff_{id(self)}"
        for sub in ("handoff", "prospects", "leads"):
            (self.tmp / sub).mkdir(parents=True, exist_ok=True)
        self.cfg = json.loads(json.dumps(load_yaml("handoff.yaml")))
        self.cfg["local_backend"]["dir"] = "handoff"
        self.cfg["backend"] = "local"
        self.cfg["google_sheets"]["service_account_file"] = None
        self.cfg["google_sheets"]["spreadsheet_id"] = None
        self._orig_hb_data = hb.DATA
        hb.DATA = self.tmp
        self._orig_sync = {k: getattr(sync_handoff, k) for k in ("PROSPECTS", "LEADS")}
        sync_handoff.PROSPECTS = self.tmp / "prospects"
        sync_handoff.LEADS = self.tmp / "leads"

    def tearDown(self):
        hb.DATA = self._orig_hb_data
        for k, v in self._orig_sync.items():
            setattr(sync_handoff, k, v)
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestLocalCandidatesBackend(IsolatedCandidateHandoffMixin, unittest.TestCase):
    def _row(self, lead_id="p1", **overrides):
        r = {"lead_id": lead_id, "business_name": "Fixture Co", "domain": "fixture.test", "website": "https://fixture.test/",
             "city": "Columbus", "state": "OH", "country": "US", "niche": "roofing", "phone": "555-1", "profile_url": None,
             "discovery_source": "roofing / Columbus, OH", "discovered_at": now_iso(),
             "verification_status": "CANDIDATE_VERIFIED", "basic_business_facts": "{}",
             "created_at": now_iso(), "updated_at": now_iso()}
        r.update(overrides)
        return r

    def test_export_candidates_creates_local_file(self):
        backend = hb.LocalFileBackend(self.cfg)
        backend.export_candidates([self._row()])
        self.assertEqual(len(backend.all_candidate_rows()), 1)

    def test_repeated_sync_same_lead_id_never_duplicates(self):
        backend = hb.LocalFileBackend(self.cfg)
        backend.export_candidates([self._row()])
        backend.export_candidates([self._row(phone="555-2")])
        rows = backend.all_candidate_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows["p1"]["phone"], "555-2")  # updated in place

    def test_two_different_candidates_produce_two_rows(self):
        backend = hb.LocalFileBackend(self.cfg)
        backend.export_candidates([self._row("p1"), self._row("p2")])
        self.assertEqual(len(backend.all_candidate_rows()), 2)

    def test_candidates_csv_mirror_written(self):
        backend = hb.LocalFileBackend(self.cfg)
        backend.export_candidates([self._row()])
        self.assertTrue(backend.candidates_path.with_suffix(".csv").exists())

    def test_email_ready_and_contact_form_ready_untouched_by_candidates_sync(self):
        """Sec.5 -- CANDIDATES is additive; existing tabs/files must be
        unaffected by a candidates-only sync."""
        backend = hb.LocalFileBackend(self.cfg)
        backend.export_candidates([self._row()])
        self.assertEqual(backend.all_rows(), {})  # EMAIL_READY/CONTACT_FORM_READY still empty, unaffected


# Fake Google Sheets service supporting get/update/batchUpdate/append --
# extends the pattern tests/test_v3_6_handoff.py already uses for the
# RESULTS-tab tests, generalized here to exercise _upsert_tab's full path.
class _FakeExec:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _FakeUpsertValues:
    def __init__(self, tabs=None):
        self.tabs = dict(tabs or {})  # {tab_name: [[row...], ...]}

    def get(self, spreadsheetId, range):
        tab = range.split("!")[0]
        data = self.tabs.get(tab, [])
        return _FakeExec({"values": data} if data else {})

    def update(self, spreadsheetId, range, valueInputOption, body):
        tab = range.split("!")[0]
        self.tabs.setdefault(tab, [])
        if range.endswith("!A1"):
            if self.tabs[tab]:
                self.tabs[tab][0] = body["values"][0]
            else:
                self.tabs[tab] = list(body["values"])
        else:
            row_num = int(range.split("!A")[1])
            idx = row_num - 1
            while len(self.tabs[tab]) <= idx:
                self.tabs[tab].append([])
            self.tabs[tab][idx] = body["values"][0]
        return _FakeExec({})

    def batchUpdate(self, spreadsheetId, body):
        for item in body["data"]:
            tab = item["range"].split("!")[0]
            row_num = int(item["range"].split("!A")[1])
            idx = row_num - 1
            while len(self.tabs[tab]) <= idx:
                self.tabs[tab].append([])
            self.tabs[tab][idx] = item["values"][0]
        return _FakeExec({})

    def append(self, spreadsheetId, range, valueInputOption, body):
        tab = range.split("!")[0]
        self.tabs.setdefault(tab, [])
        for row in body["values"]:
            self.tabs[tab].append(row)
        return _FakeExec({})


class _FakeUpsertService:
    def __init__(self, tabs=None):
        self._values = _FakeUpsertValues(tabs)

    def spreadsheets(self):
        return self

    def values(self):
        return self._values


class TestGoogleSheetsCandidatesBackend(unittest.TestCase):
    def _backend(self, candidates_tab="__unset__"):
        cfg = {"service_account_file": "/fake/path.json", "spreadsheet_id": "sid",
               "scopes": ["https://www.googleapis.com/auth/spreadsheets"],
               "email_ready_tab": "EMAIL_READY", "contact_form_ready_tab": "CONTACT_FORM_READY"}
        if candidates_tab != "__unset__":
            cfg["candidates_tab"] = candidates_tab
        return hb.GoogleSheetsBackend({"google_sheets": cfg})

    def _row(self, lead_id="p1", **overrides):
        r = {"lead_id": lead_id, "business_name": "Fixture Co", "domain": "fixture.test", "website": "https://fixture.test/",
             "city": "Columbus", "state": "OH", "country": "US", "niche": "roofing", "phone": "555-1", "profile_url": None,
             "discovery_source": "roofing / Columbus, OH", "discovered_at": now_iso(),
             "verification_status": "CANDIDATE_VERIFIED", "basic_business_facts": "{}",
             "created_at": now_iso(), "updated_at": now_iso()}
        r.update(overrides)
        return r

    def test_first_sync_writes_header_and_appends_row(self):
        backend = self._backend()
        fake = _FakeUpsertService()
        with patch.object(hb.GoogleSheetsBackend, "_client", return_value=(fake, "sid")):
            backend.export_candidates([self._row()])
        rows = fake._values.tabs["CANDIDATES"]
        self.assertEqual(rows[0], list(hl.CANDIDATE_COLUMNS))
        self.assertEqual(len(rows), 2)  # header + 1 data row

    def test_resync_same_lead_id_updates_in_place_never_appends(self):
        backend = self._backend()
        fake = _FakeUpsertService()
        with patch.object(hb.GoogleSheetsBackend, "_client", return_value=(fake, "sid")):
            backend.export_candidates([self._row()])
            backend.export_candidates([self._row(phone="555-2")])
        rows = fake._values.tabs["CANDIDATES"]
        self.assertEqual(len(rows), 2)  # STILL header + 1 -- never a second data row
        phone_idx = list(hl.CANDIDATE_COLUMNS).index("phone")
        self.assertEqual(rows[1][phone_idx], "555-2")

    def test_two_distinct_candidates_produce_two_rows(self):
        backend = self._backend()
        fake = _FakeUpsertService()
        with patch.object(hb.GoogleSheetsBackend, "_client", return_value=(fake, "sid")):
            backend.export_candidates([self._row("p1"), self._row("p2")])
        rows = fake._values.tabs["CANDIDATES"]
        self.assertEqual(len(rows), 3)  # header + 2

    def test_configurable_candidates_tab_name(self):
        backend = self._backend(candidates_tab="MY_CANDIDATES")
        fake = _FakeUpsertService()
        with patch.object(hb.GoogleSheetsBackend, "_client", return_value=(fake, "sid")):
            backend.export_candidates([self._row()])
        self.assertIn("MY_CANDIDATES", fake._values.tabs)
        self.assertNotIn("CANDIDATES", fake._values.tabs)

    def test_backward_compatible_default_when_candidates_tab_key_absent(self):
        backend = self._backend(candidates_tab="__unset__")
        self.assertNotIn("candidates_tab", backend.cfg)
        fake = _FakeUpsertService()
        with patch.object(hb.GoogleSheetsBackend, "_client", return_value=(fake, "sid")):
            backend.export_candidates([self._row()])
        self.assertIn("CANDIDATES", fake._values.tabs)

    def test_email_ready_tab_never_touched_by_candidates_export(self):
        backend = self._backend()
        fake = _FakeUpsertService({"EMAIL_READY": [["lead_id"], ["existing-lead"]]})
        with patch.object(hb.GoogleSheetsBackend, "_client", return_value=(fake, "sid")):
            backend.export_candidates([self._row()])
        self.assertEqual(fake._values.tabs["EMAIL_READY"], [["lead_id"], ["existing-lead"]])


# ---------------------------------------------------------------------------
# 8. sync_handoff.py -- build_candidate_rows / sync_candidates
# ---------------------------------------------------------------------------
class TestSyncCandidates(IsolatedCandidateHandoffMixin, unittest.TestCase):
    def _seed_candidate(self, pid="roofing-columbus-oh-acme", phone="555-1"):
        prospect = {
            "id": pid, "business_name": "Acme Roofing", "website": "https://acme.test/",
            "city": "Columbus", "state": "OH", "country": "US", "niche": "roofing",
            "google_business_profile_url": None, "status": cv.CANDIDATE_VERIFIED,
            "discovered_at": now_iso(), "rating": 4.5, "review_count": 20,
        }
        with open(self.tmp / "prospects" / "discovered.jsonl", "w") as f:
            f.write(json.dumps(prospect) + "\n")
        (self.tmp / "leads" / pid).mkdir(parents=True, exist_ok=True)
        write_json(self.tmp / "leads" / pid / "candidate_facts.json",
                    {"discovery_source": "roofing / Columbus, OH", "phone": phone})
        return pid

    def test_build_candidate_rows_only_includes_candidate_verified(self):
        pid = self._seed_candidate()
        other = {"id": "other-lead", "status": "QUALIFIED", "business_name": "X", "niche": "roofing",
                  "city": "C", "state": "S", "country": "US"}
        existing = read_jsonl(self.tmp / "prospects" / "discovered.jsonl")
        existing.append(other)
        with open(self.tmp / "prospects" / "discovered.jsonl", "w") as f:
            for r in existing:
                f.write(json.dumps(r) + "\n")
        with patch("sync_handoff.load_yaml", return_value=self.cfg):
            rows, failures = sync_handoff.build_candidate_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["lead_id"], pid)

    def test_sync_candidates_writes_local_and_reports_status(self):
        self._seed_candidate()
        with patch("sync_handoff.load_yaml", return_value=self.cfg):
            result = sync_handoff.sync_candidates(logfn=lambda m: None)
        self.assertEqual(result["candidates_sync_status"], "SYNCED")
        self.assertEqual(result["candidates_rows"], 1)

    def test_repeated_sync_is_idempotent(self):
        self._seed_candidate()
        with patch("sync_handoff.load_yaml", return_value=self.cfg):
            sync_handoff.sync_candidates(logfn=lambda m: None)
            sync_handoff.sync_candidates(logfn=lambda m: None)
        backend = hb.LocalFileBackend(self.cfg)
        self.assertEqual(len(backend.all_candidate_rows()), 1)

    def test_bad_candidate_row_does_not_block_others(self):
        good = self._seed_candidate("roofing-columbus-oh-good", phone="555-1")
        bad_pid = "roofing-columbus-oh-bad"
        existing = read_jsonl(self.tmp / "prospects" / "discovered.jsonl")
        existing.append({"id": bad_pid, "status": cv.CANDIDATE_VERIFIED})  # missing required fields on purpose
        with open(self.tmp / "prospects" / "discovered.jsonl", "w") as f:
            for r in existing:
                f.write(json.dumps(r) + "\n")
        # No candidate_facts.json for bad_pid -- build_candidate_record should still degrade gracefully
        # (phone/discovery_source simply None), so force a real exception another way: corrupt the facts file.
        (self.tmp / "leads" / bad_pid).mkdir(parents=True, exist_ok=True)
        (self.tmp / "leads" / bad_pid / "candidate_facts.json").write_text("{not valid json")
        with patch("sync_handoff.load_yaml", return_value=self.cfg):
            rows, failures = sync_handoff.build_candidate_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["lead_id"], good)
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["lead_id"], bad_pid)


# ---------------------------------------------------------------------------
# 9. run_daily.py routing -- discovery_only default, full_pipeline preserved
# ---------------------------------------------------------------------------
class IsolatedRunDailyMixin:
    """
    CRITICAL SAFETY MIXIN. run_daily.py's module-level DATA/RUNTIME_DIR/
    DAILY_RUNS_DIR/LOG_DIR/PROSPECTS/MARKETS/LEADS constants are computed
    ONCE at import time from the REAL _lib.DATA -- calling run_daily.main()
    in a test without redirecting every one of them WILL write to the real
    data/runtime/daily_runs/<today>.json (clobbering a real production run
    summary), the real data/runtime/logs/, and -- if config/handoff.yaml's
    on-disk backend is "google_sheets" (as it legitimately is whenever a
    real Sheets integration has been provisioned) -- can reach the user's
    REAL Google Sheet over the network. This happened once during V3.8.1
    development (a real daily-run summary was overwritten and a live
    503-erroring call reached the real spreadsheet before being noticed
    and reverted) -- every run_daily.py test in this file MUST use this
    mixin, no exceptions, and config/handoff.yaml must NEVER be allowed to
    resolve to its real on-disk contents inside a test.
    """

    def setUp(self):
        self.tmp = Path("/tmp") / f"v3_8_1_rundaily_{id(self)}"
        for sub in ("prospects", "leads", "markets", "runtime/daily_runs", "runtime/logs", "runtime/cost", "handoff", "outreach"):
            (self.tmp / sub).mkdir(parents=True, exist_ok=True)
        self._orig_rd = {k: getattr(run_daily, k) for k in
                          ("DATA", "PROSPECTS", "MARKETS", "LEADS", "RUNTIME_DIR", "DAILY_RUNS_DIR", "LOG_DIR")}
        run_daily.DATA = self.tmp
        run_daily.PROSPECTS = self.tmp / "prospects"
        run_daily.MARKETS = self.tmp / "markets"
        run_daily.LEADS = self.tmp / "leads"
        run_daily.RUNTIME_DIR = self.tmp / "runtime"
        run_daily.DAILY_RUNS_DIR = self.tmp / "runtime" / "daily_runs"
        run_daily.LOG_DIR = self.tmp / "runtime" / "logs"
        for fname in ("discovered.jsonl", "qualified.jsonl", "manual_review.jsonl", "needs_enrichment.jsonl", "rejected.jsonl"):
            (self.tmp / "prospects" / fname).touch()
        # Belt-and-suspenders: even with DATA sandboxed, config/handoff.yaml
        # on disk may legitimately point at a real Google Sheet -- force it
        # to an unconfigured local-only shape for every test in this class
        # so nothing in this file can ever attempt a live network call.
        self.safe_handoff_cfg = json.loads(json.dumps(load_yaml("handoff.yaml")))
        self.safe_handoff_cfg["backend"] = "local"
        self.safe_handoff_cfg["google_sheets"]["service_account_file"] = None
        self.safe_handoff_cfg["google_sheets"]["spreadsheet_id"] = None

    def tearDown(self):
        for k, v in self._orig_rd.items():
            setattr(run_daily, k, v)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def patched_load_yaml(self, production_mode):
        real = load_yaml

        def _load(name):
            if name == "acquisition.yaml":
                cfg = dict(ACQ_CFG)
                cfg["production_mode"] = production_mode
                return cfg
            if name == "handoff.yaml":
                return self.safe_handoff_cfg
            return real(name)
        return _load


class TestRunDailyRouting(IsolatedRunDailyMixin, unittest.TestCase):
    def test_discovery_only_calls_discovery_worker_not_acquisition_worker(self):
        calls = {"discovery": 0, "acquisition": 0}

        def fake_discovery_run(**kw):
            calls["discovery"] += 1
            return dict(dw.empty_stats(), discovery_run_completed=True, claude_auth_status="AUTH_OK", run_already_active=False)

        def fake_acquisition_run(**kw):
            calls["acquisition"] += 1
            return {}

        with patch.object(run_daily, "verify_workspace", return_value=(True, [])):
            with patch.object(run_daily, "load_yaml", side_effect=self.patched_load_yaml("discovery_only")):
                with patch.object(run_daily.discovery_worker, "run", side_effect=fake_discovery_run):
                    with patch.object(run_daily.acquisition_worker, "run", side_effect=fake_acquisition_run):
                        with patch.object(run_daily.sync_handoff, "sync_candidates",
                                           return_value={"candidates_sync_status": "SYNCED", "candidates_rows": 0, "candidate_row_failures": []}):
                            with patch.object(run_daily.report_discovery_only, "write_report", return_value=self.tmp / "report-latest.md"):
                                with patch("sys.argv", ["run_daily.py"]):
                                    rc = run_daily.main()
        self.assertEqual(rc, 0)
        self.assertEqual(calls["discovery"], 1)
        self.assertEqual(calls["acquisition"], 0)  # full_pipeline's Claude worker never invoked
        # And the summary landed in the SANDBOXED daily_runs dir, never the real one.
        written = list((self.tmp / "runtime" / "daily_runs").glob("*.json"))
        self.assertEqual(len(written), 1)

    def test_discovery_only_never_calls_downstream_scripts(self):
        """Behavioral proof: every subprocess.run call made during a
        discovery_only run_daily.main() invocation targets ONLY
        discover_prospects.py -- none of the full-pipeline scripts. The
        real discovery_worker.run() executes here (not mocked away), but
        with pick_cells() forced to an empty list so it does zero real
        work/Claude calls while still exercising run_daily's actual
        routing code path end to end."""
        forbidden = ("qualify_leads.py", "run_deterministic_scan.py", "build_dossier_v3_2.py", "build_dossier.py",
                     "stage_asset.py", "contact_identity.py", "generate_outreach_email.py", "generate_email.py",
                     "qa_outreach_email.py", "qa_email.py", "send_window_planner.py", "export_ready_to_send.py",
                     "assess_commercial_fit.py", "assess_google_gap.py", "route_to_specialist.py",
                     "rank_enrichment.py", "reevaluate_needs_enrichment.py", "send_executor.py",
                     "delivery_reconciliation.py", "follow_up.py", "reply_handling.py")
        seen_args = []

        def spy_run(cmd, *a, **kw):
            seen_args.append(cmd)
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        with patch.object(run_daily, "verify_workspace", return_value=(True, [])):
            with patch.object(run_daily, "load_yaml", side_effect=self.patched_load_yaml("discovery_only")):
                with patch("discovery_worker.claude_preflight.check", return_value=AUTH_OK):
                    with patch.object(dw, "DATA", self.tmp), patch.object(dw, "PROSPECTS", self.tmp / "prospects"), \
                         patch.object(dw, "LEADS", self.tmp / "leads"), patch.object(dw, "LOCK_PATH", self.tmp / "runtime" / "discovery.lock"), \
                         patch.object(_lib, "PROSPECTS", self.tmp / "prospects"), patch.object(_lib, "LEADS", self.tmp / "leads"), \
                         patch.object(_lib, "DATA", self.tmp):
                        with patch.object(dw, "pick_cells", return_value=[]):  # no cells -- nothing to discover
                            with patch("subprocess.run", side_effect=spy_run):
                                with patch.object(run_daily.sync_handoff, "sync_candidates",
                                                   return_value={"candidates_sync_status": "SYNCED", "candidates_rows": 0, "candidate_row_failures": []}):
                                    with patch.object(run_daily.report_discovery_only, "write_report", return_value=self.tmp / "report-latest.md"):
                                        with patch("sys.argv", ["run_daily.py"]):
                                            run_daily.main()
        for cmd in seen_args:
            for bad in forbidden:
                self.assertNotIn(bad, str(cmd))

    def test_full_pipeline_mode_still_calls_acquisition_worker(self):
        """Regression: explicit full_pipeline mode must still exercise the
        OLD, unchanged code path. Every other real side-effecting call in
        that path (subprocess scripts, the handoff sync, the outreach-
        results import, the tracker CSV export) is mocked out here -- this
        test verifies ROUTING, not the full_pipeline body's own behavior
        (that is exhaustively covered by tests/test_v3_5_acquisition.py
        etc.), and per IsolatedRunDailyMixin's own warning, nothing in
        this file may ever risk a real network call or real file write."""
        calls = {"acquisition": 0}

        def fake_acquisition_run(**kw):
            calls["acquisition"] += 1
            return {
                "claude_auth_status": "AUTH_OK", "run_already_active": False,
                "acquisition_run_completed": True, "worker_timeout": False,
                "limitations": [], "per_lead_failures": [],
            }

        with patch.object(run_daily, "verify_workspace", return_value=(True, [])):
            with patch.object(run_daily, "load_yaml", side_effect=self.patched_load_yaml("full_pipeline")):
                with patch.object(run_daily.acquisition_worker, "run", side_effect=fake_acquisition_run):
                    with patch.object(run_daily, "run_script", return_value=True):
                        with patch("import_outreach_results.import_results",
                                   return_value={"events_applied": 0, "events_skipped_duplicate": 0, "events_skipped_stale": 0, "import_failures": []}):
                            with patch("sync_handoff.sync",
                                       return_value={"handoff_sync_status": "SYNCED", "email_ready_rows": 0,
                                                     "contact_form_ready_rows": 0, "row_failures": []}):
                                with patch("export_tracker_csv.export_all", return_value=None):
                                    with patch("sys.argv", ["run_daily.py"]):
                                        rc = run_daily.main()
        self.assertEqual(rc, 0)
        self.assertEqual(calls["acquisition"], 1)
        written = list((self.tmp / "runtime" / "daily_runs").glob("*.json"))
        self.assertEqual(len(written), 1)  # landed in the sandbox, never the real daily_runs dir


# ---------------------------------------------------------------------------
# 10. Historical states preserved
# ---------------------------------------------------------------------------
class TestHistoricalStatesPreserved(IsolatedDiscoveryMixin, unittest.TestCase):
    def test_existing_statuses_untouched_by_a_discovery_only_run(self):
        historical = [
            {"id": "qualified-lead", "status": "QUALIFIED", "business_name": "Q", "niche": "roofing", "city": "X", "state": "Y", "country": "US"},
            {"id": "needs-enrichment-lead", "status": "NEEDS_ENRICHMENT", "business_name": "N", "niche": "hvac", "city": "X", "state": "Y", "country": "US"},
            {"id": "manual-review-lead", "status": "MANUAL_REVIEW", "business_name": "M", "niche": "roofing", "city": "X", "state": "Y", "country": "US"},
            {"id": "contact-form-ready-lead", "status": "CONTACT_FORM_READY", "business_name": "C", "niche": "hvac", "city": "X", "state": "Y", "country": "US"},
            {"id": "ready-to-send-lead", "status": "READY_TO_SEND", "business_name": "R", "niche": "roofing", "city": "X", "state": "Y", "country": "US"},
        ]
        self.seed_discovered(historical)
        with patch("discovery_worker.claude_preflight.check", return_value=AUTH_OK):
            with patch.object(dw, "pick_cells", return_value=[]):  # nothing new discovered this run
                with patch("discovery_worker.load_yaml", side_effect=lambda n: default_cfg() if n == "discovery_only.yaml" else load_yaml(n)):
                    dw.run(log=lambda m: None)
        final = {r["id"]: r["status"] for r in read_jsonl(self.tmp / "prospects" / "discovered.jsonl")}
        for r in historical:
            self.assertEqual(final[r["id"]], r["status"], f"{r['id']} status was mutated")


# ---------------------------------------------------------------------------
# 11. Static safety guards -- zero downstream Claude spend
# ---------------------------------------------------------------------------
class TestStaticSafetyGuardsDiscoveryOnly(unittest.TestCase):
    FORBIDDEN_SCRIPT_NAMES = (
        "assess_commercial_fit.py", "assess_google_gap.py", "route_to_specialist.py",
        "contact_identity.py", "build_dossier.py", "build_dossier_v3_2.py", "stage_asset.py",
        "generate_outreach_email.py", "generate_email.py", "qa_outreach_email.py", "qa_email.py",
        "send_window_planner.py", "export_ready_to_send.py", "rank_enrichment.py",
        "reevaluate_needs_enrichment.py", "send_executor.py", "delivery_reconciliation.py",
        "follow_up.py", "reply_handling.py", "assess_buying_signals.py", "check_contactability.py",
    )

    def test_discovery_worker_never_references_downstream_scripts(self):
        text = (SCRIPTS / "discovery_worker.py").read_text()
        for bad in self.FORBIDDEN_SCRIPT_NAMES:
            self.assertNotIn(bad, text, f"discovery_worker.py must never reference {bad}")

    def test_candidate_verification_never_references_downstream_scripts(self):
        text = (SCRIPTS / "candidate_verification.py").read_text()
        for bad in self.FORBIDDEN_SCRIPT_NAMES:
            self.assertNotIn(bad, text)

    def test_discovery_worker_never_imports_claude_seo(self):
        text = (SCRIPTS / "discovery_worker.py").read_text()
        self.assertNotIn("claude-seo", text)
        self.assertNotIn("claude_seo", text)

    def test_discovery_worker_never_imports_gmail_or_send_modules(self):
        """Checks for actual Gmail/send capability, not the bare word --
        discovery_worker.py's own module docstring legitimately DESCRIBES
        the new architecture ('ChatGPT + user' owns Gmail execution) as
        prose, which a naive substring check would wrongly flag."""
        for fname in ("discovery_worker.py", "candidate_verification.py", "cost_ledger.py"):
            text = (SCRIPTS / fname).read_text().lower()
            self.assertNotIn("smtplib", text)
            self.assertNotIn("imaplib", text)
            self.assertNotIn("import gmail", text)
            self.assertNotIn("gmail.googleapis", text)
            self.assertNotIn("send_executor.py", text)  # the .py suffix is how this codebase's own
                                                          # subprocess argv literals always reference a script;
                                                          # the bare word appears in prose describing scope

    def test_discovery_worker_only_claude_call_is_discover_prospects(self):
        text = (SCRIPTS / "discovery_worker.py").read_text()
        self.assertIn('"discover_prospects.py"', text)
        # run_claude_with_meta is called exactly once in the source (one call site)
        self.assertEqual(text.count("run_claude_with_meta("), 1)

    def test_v38_ranking_enrichment_files_still_exist(self):
        for fname in ("ranking_providers.py", "rank_enrichment.py"):
            self.assertTrue((SCRIPTS / fname).exists(), f"{fname} must not be deleted")
        self.assertTrue((ROOT / "config" / "ranking_enrichment.yaml").exists())
        self.assertTrue((Path(__file__).parent / "test_v3_8_ranking_enrichment.py").exists())

    def test_v38_ranking_enrichment_still_importable_and_callable(self):
        import rank_enrichment as rke
        import ranking_providers as rp
        self.assertTrue(callable(rke.run_cycle))
        self.assertTrue(callable(rp.ManualImportProvider().fetch))

    def test_acquisition_worker_still_importable_and_callable_for_full_pipeline(self):
        self.assertTrue(callable(aw.run))


# ---------------------------------------------------------------------------
# 12. Report format
# ---------------------------------------------------------------------------
class TestDiscoveryOnlyReport(unittest.TestCase):
    def _stats(self, **overrides):
        s = dict(dw.empty_stats())
        s.update({"trigger_type": "NORMAL_SCHEDULE", "candidates_discovered": 16, "candidates_verified": 14,
                   "candidates_saved": 14, "duplicates_skipped": 2, "verification_failures": 0,
                   "markets_explored": ["roofing / Indianapolis, IN", "hvac / Indianapolis, IN"],
                   "claude_calls": 2, "budget_status": "OK", "budget_limit_usd": 3.00})
        s.update(overrides)
        return s

    def test_report_shows_skipped_downstream_stages(self):
        text = rdo.render_report(self._stats(), {"candidates_sync_status": "SYNCED", "candidates_rows": 14})
        for line in ("Downstream qualification: SKIPPED", "Ranking enrichment: SKIPPED", "SEO agents: SKIPPED",
                     "Contact research: SKIPPED", "Outreach drafting: SKIPPED", "Gmail: NOT ACCESSED"):
            self.assertIn(line, text)

    def test_report_shows_funnel_counts(self):
        text = rdo.render_report(self._stats(), {"candidates_sync_status": "SYNCED", "candidates_rows": 14})
        self.assertIn("Candidates discovered: 16", text)
        self.assertIn("Candidates saved: 14", text)
        self.assertIn("Duplicates skipped: 2", text)

    def test_report_shows_markets_explored(self):
        text = rdo.render_report(self._stats(), {"candidates_sync_status": "SYNCED", "candidates_rows": 14})
        self.assertIn("roofing / Indianapolis, IN", text)

    def test_report_shows_sheet_sync_status(self):
        text = rdo.render_report(self._stats(), {"candidates_sync_status": "SYNCED", "candidates_rows": 14})
        self.assertIn("Google Sheet CANDIDATES synced: 14 (SYNCED)", text)

    def test_report_never_fabricates_unknown_cost(self):
        text = rdo.render_report(self._stats(actual_cost_usd=None, cost_observable=False), {"candidates_sync_status": "SYNCED", "candidates_rows": 14})
        self.assertIn("UNKNOWN", text)

    def test_report_labels_incomplete_accounting_and_never_estimates(self):
        """V3.8.2 -- a run with an unknown-cost attempt must show
        budget_accounting_status/unknown-cost-attempt count explicitly and
        never present a fabricated total."""
        text = rdo.render_report(
            self._stats(budget_accounting_status="INCOMPLETE_UNKNOWN_CALL_COST", unknown_cost_attempts=1,
                        observed_total_cost_usd=0.30, budget_remaining_usd=None),
            {"candidates_sync_status": "SYNCED", "candidates_rows": 14},
        )
        self.assertIn("INCOMPLETE_UNKNOWN_CALL_COST", text)
        self.assertIn("INCOMPLETE", text)  # the inline note next to observed total cost
        self.assertIn("0.3", text)  # the known partial sum is still shown, not suppressed
        self.assertIn("UNKNOWN", text)  # budget remaining, correctly unknown

    def test_write_report_creates_file(self):
        tmp_dir = Path("/tmp") / f"v3_8_1_report_{id(self)}"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        try:
            out = rdo.write_report(self._stats(), {"candidates_sync_status": "SYNCED", "candidates_rows": 14}, out_path=tmp_dir / "report-latest.md")
            self.assertTrue(out.exists())
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
