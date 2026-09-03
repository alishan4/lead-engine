"""
V3.8 tests: Automated Ranking Enrichment. Synthetic fixtures only -- no
real prospect/business data is read or mutated, no live network call, no
Claude call anywhere in this file's exercised code paths.
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

from _lib import load_yaml, now_iso  # noqa: E402
import rank_enrichment as rke  # noqa: E402
import ranking_providers as rp  # noqa: E402
import acquisition_worker as aw  # noqa: E402

NICHES_CFG = load_yaml("niches.yaml")
RANK_CFG = load_yaml("ranking_enrichment.yaml")
SCORING_CFG = load_yaml("scoring.yaml")


def synth_lead(pid, niche="roofing", city="Columbus", state="OH", **overrides):
    p = {
        "id": pid, "business_name": f"Synthetic Fixture {pid}", "website": f"https://{pid}.test/",
        "niche": niche, "city": city, "state": state, "status": "NEEDS_ENRICHMENT",
        "maps_position": None, "organic_position": None,
        "fit_confirmed_score": 40, "fit_potential_score": 93,
        "gap_confirmed_score": 0, "gap_potential_score": 55,
        "fit_completeness": 56, "gap_completeness": 75, "contactability_score": 2,
    }
    p.update(overrides)
    return p


# ---------------------------------------------------------------------------
# 1. Priority queue
# ---------------------------------------------------------------------------
class TestEnrichmentQueuePriority(unittest.TestCase):
    def test_higher_fit_confirmed_sorts_first(self):
        low = synth_lead("low-fit", fit_confirmed_score=40)
        high = synth_lead("high-fit", fit_confirmed_score=51)
        queue = rke.build_enrichment_queue([low, high], NICHES_CFG)
        self.assertEqual([p["id"] for p in queue], ["high-fit", "low-fit"])

    def test_gap_potential_is_the_tiebreak_after_fit(self):
        a = synth_lead("a", fit_confirmed_score=40, gap_potential_score=55)
        b = synth_lead("b", fit_confirmed_score=40, gap_potential_score=65)
        queue = rke.build_enrichment_queue([a, b], NICHES_CFG)
        self.assertEqual([p["id"] for p in queue], ["b", "a"])

    def test_tier1_niche_sorts_before_tier2_at_equal_fit_and_gap(self):
        tier2 = synth_lead("family-law-lead", niche="family_law", fit_confirmed_score=44, gap_potential_score=55)
        tier1 = synth_lead("roofing-lead", niche="roofing", fit_confirmed_score=44, gap_potential_score=55)
        queue = rke.build_enrichment_queue([tier2, tier1], NICHES_CFG)
        self.assertEqual([p["id"] for p in queue], ["roofing-lead", "family-law-lead"])

    def test_contactability_then_completeness_are_final_tiebreaks(self):
        low_contact = synth_lead("low-contact", contactability_score=1, fit_completeness=90, gap_completeness=90)
        high_contact = synth_lead("high-contact", contactability_score=2, fit_completeness=10, gap_completeness=10)
        queue = rke.build_enrichment_queue([low_contact, high_contact], NICHES_CFG)
        self.assertEqual([p["id"] for p in queue], ["high-contact", "low-contact"])

    def test_ordering_is_deterministic_across_repeated_calls(self):
        records = [synth_lead(f"p{i}", fit_confirmed_score=40 + (i % 3)) for i in range(10)]
        first = [p["id"] for p in rke.build_enrichment_queue(records, NICHES_CFG)]
        second = [p["id"] for p in rke.build_enrichment_queue(list(reversed(records)), NICHES_CFG)]
        self.assertEqual(first, second)


class TestManualReviewExcluded(unittest.TestCase):
    def test_manual_review_never_enters_the_queue(self):
        mr = synth_lead("manual-review-lead", status="MANUAL_REVIEW", fit_confirmed_score=99)
        ne = synth_lead("needs-enrichment-lead", fit_confirmed_score=1)
        queue = rke.build_enrichment_queue([mr, ne], NICHES_CFG)
        self.assertEqual([p["id"] for p in queue], ["needs-enrichment-lead"])

    def test_mixed_statuses_only_needs_enrichment_survives(self):
        records = [
            synth_lead("a", status="NEEDS_ENRICHMENT"),
            synth_lead("b", status="MANUAL_REVIEW"),
            synth_lead("c", status="QUALIFIED"),
            synth_lead("d", status="REJECTED"),
        ]
        queue = rke.build_enrichment_queue(records, NICHES_CFG)
        self.assertEqual([p["id"] for p in queue], ["a"])

    def test_manual_review_ids_never_appear_even_if_they_would_rank_highest(self):
        """Direct regression for 'ranking alone can never qualify a
        MANUAL_REVIEW lead' -- even an extremely high-priority-looking
        MANUAL_REVIEW record must never surface in the queue this module
        drives ranking enrichment from."""
        records = [synth_lead(f"mr-{i}", status="MANUAL_REVIEW", fit_confirmed_score=100 - i) for i in range(5)]
        records.append(synth_lead("real-lead", fit_confirmed_score=1))
        queue = rke.build_enrichment_queue(records, NICHES_CFG)
        self.assertEqual([p["id"] for p in queue], ["real-lead"])


# ---------------------------------------------------------------------------
# 2. Query selection
# ---------------------------------------------------------------------------
class TestQuerySelection(unittest.TestCase):
    def test_bounded_between_min_and_max(self):
        p = synth_lead("q1", niche="roofing")  # roofing has 4 money_keywords, both fields missing -> 8 possible combos
        queries = rke.select_queries(p, NICHES_CFG, RANK_CFG)
        self.assertGreaterEqual(len(queries), RANK_CFG["min_queries_per_lead"])
        self.assertLessEqual(len(queries), RANK_CFG["max_queries_per_lead"])

    def test_respects_a_lower_max_queries_per_lead_override(self):
        p = synth_lead("q2", niche="roofing")
        cfg = dict(RANK_CFG, max_queries_per_lead=2)
        queries = rke.select_queries(p, NICHES_CFG, cfg)
        self.assertEqual(len(queries), 2)

    def test_never_generates_dozens_of_keywords(self):
        p = synth_lead("q3", niche="roofing")
        queries = rke.select_queries(p, NICHES_CFG, RANK_CFG)
        self.assertLessEqual(len(queries), 4)

    def test_no_missing_fields_yields_no_queries(self):
        p = synth_lead("q4", maps_position=6, organic_position=12)
        self.assertEqual(rke.select_queries(p, NICHES_CFG, RANK_CFG), [])

    def test_unknown_niche_or_missing_city_yields_no_queries(self):
        p = synth_lead("q5", niche="not_a_real_niche")
        self.assertEqual(rke.select_queries(p, NICHES_CFG, RANK_CFG), [])
        p2 = synth_lead("q6")
        p2["city"] = None
        self.assertEqual(rke.select_queries(p2, NICHES_CFG, RANK_CFG), [])

    def test_every_query_preserves_required_fields(self):
        p = synth_lead("q7", niche="hvac")
        for qr in rke.select_queries(p, NICHES_CFG, RANK_CFG):
            for field in ("query", "location", "business_name", "domain", "niche", "intended_evidence_type", "why"):
                self.assertIn(field, qr)
            self.assertIn(qr["intended_evidence_type"], ("MAPS", "ORGANIC"))

    def test_maps_and_organic_both_requested_when_both_missing(self):
        p = synth_lead("q8", niche="hvac")
        types = {qr["intended_evidence_type"] for qr in rke.select_queries(p, NICHES_CFG, RANK_CFG)}
        self.assertEqual(types, {"MAPS", "ORGANIC"})

    def test_only_missing_type_requested_when_one_already_known(self):
        p = synth_lead("q9", niche="hvac", maps_position=6)
        types = {qr["intended_evidence_type"] for qr in rke.select_queries(p, NICHES_CFG, RANK_CFG)}
        self.assertEqual(types, {"ORGANIC"})

    def test_queries_reuse_configured_money_keywords_never_invent_one(self):
        p = synth_lead("q10", niche="roofing")
        money_keywords = NICHES_CFG["niches"]["roofing"]["money_keywords"]
        for qr in rke.select_queries(p, NICHES_CFG, RANK_CFG):
            self.assertTrue(any(kw in qr["query"] for kw in money_keywords),
                             f"query {qr['query']!r} does not contain any configured money_keyword")


# ---------------------------------------------------------------------------
# 3. Provider abstraction -- pure helpers
# ---------------------------------------------------------------------------
class TestProviderHelpers(unittest.TestCase):
    def test_queries_match_is_punctuation_and_case_insensitive(self):
        self.assertTrue(rp.queries_match("Roof Replacement, Columbus OH", "roof replacement columbus oh"))

    def test_queries_match_rejects_unrelated_query(self):
        self.assertFalse(rp.queries_match("roof replacement columbus oh", "emergency plumber columbus oh"))

    def test_queries_match_rejects_blank(self):
        self.assertFalse(rp.queries_match("", "roof replacement columbus oh"))
        self.assertFalse(rp.queries_match(None, None))

    def test_valid_position_accepts_reasonable_int(self):
        ok, v = rp.valid_position("6")
        self.assertTrue(ok)
        self.assertEqual(v, 6)

    def test_valid_position_rejects_malformed(self):
        for bad in ("not a number", None, "", -1, 0, 500, "NaN"):
            ok, v = rp.valid_position(bad)
            self.assertFalse(ok, f"{bad!r} should be rejected")

    def test_entity_mismatch_true_when_domains_conflict(self):
        self.assertTrue(rp.entity_mismatch({"domain": "other-business.test"}, "fixture-roofing.test"))

    def test_entity_mismatch_false_when_domains_match(self):
        self.assertFalse(rp.entity_mismatch({"domain": "fixture-roofing.test"}, "fixture-roofing.test"))

    def test_entity_mismatch_false_when_row_has_no_domain(self):
        """Absence is never treated as a conflict -- only an explicit,
        different domain counts as a mismatch."""
        self.assertFalse(rp.entity_mismatch({"domain": None}, "fixture-roofing.test"))


# ---------------------------------------------------------------------------
# 4. ManualImportProvider -- freshness, entity mismatch, Maps/organic separation,
#    [6, 4, 2] query-level regression
# ---------------------------------------------------------------------------
def _write_rankings_csv(tmp_dir, market_id, rows, header=None):
    import csv
    header = header or ["market_id", "business_name", "domain", "keyword", "maps_position", "organic_position",
                         "source", "observed_at", "exact_rank_verified"]
    path = tmp_dir / f"{market_id}.csv"
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        for r in rows:
            w.writerow({h: r.get(h, "") for h in header})
    return path


class ManualImportProviderMixin:
    def setUp(self):
        self.tmp = Path("/tmp") / f"v3_8_manual_import_{id(self)}"
        self.tmp.mkdir(parents=True, exist_ok=True)
        self._patch = patch("rescore_leads.rankings_path", lambda market_id: self.tmp / f"{market_id}.csv")
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _qr(self, **overrides):
        qr = {
            "query": "water damage restoration example city co", "market_id": "restoration-example-city-co",
            "business_name": "Example Restoration Co.", "domain": "example-restoration.test",
            "intended_evidence_type": "MAPS",
        }
        qr.update(overrides)
        return qr


class TestQueryLevelRegression(ManualImportProviderMixin, unittest.TestCase):
    """V3.8 regression, reproducing the real shape of a genuine V3.7.1
    production case (a business tracked under 3 distinct queries at
    maps_position 6/4/2 -- see docs/LEAD-ENGINE.md)
    with a synthetic business/domain. Every query must return its OWN real
    position -- never blended, never reduced to 'the business ranks #2'."""

    def _seed(self):
        rows = [
            {"market_id": "restoration-example-city-co", "business_name": "Example Restoration Co.", "domain": "example-restoration.test",
             "keyword": "water damage restoration example city co", "maps_position": "6", "source": "manual_maps_check",
             "observed_at": now_iso(), "exact_rank_verified": "True"},
            {"market_id": "restoration-example-city-co", "business_name": "Example Restoration Co.", "domain": "example-restoration.test",
             "keyword": "mold remediation example city co", "maps_position": "4", "source": "manual_maps_check",
             "observed_at": now_iso(), "exact_rank_verified": "True"},
            {"market_id": "restoration-example-city-co", "business_name": "Example Restoration Co.", "domain": "example-restoration.test",
             "keyword": "fire damage restoration example city co", "maps_position": "2", "source": "manual_maps_check",
             "observed_at": now_iso(), "exact_rank_verified": "True"},
        ]
        _write_rankings_csv(self.tmp, "restoration-example-city-co", rows)

    def test_each_query_returns_its_own_distinct_position(self):
        self._seed()
        provider = rp.ManualImportProvider()
        expected = {"water damage restoration example city co": 6, "mold remediation example city co": 4,
                    "fire damage restoration example city co": 2}
        for query, expected_pos in expected.items():
            result = provider.fetch(self._qr(query=query), freshness_days=14)
            self.assertEqual(result.status, rp.STATUS_ALREADY_SATISFIED)
            self.assertIn(f"maps_position={expected_pos}", result.reason)

    def test_never_collapses_to_a_single_overall_number(self):
        """The opposite failure mode this test guards against: naively
        matching by business identity alone (ignoring the query) and
        taking min() would report #2 for every query. Each of the 3
        distinct queries here must independently report its own real
        value, not all converge on 2."""
        self._seed()
        provider = rp.ManualImportProvider()
        positions = set()
        for query in ("water damage restoration example city co", "mold remediation example city co", "fire damage restoration example city co"):
            result = provider.fetch(self._qr(query=query), freshness_days=14)
            pos = int(result.reason.split("maps_position=")[1].split()[0])
            positions.add(pos)
        self.assertEqual(positions, {6, 4, 2})


class TestManualImportProviderFreshness(ManualImportProviderMixin, unittest.TestCase):
    def test_fresh_row_is_already_satisfied(self):
        _write_rankings_csv(self.tmp, "restoration-example-city-co", [{
            "market_id": "restoration-example-city-co", "business_name": "Example Restoration Co.", "domain": "example-restoration.test",
            "keyword": "water damage restoration example city co", "maps_position": "6", "source": "manual_maps_check",
            "observed_at": now_iso(), "exact_rank_verified": "True",
        }])
        result = rp.ManualImportProvider().fetch(self._qr(), freshness_days=14)
        self.assertEqual(result.status, rp.STATUS_ALREADY_SATISFIED)

    def test_stale_row_does_not_count_as_fresh_evidence(self):
        old_date = "2020-01-01T00:00:00+00:00"
        _write_rankings_csv(self.tmp, "restoration-example-city-co", [{
            "market_id": "restoration-example-city-co", "business_name": "Example Restoration Co.", "domain": "example-restoration.test",
            "keyword": "water damage restoration example city co", "maps_position": "6", "source": "manual_maps_check",
            "observed_at": old_date, "exact_rank_verified": "True",
        }])
        result = rp.ManualImportProvider().fetch(self._qr(), freshness_days=14)
        self.assertEqual(result.status, rp.STATUS_SOURCE_REQUIRED)
        self.assertIn("stale", result.reason)

    def test_stale_evidence_remains_in_history_but_query_is_still_open(self):
        """Item 14's exact requirement: a stale observation stays on file
        (never deleted -- this test never even touches the CSV after
        writing it) but cannot silently act as fresh qualification
        evidence -- the query must still come back as needing real
        re-enrichment, not a fabricated pass."""
        old_date = "2020-01-01T00:00:00+00:00"
        path = _write_rankings_csv(self.tmp, "restoration-example-city-co", [{
            "market_id": "restoration-example-city-co", "business_name": "Example Restoration Co.", "domain": "example-restoration.test",
            "keyword": "water damage restoration example city co", "maps_position": "6", "source": "manual_maps_check",
            "observed_at": old_date, "exact_rank_verified": "True",
        }])
        result = rp.ManualImportProvider().fetch(self._qr(), freshness_days=14)
        self.assertNotEqual(result.status, rp.STATUS_ALREADY_SATISFIED)
        self.assertTrue(path.exists())  # the stale row itself is never deleted


class TestManualImportProviderRejections(ManualImportProviderMixin, unittest.TestCase):
    def test_no_data_at_all_is_source_required(self):
        result = rp.ManualImportProvider().fetch(self._qr(), freshness_days=14)
        self.assertEqual(result.status, rp.STATUS_SOURCE_REQUIRED)

    def test_missing_source_row_never_counts(self):
        """A row with an empty/unknown source can only ever come from a bug
        or a hand-edited file -- both real writers gate on KNOWN_SOURCES,
        so this is a defensive test of that second line of defense."""
        _write_rankings_csv(self.tmp, "restoration-example-city-co", [{
            "market_id": "restoration-example-city-co", "business_name": "Example Restoration Co.", "domain": "example-restoration.test",
            "keyword": "water damage restoration example city co", "maps_position": "6", "source": "",
            "observed_at": now_iso(), "exact_rank_verified": "True",
        }])
        result = rp.ManualImportProvider().fetch(self._qr(), freshness_days=14)
        self.assertEqual(result.status, rp.STATUS_SOURCE_REQUIRED)

    def test_unknown_source_never_counts(self):
        _write_rankings_csv(self.tmp, "restoration-example-city-co", [{
            "market_id": "restoration-example-city-co", "business_name": "Example Restoration Co.", "domain": "example-restoration.test",
            "keyword": "water damage restoration example city co", "maps_position": "6", "source": "i_made_this_up",
            "observed_at": now_iso(), "exact_rank_verified": "True",
        }])
        result = rp.ManualImportProvider().fetch(self._qr(), freshness_days=14)
        self.assertEqual(result.status, rp.STATUS_SOURCE_REQUIRED)

    def test_malformed_position_never_counts(self):
        _write_rankings_csv(self.tmp, "restoration-example-city-co", [{
            "market_id": "restoration-example-city-co", "business_name": "Example Restoration Co.", "domain": "example-restoration.test",
            "keyword": "water damage restoration example city co", "maps_position": "not-a-number", "source": "manual_maps_check",
            "observed_at": now_iso(), "exact_rank_verified": "True",
        }])
        result = rp.ManualImportProvider().fetch(self._qr(), freshness_days=14)
        self.assertEqual(result.status, rp.STATUS_SOURCE_REQUIRED)

    def test_entity_mismatch_rejected_as_failure_not_silently_accepted(self):
        _write_rankings_csv(self.tmp, "restoration-example-city-co", [{
            "market_id": "restoration-example-city-co", "business_name": "Example Restoration Co.", "domain": "a-totally-different-company.test",
            "keyword": "water damage restoration example city co", "maps_position": "6", "source": "manual_maps_check",
            "observed_at": now_iso(), "exact_rank_verified": "True",
        }])
        result = rp.ManualImportProvider().fetch(self._qr(), freshness_days=14)
        self.assertEqual(result.status, rp.STATUS_FAILURE)
        self.assertIn("entity_mismatch", result.reason)

    def test_maps_and_organic_never_conflated(self):
        """A row with ONLY an organic_position must never satisfy a MAPS
        query, and vice versa."""
        _write_rankings_csv(self.tmp, "restoration-example-city-co", [{
            "market_id": "restoration-example-city-co", "business_name": "Example Restoration Co.", "domain": "example-restoration.test",
            "keyword": "water damage restoration example city co", "organic_position": "8", "source": "manual_maps_check",
            "observed_at": now_iso(), "exact_rank_verified": "True",
        }])
        maps_result = rp.ManualImportProvider().fetch(self._qr(intended_evidence_type="MAPS"), freshness_days=14)
        organic_result = rp.ManualImportProvider().fetch(self._qr(intended_evidence_type="ORGANIC"), freshness_days=14)
        self.assertEqual(maps_result.status, rp.STATUS_SOURCE_REQUIRED)  # no maps_position on this row
        self.assertEqual(organic_result.status, rp.STATUS_ALREADY_SATISFIED)  # organic_position=8 is real and fresh


# ---------------------------------------------------------------------------
# 5. SemrushFileProvider -- inbox file handling, malformed file isolation
# ---------------------------------------------------------------------------
class TestSemrushFileProvider(unittest.TestCase):
    def setUp(self):
        self.tmp = Path("/tmp") / f"v3_8_inbox_{id(self)}"
        self.tmp.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _qr(self, **overrides):
        qr = {
            "query": "roof replacement columbus oh", "market_id": "roofing-columbus-oh",
            "business_name": "Fixture Roofing Co", "domain": "fixture-roofing.test",
            "intended_evidence_type": "MAPS",
        }
        qr.update(overrides)
        return qr

    def test_no_inbox_file_is_source_required(self):
        provider = rp.SemrushFileProvider(inbox_dir=self.tmp)
        result = provider.fetch(self._qr(), freshness_days=14)
        self.assertEqual(result.status, rp.STATUS_SOURCE_REQUIRED)

    def test_valid_matching_observation_is_returned(self):
        obs = {"location": "roofing-columbus-oh", "query": "roof replacement columbus oh",
               "observed_at": now_iso(), "source": "semrush", "maps_position": 5,
               "business_name": "Fixture Roofing Co", "domain": "fixture-roofing.test"}
        (self.tmp / "roofing-columbus-oh.json").write_text(json.dumps([obs]))
        provider = rp.SemrushFileProvider(inbox_dir=self.tmp)
        result = provider.fetch(self._qr(), freshness_days=14)
        self.assertEqual(result.status, rp.STATUS_OBSERVATION)
        self.assertEqual(result.observation["maps_position"], 5)

    def test_malformed_json_file_isolated_as_failure_not_a_crash(self):
        (self.tmp / "roofing-columbus-oh.json").write_text("{not valid json::")
        provider = rp.SemrushFileProvider(inbox_dir=self.tmp)
        with self.assertRaises(rp.ProviderMalformedResponse):
            provider.fetch(self._qr(), freshness_days=14)

    def test_one_bad_row_in_batch_does_not_block_a_good_row(self):
        bad = {"location": "roofing-columbus-oh", "query": "roof replacement columbus oh",
               "observed_at": now_iso(), "source": "not_a_real_source", "maps_position": 5,
               "business_name": "Fixture Roofing Co"}
        good = {"location": "roofing-columbus-oh", "query": "roof replacement columbus oh",
                "observed_at": now_iso(), "source": "semrush", "maps_position": 9,
                "business_name": "Fixture Roofing Co", "domain": "fixture-roofing.test"}
        (self.tmp / "roofing-columbus-oh.json").write_text(json.dumps([bad, good]))
        provider = rp.SemrushFileProvider(inbox_dir=self.tmp)
        result = provider.fetch(self._qr(), freshness_days=14)
        self.assertEqual(result.status, rp.STATUS_OBSERVATION)
        self.assertEqual(result.observation["maps_position"], 9)

    def test_stale_observation_in_inbox_file_not_used(self):
        obs = {"location": "roofing-columbus-oh", "query": "roof replacement columbus oh",
               "observed_at": "2020-01-01T00:00:00+00:00", "source": "semrush", "maps_position": 5,
               "business_name": "Fixture Roofing Co", "domain": "fixture-roofing.test"}
        (self.tmp / "roofing-columbus-oh.json").write_text(json.dumps([obs]))
        provider = rp.SemrushFileProvider(inbox_dir=self.tmp)
        result = provider.fetch(self._qr(), freshness_days=14)
        self.assertEqual(result.status, rp.STATUS_SOURCE_REQUIRED)

    def test_entity_mismatch_in_inbox_file_skipped(self):
        obs = {"location": "roofing-columbus-oh", "query": "roof replacement columbus oh",
               "observed_at": now_iso(), "source": "semrush", "maps_position": 5,
               "business_name": "A Different Business", "domain": "totally-different.test"}
        (self.tmp / "roofing-columbus-oh.json").write_text(json.dumps([obs]))
        provider = rp.SemrushFileProvider(inbox_dir=self.tmp)
        result = provider.fetch(self._qr(), freshness_days=14)
        self.assertEqual(result.status, rp.STATUS_SOURCE_REQUIRED)


class TestExternalRankProvider(unittest.TestCase):
    def test_always_fails_closed_to_source_required(self):
        result = rp.ExternalRankProvider().fetch(
            {"query": "q", "market_id": "m", "business_name": "b", "domain": "d.test", "intended_evidence_type": "MAPS"},
            freshness_days=14,
        )
        self.assertEqual(result.status, rp.STATUS_SOURCE_REQUIRED)

    def test_never_raises_never_fabricates(self):
        # Calling it 20 times must never once return OBSERVATION/ALREADY_SATISFIED
        # -- there is genuinely no live data source behind this stub.
        for _ in range(20):
            result = rp.ExternalRankProvider().fetch(
                {"query": "q", "market_id": "m", "business_name": "b", "domain": "d.test", "intended_evidence_type": "ORGANIC"},
                freshness_days=14,
            )
            self.assertEqual(result.status, rp.STATUS_SOURCE_REQUIRED)


# ---------------------------------------------------------------------------
# 6. attempt_query -- provider-chain isolation (timeout / malformed / no source)
# ---------------------------------------------------------------------------
class _FakeProvider(rp.RankingProvider):
    def __init__(self, name, behavior):
        self.name = name
        self.behavior = behavior  # callable(qr, freshness_days) -> ProviderResult, or raises

    def fetch(self, qr, freshness_days):
        return self.behavior(qr, freshness_days)


class TestAttemptQueryIsolation(unittest.TestCase):
    def _qr(self):
        return {"query": "q", "market_id": "m", "business_name": "b", "domain": "d.test", "intended_evidence_type": "MAPS"}

    def test_provider_timeout_is_isolated_and_chain_continues(self):
        def raises_timeout(qr, fd):
            raise rp.ProviderTimeout("simulated timeout")

        def returns_observation(qr, fd):
            return rp.ProviderResult(rp.STATUS_OBSERVATION, "good", observation={"maps_position": 5})

        providers = [_FakeProvider("flaky", raises_timeout), _FakeProvider("good", returns_observation)]
        result = rp.attempt_query(providers, self._qr(), 14, log=lambda m: None)
        self.assertEqual(result.status, rp.STATUS_OBSERVATION)  # the second provider still answered

    def test_provider_timeout_alone_is_reported_as_failure_not_fabricated_pass(self):
        def raises_timeout(qr, fd):
            raise rp.ProviderTimeout("simulated timeout")

        result = rp.attempt_query([_FakeProvider("flaky", raises_timeout)], self._qr(), 14, log=lambda m: None)
        self.assertEqual(result.status, rp.STATUS_FAILURE)

    def test_malformed_provider_result_isolated(self):
        def raises_malformed(qr, fd):
            raise rp.ProviderMalformedResponse("garbage response")

        result = rp.attempt_query([_FakeProvider("bad", raises_malformed)], self._qr(), 14, log=lambda m: None)
        self.assertEqual(result.status, rp.STATUS_FAILURE)

    def test_auth_and_rate_limit_errors_also_isolated(self):
        for exc in (rp.ProviderAuthError("bad key"), rp.ProviderRateLimited("429"), rp.ProviderGeoUnresolved("no geo")):
            def raiser(qr, fd, exc=exc):
                raise exc
            result = rp.attempt_query([_FakeProvider("x", raiser)], self._qr(), 14, log=lambda m: None)
            self.assertEqual(result.status, rp.STATUS_FAILURE)

    def test_unexpected_non_provider_exception_still_isolated(self):
        """Defensive backstop -- even a totally unexpected bug in a
        provider must never crash the whole cycle."""
        def raises_value_error(qr, fd):
            raise ValueError("totally unexpected bug")

        result = rp.attempt_query([_FakeProvider("buggy", raises_value_error)], self._qr(), 14, log=lambda m: None)
        self.assertEqual(result.status, rp.STATUS_FAILURE)

    def test_no_provider_configured_is_ranking_source_required(self):
        result = rp.attempt_query([], self._qr(), 14, log=lambda m: None)
        self.assertEqual(result.status, "RANKING_SOURCE_REQUIRED")

    def test_every_provider_returning_source_required_yields_ranking_source_required(self):
        def source_required(qr, fd):
            return rp.ProviderResult(rp.STATUS_SOURCE_REQUIRED, "x", reason="nothing available")

        result = rp.attempt_query([_FakeProvider("a", source_required), _FakeProvider("b", source_required)],
                                   self._qr(), 14, log=lambda m: None)
        self.assertEqual(result.status, rp.STATUS_SOURCE_REQUIRED)
        self.assertEqual(result.status, "RANKING_SOURCE_REQUIRED")

    def test_never_marks_qualified_via_fallback_after_a_failure(self):
        """A provider failure must never be silently converted into a
        usable observation -- confirms attempt_query's return carries no
        `observation` payload when the outcome is FAILURE."""
        def raises_timeout(qr, fd):
            raise rp.ProviderTimeout("boom")

        result = rp.attempt_query([_FakeProvider("flaky", raises_timeout)], self._qr(), 14, log=lambda m: None)
        self.assertIsNone(result.observation)


# ---------------------------------------------------------------------------
# 7. run_cycle -- budgets, isolation across leads, reporting fields
# ---------------------------------------------------------------------------
class RunCycleMixin:
    def setUp(self):
        self.tmp = Path("/tmp") / f"v3_8_run_cycle_{id(self)}"
        (self.tmp / "prospects").mkdir(parents=True, exist_ok=True)
        (self.tmp / "leads").mkdir(parents=True, exist_ok=True)
        self._orig = {"PROSPECTS": rke.PROSPECTS}
        rke.PROSPECTS = self.tmp / "prospects"

    def tearDown(self):
        rke.PROSPECTS = self._orig["PROSPECTS"]
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed_needs_enrichment(self, records):
        with open(self.tmp / "prospects" / "needs_enrichment.jsonl", "w") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")
        with open(self.tmp / "prospects" / "discovered.jsonl", "w") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")
        for r in records:
            (self.tmp / "leads" / r["id"]).mkdir(parents=True, exist_ok=True)
        for fname in ("qualified.jsonl", "manual_review.jsonl", "rejected.jsonl"):
            (self.tmp / "prospects" / fname).touch()


class TestRunCycleBudgets(RunCycleMixin, unittest.TestCase):
    def test_empty_backlog_is_a_cheap_no_op(self):
        self._seed_needs_enrichment([])
        stats = rke.run_cycle(cfg=RANK_CFG, niches_cfg=NICHES_CFG, log=lambda m: None)
        self.assertEqual(stats["ranking_backlog_before"], 0)
        self.assertEqual(stats["ranking_leads_attempted"], 0)

    def test_max_enrichment_leads_per_run_is_respected(self):
        records = [synth_lead(f"lead-{i}", niche="roofing") for i in range(5)]
        self._seed_needs_enrichment(records)
        cfg = dict(RANK_CFG, max_enrichment_leads_per_run=2, providers=[])
        stats = rke.run_cycle(cfg=cfg, niches_cfg=NICHES_CFG, log=lambda m: None)
        self.assertEqual(stats["ranking_backlog_before"], 5)
        self.assertEqual(stats["ranking_leads_attempted"], 2)

    def test_max_provider_requests_per_run_caps_total_queries(self):
        records = [synth_lead(f"lead-{i}", niche="roofing") for i in range(3)]  # each needs up to 4 queries missing both fields
        self._seed_needs_enrichment(records)
        cfg = dict(RANK_CFG, max_enrichment_leads_per_run=10, max_queries_per_lead=4,
                   max_provider_requests_per_run=3, providers=[])
        stats = rke.run_cycle(cfg=cfg, niches_cfg=NICHES_CFG, log=lambda m: None)
        self.assertLessEqual(stats["ranking_queries_attempted"], 3)

    def test_one_leads_provider_failure_never_blocks_another_leads_queries(self):
        good = synth_lead("good-lead", niche="roofing")
        bad = synth_lead("bad-lead", niche="hvac")
        self._seed_needs_enrichment([bad, good])

        calls = []

        def flaky_fetch(self_, qr, freshness_days):
            calls.append(qr["business_name"])
            if "bad-lead" in qr["business_name"]:
                raise rp.ProviderTimeout("simulated")
            return rp.ProviderResult(rp.STATUS_SOURCE_REQUIRED, "fake", reason="nothing on file yet")

        with patch.object(rp.ManualImportProvider, "fetch", flaky_fetch):
            cfg = dict(RANK_CFG, providers=["manual_import"])
            stats = rke.run_cycle(cfg=cfg, niches_cfg=NICHES_CFG, log=lambda m: None)

        self.assertEqual(stats["ranking_leads_attempted"], 2)
        self.assertGreaterEqual(stats["ranking_provider_failures"], 1)
        # both leads' business names appear among the calls -- neither was skipped
        self.assertTrue(any("good-lead" in c for c in calls))
        self.assertTrue(any("bad-lead" in c for c in calls))


class TestRunCycleReporting(RunCycleMixin, unittest.TestCase):
    def test_reporting_fields_all_present(self):
        self._seed_needs_enrichment([synth_lead("lead-1", niche="roofing")])
        cfg = dict(RANK_CFG, providers=[])
        stats = rke.run_cycle(cfg=cfg, niches_cfg=NICHES_CFG, log=lambda m: None)
        for field in ("ranking_backlog_before", "ranking_leads_attempted", "ranking_queries_attempted",
                      "ranking_observations_imported", "ranking_provider_failures", "ranking_backlog_after",
                      "qualified_after_ranking", "still_needs_enrichment", "ranking_cost_estimate"):
            self.assertIn(field, stats)

    def test_cost_estimate_is_honestly_zero_not_invented(self):
        self._seed_needs_enrichment([synth_lead("lead-1", niche="roofing")])
        cfg = dict(RANK_CFG, providers=[])
        stats = rke.run_cycle(cfg=cfg, niches_cfg=NICHES_CFG, log=lambda m: None)
        self.assertEqual(stats["ranking_cost_estimate"], 0.0)


# ---------------------------------------------------------------------------
# 8. Deterministic re-evaluation end-to-end (real subprocess, sandboxed) --
#    mirrors tests/test_v3_7_acquisition_quality.py: TestReevaluationEndToEnd
# ---------------------------------------------------------------------------
class TestDeterministicReevaluationEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = Path("/tmp") / f"v3_8_e2e_{id(self)}"
        for sub in ("prospects", "leads", "markets", "rankings", "outreach", "runtime"):
            (self.tmp / sub).mkdir(parents=True, exist_ok=True)
        self.pid = "roofing-columbus-oh-v38-synthetic-fixture"
        self.env = dict(os.environ)
        self.env["LEAD_ENGINE_DATA_DIR"] = str(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed(self):
        prospect = {
            "id": self.pid, "business_name": "V38 Synthetic Fixture Roofing Co",
            "website": "https://v38-synthetic-fixture-roofing.test/",
            "city": "Columbus", "state": "OH", "country": "US", "niche": "roofing",
            "status": "NEEDS_ENRICHMENT", "maps_position": None, "organic_position": None,
            "fit_confirmed_score": 47, "fit_potential_score": 100,
            "gap_confirmed_score": 0, "gap_potential_score": 55,
            "obvious_website_issue": [], "obvious_gbp_issue": [], "service_page_count": 6,
            "competitor_gap": [], "review_count": 45, "commercial_value_signal": "high",
            "verified_business": True, "contactability_score": 2, "buying_signal_tiers": {},
        }
        for fname in ("discovered.jsonl", "needs_enrichment.jsonl"):
            (self.tmp / "prospects" / fname).write_text(json.dumps(prospect) + "\n")
        for fname in ("qualified.jsonl", "manual_review.jsonl", "rejected.jsonl"):
            (self.tmp / "prospects" / fname).touch()
        (self.tmp / "leads" / self.pid).mkdir(parents=True, exist_ok=True)

    def _run(self, args, timeout=30):
        return subprocess.run([sys.executable] + args, cwd=SCRIPTS, env=self.env,
                               capture_output=True, text=True, timeout=timeout)

    def test_no_evidence_anywhere_leaves_lead_at_needs_enrichment(self):
        self._seed()
        proc = self._run(["rank_enrichment.py"])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        stats = json.loads(proc.stdout)
        self.assertEqual(stats["qualified_after_ranking"], 0)
        self.assertEqual(stats["still_needs_enrichment"], 1)
        recs = [json.loads(l) for l in (self.tmp / "prospects" / "needs_enrichment.jsonl").read_text().splitlines()]
        self.assertEqual(recs[0]["status"], "NEEDS_ENRICHMENT")

    def test_already_imported_evidence_is_consumed_and_lead_is_rerouted(self):
        self._seed()
        # Simulate evidence a human already imported via the existing,
        # unchanged import_ranking_observation.py CLI in a prior cycle.
        imp = self._run(["import_ranking_observation.py", "--niche", "roofing", "--location", "Columbus, OH",
                          "--query", "roof replacement columbus oh", "--maps-position", "6", "--organic-position", "12",
                          "--observed-at", now_iso(), "--source", "manual_maps_check",
                          "--business-name", "V38 Synthetic Fixture Roofing Co", "--domain", "v38-synthetic-fixture-roofing.test"])
        self.assertEqual(imp.returncode, 0, imp.stdout + imp.stderr)

        proc = self._run(["rank_enrichment.py"])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        stats = json.loads(proc.stdout)
        self.assertEqual(stats["ranking_leads_attempted"], 1)

        final = [json.loads(l) for l in (self.tmp / "prospects" / "discovered.jsonl").read_text().splitlines()][0]
        self.assertEqual(final["maps_position"], 6)
        self.assertEqual(final["organic_position"], 12)
        self.assertNotEqual(final["status"], "NEEDS_ENRICHMENT")  # deterministically re-routed

    def test_no_rediscovery_no_claude_call_and_completes_fast(self):
        """Confirms zero re-research/re-discovery -- the whole cycle
        completes well under any Claude-call timeout because it never
        makes one."""
        self._seed()
        proc = self._run(["rank_enrichment.py"], timeout=15)
        self.assertNotIn("verify_business", proc.stdout)
        self.assertNotIn("discover_prospects", proc.stdout)
        self.assertNotIn("claude", proc.stdout.lower())


# ---------------------------------------------------------------------------
# 9. Daily orchestration ordering
# ---------------------------------------------------------------------------
def fake_completed_process():
    import subprocess as sp
    return sp.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


class TestOrchestrationOrder(unittest.TestCase):
    """Reuses test_v3_5_acquisition's IsolatedWorkerMixin/prospect()
    fixtures directly rather than duplicating that sandboxing logic."""

    def test_rank_enrichment_runs_before_fresh_discovery(self):
        from test_v3_5_acquisition import IsolatedWorkerMixin, prospect

        class _Case(IsolatedWorkerMixin, unittest.TestCase):
            def runTest(self):
                self.seed_discovered([prospect("pending-1", "DISCOVERED")])
                order = []
                with patch("acquisition_worker.claude_preflight.check", return_value=(True, "AUTH_OK", "ok")):
                    with patch.object(aw, "process_lead", side_effect=lambda ctx, pid: order.append(("pending", pid))):
                        with patch.object(aw, "call_plain", return_value=fake_completed_process()):
                            with patch.object(aw.rank_enrichment, "run_cycle",
                                               side_effect=lambda **kw: order.append(("ranking", None)) or aw.rank_enrichment.empty_stats()):
                                with patch.object(aw, "discovery_phase", side_effect=lambda ctx: order.append(("discovery", None))):
                                    with patch("acquisition_worker.load_yaml", return_value={
                                        "max_worker_runtime_seconds": 3600, "max_fresh_market_cells_per_run": 1,
                                        "max_fresh_candidates_researched_per_run": 1, "outreach_worthy_ceiling": 15,
                                        "max_claude_call_seconds_research": 10, "max_claude_call_seconds_short": 10,
                                        "max_budget_usd_per_call": 0.1, "discovery_markets": {"niches": [], "cities": []},
                                    }):
                                        aw.run(log=lambda m: None)
                self.assertEqual([o[0] for o in order], ["pending", "ranking", "discovery"])

        case = _Case()
        case.setUp()
        try:
            case.runTest()
        finally:
            case.tearDown()

    def test_ranking_stats_flow_into_the_run_summary(self):
        from test_v3_5_acquisition import IsolatedWorkerMixin, prospect

        class _Case(IsolatedWorkerMixin, unittest.TestCase):
            def runTest(self):
                self.seed_discovered([])
                fake_ranking_stats = dict(aw.rank_enrichment.empty_stats())
                fake_ranking_stats["ranking_backlog_before"] = 7
                fake_ranking_stats["qualified_after_ranking"] = 2
                with patch("acquisition_worker.claude_preflight.check", return_value=(True, "AUTH_OK", "ok")):
                    with patch.object(aw, "call_plain", return_value=fake_completed_process()):
                        with patch.object(aw.rank_enrichment, "run_cycle", return_value=fake_ranking_stats):
                            with patch("acquisition_worker.load_yaml", return_value={
                                "max_worker_runtime_seconds": 60, "max_fresh_market_cells_per_run": 0,
                                "max_fresh_candidates_researched_per_run": 0, "outreach_worthy_ceiling": 15,
                                "max_claude_call_seconds_research": 10, "max_claude_call_seconds_short": 10,
                                "max_budget_usd_per_call": 0.1, "discovery_markets": {"niches": [], "cities": []},
                            }):
                                stats = aw.run(log=lambda m: None)
                self.assertEqual(stats["ranking_backlog_before"], 7)
                self.assertEqual(stats["qualified_after_ranking"], 2)

        case = _Case()
        case.setUp()
        try:
            case.runTest()
        finally:
            case.tearDown()


# ---------------------------------------------------------------------------
# 10. Static safety guards -- no Claude, no Gmail, no threshold drift, no
#     discovery-budget inflation
# ---------------------------------------------------------------------------
class TestStaticSafetyGuards(unittest.TestCase):
    def test_no_claude_invocation_anywhere_in_ranking_enrichment_code(self):
        for fname in ("rank_enrichment.py", "ranking_providers.py"):
            text = (SCRIPTS / fname).read_text()
            self.assertNotIn("claude_invoke", text)
            self.assertNotIn("claude -p", text)
            self.assertNotIn("run_claude(", text)

    def test_no_seo_specialist_agents_invoked(self):
        for fname in ("rank_enrichment.py", "ranking_providers.py"):
            text = (SCRIPTS / fname).read_text()
            self.assertNotIn("route_to_specialist", text)
            self.assertNotIn("claude-seo", text)
            self.assertNotIn("ask_specialist", text)

    def test_no_gmail_code_path(self):
        for fname in ("rank_enrichment.py", "ranking_providers.py"):
            text = (SCRIPTS / fname).read_text().lower()
            self.assertNotIn("gmail", text)
            self.assertNotIn("smtp", text)
            self.assertNotIn("send_executor", text)

    def test_no_credentials_embedded(self):
        for fname in ("rank_enrichment.py", "ranking_providers.py", "ranking_enrichment.yaml"):
            path = SCRIPTS / fname if fname.endswith(".py") else (ROOT / "config" / fname)
            text = path.read_text().lower()
            self.assertNotIn("api_key", text)
            self.assertNotIn("password", text)
            self.assertNotIn("semrush_api", text)

    def test_fit_and_gap_thresholds_unchanged(self):
        cfg = load_yaml("scoring.yaml")
        self.assertEqual(cfg["thresholds"]["qualified_min"], 70)
        self.assertEqual(cfg["thresholds"]["manual_review_min"], 55)
        self.assertEqual(cfg["fit_thresholds"]["reject_max"], 29)
        self.assertEqual(cfg["fit_thresholds"]["manual_review_max"], 39)
        self.assertEqual(cfg["fit_thresholds"]["qualified_min"], 40)
        self.assertEqual(cfg["fit_thresholds"]["high_priority_min"], 65)
        self.assertEqual(cfg["gap_thresholds"]["qualified_min"], 40)
        self.assertEqual(cfg["gap_thresholds"]["high_priority_min"], 50)

    def test_discovery_budget_not_inflated_by_v38(self):
        """V3.8 must never increase daily Claude research just because an
        enrichment backlog exists -- these acquisition.yaml caps must stay
        exactly what they were set to by V3.7."""
        acq_cfg = load_yaml("acquisition.yaml")
        self.assertEqual(acq_cfg["max_fresh_candidates_researched_per_run"], 10)
        self.assertEqual(acq_cfg["max_fresh_market_cells_per_run"], 3)

    def test_freshness_days_matches_limits_yaml(self):
        limits_cfg = load_yaml("limits.yaml")
        self.assertEqual(RANK_CFG["freshness_days"], limits_cfg["ranking_freshness_days"])

    def test_import_and_reevaluate_scripts_remain_unmodified_choke_points(self):
        """Static guard: the V3.7 write-time provenance gate this whole
        module depends on must still be present, unmodified, in the two
        scripts it reuses rather than duplicates."""
        text = (SCRIPTS / "import_ranking_observation.py").read_text()
        self.assertIn('obs.get("source") not in KNOWN_SOURCES', text)


if __name__ == "__main__":
    unittest.main()
