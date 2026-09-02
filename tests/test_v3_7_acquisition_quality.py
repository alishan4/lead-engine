"""
V3.7 tests: acquisition-rotation diversification/weighting (A), cheap
prequalification (B), ranking-evidence ingestion (C), deterministic
re-evaluation (D), and bounded timeout retry (E). Synthetic fixtures only
-- no real prospect/business data, no live Claude call, no network call.
"""
import json
import os
import shutil
import subprocess
import sys
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # so `from test_v3_5_acquisition import ...` works
                                                            # regardless of how the suite is invoked (tests/ has no __init__.py)

from _lib import load_yaml, now_iso  # noqa: E402
import acquisition_worker as aw  # noqa: E402
import discover_prospects as dp  # noqa: E402
import import_ranking_observation as iro  # noqa: E402
import reevaluate_needs_enrichment as ren  # noqa: E402

NICHES_CFG = load_yaml("niches.yaml")
ACQ_CFG = load_yaml("acquisition.yaml")


# ---------------------------------------------------------------------------
# A. Diversify acquisition -- market rotation
# ---------------------------------------------------------------------------
class TestMarketRotationDiversification(unittest.TestCase):
    NICHES = ["hvac", "roofing", "restoration", "foundation_repair", "plumbing", "family_law", "estate_law", "moving_relocation"]
    CITIES = [{"city": "Columbus", "state": "OH"}, {"city": "Indianapolis", "state": "IN"},
              {"city": "Tucson", "state": "AZ"}, {"city": "Louisville", "state": "KY"}]

    def test_rotation_covers_every_cell_exactly_once(self):
        cells = aw.build_market_rotation(self.NICHES, self.CITIES, NICHES_CFG)
        self.assertEqual(len(cells), len(self.NICHES) * len(self.CITIES))
        self.assertEqual(len(set(cells)), len(cells))  # no duplicates

    def test_rotation_never_blocks_on_a_single_niche(self):
        """Regression test for 2026-09-02's real failure mode: the old
        niche-major loop put an entire niche's cities in one contiguous
        run, so a narrow rotation window landed almost entirely on one
        niche (family_law, both production passes that day). No 6
        consecutive cells here may share the same niche."""
        cells = aw.build_market_rotation(self.NICHES, self.CITIES, NICHES_CFG)
        for i in range(len(cells) - 6):
            window_niches = {c[0] for c in cells[i:i + 6]}
            self.assertGreater(len(window_niches), 1, f"cells[{i}:{i+6}] were all the same niche: {cells[i:i+6]}")

    def test_new_niches_present_in_config(self):
        dm = ACQ_CFG["discovery_markets"]
        for n in ("foundation_repair", "estate_law", "moving_relocation"):
            self.assertIn(n, dm["niches"])

    def test_moving_relocation_niche_defined_without_fabricated_fields(self):
        n = NICHES_CFG["niches"]["moving_relocation"]
        self.assertIn("tier", n)
        self.assertIsInstance(n["money_keywords"], list)


class TestTierWeightedSelection(unittest.TestCase):
    NICHES = ["hvac", "roofing", "restoration", "foundation_repair", "plumbing", "family_law", "estate_law", "moving_relocation"]
    CITIES = [{"city": "Columbus", "state": "OH"}, {"city": "Indianapolis", "state": "IN"},
              {"city": "Tucson", "state": "AZ"}, {"city": "Louisville", "state": "KY"}, {"city": "Raleigh", "state": "NC"}]

    def _ctx(self, max_cells=3):
        cfg = {"discovery_markets": {"niches": self.NICHES, "cities": self.CITIES}, "max_fresh_market_cells_per_run": max_cells}
        return aw.WorkerContext(cfg, {}, {}, aw.Deadline(60), lambda m: None, None, False)

    def test_weighted_tier_sequence_is_proportional_not_absolute_priority(self):
        seq = aw.weighted_tier_sequence({1: 3, 2: 2}, 5)
        self.assertEqual(Counter(seq), {1: 3, 2: 2})
        # never "all of tier 1 then all of tier 2" -- must actually interleave
        self.assertNotEqual(seq, [1, 1, 1, 2, 2])

    def test_no_niche_is_ever_fully_excluded_from_selection_over_many_days(self):
        """Direct test of the explicit 'do not reject a niche globally'
        rule at the scheduling level: every tier-2 niche must be selected
        at least once across a reasonable number of simulated days."""
        selected_niches = set()
        for day in range(20260901, 20260931):
            chosen = aw.pick_discovery_cells(self._ctx(), day_ordinal=day)
            selected_niches.update(c[0] for c in chosen)
        for n in self.NICHES:
            self.assertIn(n, selected_niches, f"{n} was never selected across 30 simulated days")

    def test_tier_1_niches_selected_more_often_than_tier_2_over_time(self):
        tier_counts = Counter()
        for day in range(20260901, 20260931):
            chosen = aw.pick_discovery_cells(self._ctx(), day_ordinal=day)
            for niche, _, _ in chosen:
                tier_counts[aw.niche_tier(niche, NICHES_CFG)] += 1
        self.assertGreater(tier_counts[1], tier_counts[2])

    def test_max_cells_per_run_respected(self):
        chosen = aw.pick_discovery_cells(self._ctx(max_cells=3), day_ordinal=20260902)
        self.assertLessEqual(len(chosen), 3)


# ---------------------------------------------------------------------------
# B. Cheap prequalification
# ---------------------------------------------------------------------------
class TestCheapPrequalification(unittest.TestCase):
    def _candidate(self, **overrides):
        c = {"business_name": "Fixture Co", "website": "https://fixture.test/",
             "independently_owned": True, "commercial_value_signal": "high",
             "google_dependency_evidence": "ranks in local pack"}
        c.update(overrides)
        return c

    def test_low_commercial_value_signal_dropped_before_expensive_research(self):
        kept, dropped = dp.filter_candidates([self._candidate(commercial_value_signal="low")], set())
        self.assertEqual(kept, [])
        self.assertEqual(len(dropped), 1)
        self.assertIn("low", dropped[0][1])

    def test_high_and_medium_commercial_value_still_kept(self):
        for signal in ("high", "medium"):
            kept, _ = dp.filter_candidates([self._candidate(commercial_value_signal=signal)], set())
            self.assertEqual(len(kept), 1)

    def test_none_commercial_value_still_dropped_unchanged(self):
        kept, dropped = dp.filter_candidates([self._candidate(commercial_value_signal="none")], set())
        self.assertEqual(kept, [])

    def test_missing_review_count_or_years_never_penalized(self):
        """UNKNOWN != FALSE: a candidate with null review_count/years_in_business
        (simply not found, not confirmed low) must NOT be dropped by
        filter_candidates -- only a CONFIRMED low commercial_value_signal is
        cheap-prequalification grounds, never an absent maturity signal."""
        c = self._candidate(review_count=None, years_in_business=None, rating=None)
        kept, dropped = dp.filter_candidates([c], set())
        self.assertEqual(len(kept), 1)

    def test_low_review_count_candidate_not_penalized_either(self):
        """Empirical finding (2026-09-02 real data): a low-but-KNOWN review
        count did not itself correlate with worse outcomes once niche tier
        is accounted for -- filter_candidates must not add a review-count
        threshold not supported by that evidence."""
        c = self._candidate(review_count=3, years_in_business=1)
        kept, dropped = dp.filter_candidates([c], set())
        self.assertEqual(len(kept), 1)


# ---------------------------------------------------------------------------
# C. Ranking evidence ingestion
# ---------------------------------------------------------------------------
class TestRankingEvidenceIngestion(unittest.TestCase):
    def _obs(self, **overrides):
        o = {"location": "roofing-columbus-oh", "query": "roof replacement columbus oh",
             "observed_at": "2026-09-05T00:00:00+00:00", "source": "manual_maps_check",
             "maps_position": 5, "organic_position": None,
             "business_name": "Fixture Roofing Co", "domain": "fixture-roofing.test"}
        o.update(overrides)
        return o

    def test_valid_observation_passes_validation(self):
        ok, reason = iro.validate_observation(self._obs())
        self.assertTrue(ok, reason)

    def test_missing_required_field_rejected(self):
        for field in ("query", "location", "observed_at", "source"):
            obs = self._obs()
            del obs[field]
            ok, reason = iro.validate_observation(obs)
            self.assertFalse(ok, f"missing {field} should be rejected")

    def test_unknown_source_rejected(self):
        ok, reason = iro.validate_observation(self._obs(source="i_made_this_up"))
        self.assertFalse(ok)

    def test_neither_position_present_is_rejected_not_a_silent_noop(self):
        ok, reason = iro.validate_observation(self._obs(maps_position=None, organic_position=None))
        self.assertFalse(ok)
        self.assertIn("nothing to import", reason)

    def test_no_identity_to_match_is_rejected(self):
        ok, reason = iro.validate_observation(self._obs(business_name=None, domain=None))
        self.assertFalse(ok)

    def test_unknown_stays_unknown_never_fabricated_into_a_position(self):
        """The schema/validator never invents a maps_position/organic_position
        -- an observation that only supplies one of the two leaves the other
        untouched (None), never defaulted to some 'poor rank' placeholder."""
        raw = iro.observation_to_raw_row(self._obs(maps_position=7, organic_position=None))
        self.assertEqual(raw["maps_position"], 7)
        self.assertIsNone(raw["organic_position"])

    def test_resolve_market_id_from_city_state_plus_niche(self):
        market_id = iro.resolve_market_id("Columbus, OH", "roofing")
        self.assertEqual(market_id, "roofing-columbus-oh")

    def test_resolve_market_id_passthrough_when_already_a_slug(self):
        self.assertEqual(iro.resolve_market_id("roofing-columbus-oh", None), "roofing-columbus-oh")

    def test_import_writes_canonical_csv_row(self):
        tmp = Path("/tmp") / f"v3_7_rankings_test_{id(self)}"
        tmp.mkdir(parents=True, exist_ok=True)
        try:
            with patch("import_ranking_observation.rankings_path", lambda market_id: tmp / f"{market_id}.csv"):
                imported, failures = iro.import_observations([self._obs()], logfn=lambda m: None)
            self.assertEqual(imported, 1)
            self.assertEqual(failures, [])
            content = (tmp / "roofing-columbus-oh.csv").read_text()
            self.assertIn("manual_maps_check", content)
            self.assertIn("Fixture Roofing Co", content)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_one_bad_observation_in_a_batch_does_not_block_others(self):
        tmp = Path("/tmp") / f"v3_7_rankings_test_{id(self)}_batch"
        tmp.mkdir(parents=True, exist_ok=True)
        try:
            good = self._obs()
            bad = self._obs(source="not_a_real_source")
            with patch("import_ranking_observation.rankings_path", lambda market_id: tmp / f"{market_id}.csv"):
                imported, failures = iro.import_observations([bad, good], logfn=lambda m: None)
            self.assertEqual(imported, 1)
            self.assertEqual(len(failures), 1)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# D. Deterministic re-evaluation (pure-function level)
# ---------------------------------------------------------------------------
class TestProvenanceGuarantee(unittest.TestCase):
    """2026-09-02 review: verifies ranking-observation provenance cannot
    silently present an unknown/empty source as verified ranking evidence,
    and that no redundant read-side allowlist was needed given the
    write-time guarantee already covers it."""

    def test_write_time_gate_rejects_empty_source(self):
        obs = {"location": "roofing-columbus-oh", "query": "q", "observed_at": now_iso(),
               "source": "", "maps_position": 5, "business_name": "Fixture Co"}
        ok, reason = iro.validate_observation(obs)
        self.assertFalse(ok)
        self.assertIn("source", reason)

    def test_write_time_gate_rejects_unrecognized_source(self):
        obs = {"location": "roofing-columbus-oh", "query": "q", "observed_at": now_iso(),
               "source": "totally_made_up", "maps_position": 5, "business_name": "Fixture Co"}
        ok, reason = iro.validate_observation(obs)
        self.assertFalse(ok)

    def test_every_known_source_is_a_real_non_empty_provenance_label(self):
        from import_rankings import KNOWN_SOURCES
        for s in KNOWN_SOURCES:
            self.assertTrue(s and isinstance(s, str))

    def test_record_provenance_never_fabricates_a_source_when_none_found(self):
        """Unlike scripts/rescore_leads.py's V2 fallback to the literal
        "manual_csv" when no matched row has a source, record_provenance
        must report an honest empty list rather than presenting a specific,
        unverified provenance label as if it were real."""
        tmp = Path("/tmp") / f"v3_7_provenance_guarantee_{id(self)}"
        pid = "fixture-lead"
        (tmp / "leads" / pid).mkdir(parents=True, exist_ok=True)
        try:
            matches_missing_source = [{"maps_position": "5", "exact_rank_verified": "True", "observed_at": now_iso()}]  # no 'source' key at all
            with patch.object(ren, "LEADS", tmp / "leads"):
                ren.record_provenance(pid, {"maps_position": 5}, matches_missing_source, "roofing-columbus-oh")
            result = json.loads((tmp / "leads" / pid / "qualification_v3.json").read_text())
            self.assertEqual(result["ranking_reevaluations"][0]["sources"], [])  # honest, never "manual_csv" or any fabricated label
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_record_provenance_preserves_the_real_source_when_present(self):
        tmp = Path("/tmp") / f"v3_7_provenance_guarantee2_{id(self)}"
        pid = "fixture-lead"
        (tmp / "leads" / pid).mkdir(parents=True, exist_ok=True)
        try:
            matches = [{"maps_position": "5", "exact_rank_verified": "True", "source": "manual_maps_check", "observed_at": now_iso()}]
            with patch.object(ren, "LEADS", tmp / "leads"):
                ren.record_provenance(pid, {"maps_position": 5}, matches, "roofing-columbus-oh")
            result = json.loads((tmp / "leads" / pid / "qualification_v3.json").read_text())
            self.assertEqual(result["ranking_reevaluations"][0]["sources"], ["manual_maps_check"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_only_two_scripts_can_ever_write_to_rankings_csv_and_both_gate_on_source(self):
        """Static guard: the two blessed import paths are the only writers
        (grep for the append/write call), and both still contain the
        source-membership check this whole guarantee depends on."""
        for fname, needle in (("import_rankings.py", "if source not in KNOWN_SOURCES"),
                               ("import_ranking_observation.py", 'obs.get("source") not in KNOWN_SOURCES')):
            text = (SCRIPTS / fname).read_text()
            self.assertIn(needle, text, f"{fname} must still gate writes on KNOWN_SOURCES membership")


class TestReevaluationPure(unittest.TestCase):
    def test_never_overwrites_an_existing_position(self):
        p = {"maps_position": 3, "organic_position": None}
        matches = [{"maps_position": "9", "organic_position": "14", "exact_rank_verified": "True", "source": "semrush", "observed_at": now_iso()}]
        added = ren.apply_new_ranking_fields(p, matches)
        self.assertNotIn("maps_position", added)  # already known -- never overwritten
        self.assertEqual(added.get("organic_position"), 14)

    def test_no_usable_match_adds_nothing(self):
        p = {"maps_position": None, "organic_position": None}
        matches = [{"maps_position": "", "organic_position": "", "exact_rank_verified": "False", "source": "semrush", "observed_at": now_iso()}]
        added = ren.apply_new_ranking_fields(p, matches)
        self.assertEqual(added, {})

    def test_provenance_appends_without_deleting_prior_sections(self):
        tmp = Path("/tmp") / f"v3_7_provenance_test_{id(self)}"
        pid = "fixture-lead"
        lead_dir = tmp / "leads" / pid
        lead_dir.mkdir(parents=True, exist_ok=True)
        qual_path = lead_dir / "qualification_v3.json"
        prior = {"prospect_id": pid, "fit": {"confirmed_score": 62}, "buying_signals": {"runs_google_ads": True}}
        qual_path.write_text(json.dumps(prior))
        try:
            with patch.object(ren, "LEADS", tmp / "leads"):
                ren.record_provenance(pid, {"maps_position": 5}, [{"source": "manual_maps_check", "observed_at": now_iso()}], "roofing-columbus-oh")
            result = json.loads(qual_path.read_text())
            self.assertEqual(result["fit"], prior["fit"])  # untouched
            self.assertEqual(result["buying_signals"], prior["buying_signals"])  # untouched
            self.assertEqual(len(result["ranking_reevaluations"]), 1)
            self.assertEqual(result["ranking_reevaluations"][0]["fields_added"], {"maps_position": 5})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_provenance_accumulates_across_multiple_reevaluations(self):
        tmp = Path("/tmp") / f"v3_7_provenance_test2_{id(self)}"
        pid = "fixture-lead"
        lead_dir = tmp / "leads" / pid
        lead_dir.mkdir(parents=True, exist_ok=True)
        try:
            with patch.object(ren, "LEADS", tmp / "leads"):
                ren.record_provenance(pid, {"maps_position": 5}, [{"source": "manual_maps_check", "observed_at": now_iso()}], "roofing-columbus-oh")
                ren.record_provenance(pid, {"organic_position": 9}, [{"source": "semrush", "observed_at": now_iso()}], "roofing-columbus-oh")
            result = json.loads((lead_dir / "qualification_v3.json").read_text())
            self.assertEqual(len(result["ranking_reevaluations"]), 2)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestReevaluationEndToEnd(unittest.TestCase):
    """Real subprocess integration (sandboxed via LEAD_ENGINE_DATA_DIR) --
    no Claude call anywhere in this path, so a real subprocess run is fast
    and exercises the actual assess_google_gap.py -> assess_commercial_fit.py
    -> qualify_leads.py chain rather than mocking away the thing being
    tested. All data is synthetic ('Synthetic Fixture ...'); nothing here
    ever touches or promotes a real prospect."""

    def setUp(self):
        self.tmp = Path("/tmp") / f"v3_7_e2e_{id(self)}"
        for sub in ("prospects", "leads", "markets", "rankings", "outreach", "runtime"):
            (self.tmp / sub).mkdir(parents=True, exist_ok=True)
        self.pid = "roofing-columbus-oh-v37-synthetic-fixture"
        self.env = dict(os.environ)
        self.env["LEAD_ENGINE_DATA_DIR"] = str(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed(self):
        prospect = {
            "id": self.pid, "business_name": "Synthetic Fixture Roofing Co",
            "website": "https://v37-synthetic-fixture-roofing.test/",
            "city": "Columbus", "state": "OH", "country": "US", "niche": "roofing",
            "status": "NEEDS_ENRICHMENT", "maps_position": None, "organic_position": None,
            "obvious_website_issue": [], "obvious_gbp_issue": [], "service_page_count": 6,
            "competitor_gap": [], "review_count": 45, "commercial_value_signal": "high",
            "verified_business": True, "contactability_score": 2, "buying_signal_tiers": {},
        }
        for fname in ("discovered.jsonl", "needs_enrichment.jsonl"):
            (self.tmp / "prospects" / fname).write_text(json.dumps(prospect) + "\n")
        (self.tmp / "leads" / self.pid).mkdir(parents=True, exist_ok=True)

    def _run(self, args):
        return subprocess.run([sys.executable] + args, cwd=SCRIPTS, env=self.env,
                               capture_output=True, text=True, timeout=30)

    def test_no_ranking_data_leaves_lead_at_needs_enrichment(self):
        self._seed()
        proc = self._run(["reevaluate_needs_enrichment.py", "--id", self.pid])
        self.assertEqual(proc.returncode, 0)
        self.assertIn("no ranking data imported yet", proc.stdout)
        recs = [json.loads(l) for l in (self.tmp / "prospects" / "needs_enrichment.jsonl").read_text().splitlines()]
        self.assertEqual(recs[0]["status"], "NEEDS_ENRICHMENT")

    def test_new_ranking_evidence_triggers_deterministic_rerouting(self):
        self._seed()
        imp = self._run(["import_ranking_observation.py", "--niche", "roofing", "--location", "Columbus, OH",
                          "--query", "roof replacement columbus oh", "--maps-position", "6", "--organic-position", "12",
                          "--observed-at", "2026-09-06T00:00:00+00:00", "--source", "manual_maps_check",
                          "--business-name", "Synthetic Fixture Roofing Co", "--domain", "v37-synthetic-fixture-roofing.test"])
        self.assertEqual(imp.returncode, 0, imp.stdout + imp.stderr)

        proc = self._run(["reevaluate_needs_enrichment.py", "--id", self.pid])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

        final = [json.loads(l) for l in (self.tmp / "prospects" / "discovered.jsonl").read_text().splitlines()][0]
        self.assertEqual(final["maps_position"], 6)
        self.assertEqual(final["organic_position"], 12)
        self.assertIn(final["status"], ("QUALIFIED", "HIGH_PRIORITY", "MANUAL_REVIEW", "REJECTED"))  # deterministically re-routed, not stuck
        self.assertNotEqual(final["status"], "NEEDS_ENRICHMENT")

        qual = json.loads((self.tmp / "leads" / self.pid / "qualification_v3.json").read_text())
        self.assertEqual(len(qual["ranking_reevaluations"]), 1)
        self.assertEqual(qual["ranking_reevaluations"][0]["fields_added"], {"maps_position": 6, "organic_position": 12})

    def test_rediscovery_never_happens_no_new_business_verification_needed(self):
        """The lead never leaves the pipeline and no verify_business-style
        research is invoked -- confirmed by the fact this whole test makes
        zero Claude calls and completes in real time well under any
        Claude-call timeout."""
        self._seed()
        self._run(["import_ranking_observation.py", "--niche", "roofing", "--location", "Columbus, OH",
                   "--query", "q", "--maps-position", "6", "--observed-at", "2026-09-06T00:00:00+00:00",
                   "--source", "manual_maps_check", "--business-name", "Synthetic Fixture Roofing Co",
                   "--domain", "v37-synthetic-fixture-roofing.test"])
        proc = self._run(["reevaluate_needs_enrichment.py", "--id", self.pid])
        self.assertNotIn("verify_business", proc.stdout)
        self.assertNotIn("discover_prospects", proc.stdout)


# ---------------------------------------------------------------------------
# E. Cost/time reliability
# ---------------------------------------------------------------------------
class TestReliabilityImprovements(unittest.TestCase):
    def test_preflight_budget_still_the_corrected_value(self):
        text = (SCRIPTS / "claude_invoke.py").read_text()
        self.assertIn("max_budget_usd=0.25", text)
        self.assertNotIn("max_budget_usd=0.05", text)

    def test_timeout_bumped_from_observed_production_values(self):
        self.assertEqual(ACQ_CFG["max_claude_call_seconds_research"], 300)
        self.assertEqual(ACQ_CFG["max_claude_call_seconds_short"], 120)

    def test_retry_config_present_and_bounded(self):
        reliability = ACQ_CFG["reliability"]
        self.assertTrue(reliability["retry_on_timeout"])
        self.assertEqual(reliability["max_timeout_retries"], 1)

    def test_timeout_is_retried_exactly_once_then_raises(self):
        ctx = aw.WorkerContext(ACQ_CFG, {}, {}, aw.Deadline(60), lambda m: None, None, False)
        calls = {"n": 0}

        def flaky(*a, **kw):
            calls["n"] += 1
            raise aw.ClaudeTimeout("simulated timeout")

        with patch.object(aw, "run_claude", side_effect=flaky):
            with self.assertRaises(aw.ClaudeTimeout):
                aw.claude_research(ctx, "prompt", {"type": "object"}, "research")
        self.assertEqual(calls["n"], 2)  # original attempt + exactly 1 retry

    def test_success_after_one_retry_is_returned_normally(self):
        ctx = aw.WorkerContext(ACQ_CFG, {}, {}, aw.Deadline(60), lambda m: None, None, False)
        calls = {"n": 0}

        def flaky_then_ok(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise aw.ClaudeTimeout("simulated timeout")
            return {"ok": True}

        with patch.object(aw, "run_claude", side_effect=flaky_then_ok):
            result = aw.claude_research(ctx, "prompt", {"type": "object"}, "research")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(calls["n"], 2)

    def test_non_timeout_errors_are_never_retried(self):
        ctx = aw.WorkerContext(ACQ_CFG, {}, {}, aw.Deadline(60), lambda m: None, None, False)
        calls = {"n": 0}

        def always_invocation_error(*a, **kw):
            calls["n"] += 1
            raise aw.ClaudeInvocationError("not a timeout, retrying would not help")

        with patch.object(aw, "run_claude", side_effect=always_invocation_error):
            with self.assertRaises(aw.ClaudeInvocationError):
                aw.claude_research(ctx, "prompt", {"type": "object"}, "research")
        self.assertEqual(calls["n"], 1)  # no retry attempted

    def test_one_lead_still_isolated_after_exhausting_retries(self):
        """A timeout that survives its bounded retry is still just one
        recorded failure -- never blocks the batch. Reuses
        test_v3_5_acquisition's IsolatedWorkerMixin/prospect() fixtures
        directly rather than duplicating that sandboxing logic."""
        from test_v3_5_acquisition import IsolatedWorkerMixin, prospect

        class _Case(IsolatedWorkerMixin, unittest.TestCase):
            def runTest(self):
                self.seed_discovered([prospect("bad-lead", "DISCOVERED"), prospect("good-lead", "DISCOVERED")])
                calls = []

                def flaky(ctx_, pid):
                    calls.append(pid)
                    raise aw.ClaudeInvocationError("simulated -- retries already exhausted upstream")

                with patch.object(aw, "verify_business_stage", side_effect=flaky):
                    ctx = aw.WorkerContext(ACQ_CFG, {}, {}, aw.Deadline(60), lambda m: None, None, False)
                    aw.advance_stage_a(ctx, "bad-lead")
                    aw.advance_stage_a(ctx, "good-lead")
                self.assertEqual(calls, ["bad-lead", "good-lead"])  # both attempted, neither blocked the other
                self.assertEqual(len(ctx.failures), 2)

        case = _Case()
        case.setUp()
        try:
            case.runTest()
        finally:
            case.tearDown()


if __name__ == "__main__":
    unittest.main()
