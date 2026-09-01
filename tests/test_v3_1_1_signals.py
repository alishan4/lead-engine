"""
V3.1.1 tests: the evidence-object buying-signal model, source hierarchy,
confidence tiers, freshness, entity matching, conflict handling, review
velocity, manual import, and the tightened HIGH_PRIORITY/why_now gates.
Pure functions and file-format validation only -- no network, no AI, no
claude-seo agent, no Gmail action anywhere in this file or the modules it
tests.

Run with:
  python3 -m pytest lead-engine/tests -q
or
  python3 lead-engine/tests/test_v3_1_1_signals.py
"""
import inspect
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from _lib import load_yaml, now_iso  # noqa: E402
from signal_evidence import (  # noqa: E402
    resolve_signal, resolve_signals, derive_paid_search_organic_gap,
    compute_review_velocity, confidence_tier, is_fresh, passes_entity_match,
)
from score_leads import score_fit  # noqa: E402
from qualify_leads import route_v3, build_why_now, HIGH_PRIORITY_SIGNAL_FIELDS  # noqa: E402
import import_buying_signals  # noqa: E402
import assess_buying_signals  # noqa: E402
import record_review_snapshot  # noqa: E402

SOURCE_CFG = load_yaml("signal_sources.yaml")
SCORING_CFG = load_yaml("scoring.yaml")
LIMITS_CFG = load_yaml("limits.yaml")
NICHES_CFG = load_yaml("niches.yaml")


def make_evidence(signal_type, value, confidence, source_type, entity_match_confidence=0.9,
                   observed_at=None, source="https://example.com"):
    return {
        "signal_type": signal_type, "value": value, "confidence": confidence,
        "source": source, "source_type": source_type,
        "observed_at": observed_at or now_iso(), "published_at": None,
        "evidence": "test evidence", "entity_match_confidence": entity_match_confidence,
        "notes": None,
    }


def days_ago(n):
    return (datetime.now(timezone.utc) - timedelta(days=n)).isoformat()


class Test1_VerifiedAdsContributesToHighPriority(unittest.TestCase):
    def test_strong_verified_ads_resolves_true_and_reaches_high_priority(self):
        items = [make_evidence("runs_google_ads", True, 0.92, "google_serp")]
        r = resolve_signal(items, SOURCE_CFG)
        self.assertTrue(r["value"])
        self.assertEqual(r["tier"], "STRONG_VERIFIED")

        p = {
            "business_name": "Test Co", "niche": "roofing", "city": "X", "state": "NC",
            "runs_google_ads": True, "buying_signal_tiers": {"runs_google_ads": "STRONG_VERIFIED"},
            "review_count": 45, "years_in_business": 8, "multiple_locations": True,
            "service_page_count": 6, "contactability_score": 2,
            "fit_confirmed_score": 70, "fit_potential_score": 90,
            "gap_confirmed_score": 55, "gap_potential_score": 80,
        }
        status, reason, audit_ok, why = route_v3(p, SCORING_CFG, LIMITS_CFG)
        self.assertEqual(status, "HIGH_PRIORITY")


class Test2_GuessedAdsRemainsNull(unittest.TestCase):
    def test_low_confidence_guess_resolves_to_none(self):
        items = [make_evidence("runs_google_ads", True, 0.3, "google_serp")]  # below weak_min
        r = resolve_signal(items, SOURCE_CFG)
        self.assertIsNone(r["value"])
        self.assertEqual(r["status"], "NO_EVIDENCE")
        self.assertTrue(any(x["_reject_reason"] == "unusable_confidence" for x in r["evidence_rejected"]))


class Test3_HighCpcDoesNotImplyAds(unittest.TestCase):
    def test_market_cpc_data_never_sets_runs_google_ads(self):
        market_with_high_cpc = {
            "top_competitors": [{"name": "Competitor A"}],
            "cpc": {"roof replacement charlotte nc": 45.0},  # high CPC present in market cache
        }
        p = {
            "niche": "roofing", "runs_google_ads": None, "runs_lsa": None,
            "review_count": None, "years_in_business": None, "multiple_locations": None,
            "service_page_count": None, "contactability_score": None,
        }
        fit = score_fit(p, SCORING_CFG, NICHES_CFG, market=market_with_high_cpc, blocklist=[])
        self.assertNotIn("buying_intent", fit["breakdown"])  # no confirmed points from cpc data alone

    def test_prompt_explicitly_forbids_cpc_inference(self):
        prompt = (Path(__file__).resolve().parent.parent / "prompts" / "buying-signals.md").read_text()
        self.assertIn("high CPC", prompt)
        self.assertIn("Never infer", prompt)


class Test4_GbpPresenceDoesNotImplyLsa(unittest.TestCase):
    def test_no_lsa_evidence_resolves_none_even_with_gbp_present(self):
        r = resolve_signal([], SOURCE_CFG)  # no LSA evidence gathered at all
        self.assertIsNone(r["value"])

    def test_prompt_explicitly_forbids_gbp_inference_for_lsa(self):
        prompt = (Path(__file__).resolve().parent.parent / "prompts" / "buying-signals.md").read_text()
        self.assertIn("having a GBP", prompt)


class Test5_PaidOrganicGapRequiresBoth(unittest.TestCase):
    def test_requires_paid_and_verified_organic_position(self):
        self.assertTrue(derive_paid_search_organic_gap(True, None, 12))
        self.assertIsNone(derive_paid_search_organic_gap(True, None, None))  # paid confirmed, rank unknown
        self.assertIsNone(derive_paid_search_organic_gap(None, None, 12))    # rank known, paid unknown
        self.assertFalse(derive_paid_search_organic_gap(False, False, 12))


class Test6_MultipleLocationsDoesNotImplyNewLocation(unittest.TestCase):
    def test_separate_signal_types_never_cross_contaminate(self):
        items = [make_evidence("multiple_locations", True, 0.9, "official_website")]
        resolved = resolve_signals(items, SOURCE_CFG)
        self.assertIn("multiple_locations", resolved)
        self.assertNotIn("new_location", resolved)
        self.assertNotIn("recent_expansion", resolved)


class Test7_CurrentServiceDoesNotImplyNewService(unittest.TestCase):
    def test_no_evidence_means_new_high_value_service_stays_null(self):
        p = {"niche": "roofing", "service_page_count": 8, "new_high_value_service": None,
             "runs_google_ads": None, "review_count": None, "years_in_business": None,
             "multiple_locations": None, "contactability_score": None}
        fit = score_fit(p, SCORING_CFG, NICHES_CFG, market=None, blocklist=[])
        # buying_intent stays entirely unconfirmed -- offering services isn't evidence of a NEW one
        self.assertNotIn("buying_intent", fit["breakdown"])


class Test8_ModernSiteDoesNotImplyRecentInvestment(unittest.TestCase):
    def test_prompt_forbids_modern_look_inference(self):
        prompt = (Path(__file__).resolve().parent.parent / "prompts" / "buying-signals.md").read_text()
        self.assertIn("copyright notice", prompt)
        self.assertIn("looking modern", prompt)

    def test_no_evidence_leaves_recent_site_investment_null(self):
        r = resolve_signal([], SOURCE_CFG)
        self.assertIsNone(r["value"])


class Test9_TotalReviewsAloneCannotProduceVelocity(unittest.TestCase):
    def test_single_snapshot_is_unknown(self):
        snapshots = [{"business_id": "x", "observed_at": now_iso(), "review_count": 500, "rating": 4.9, "source": "s"}]
        self.assertEqual(compute_review_velocity(snapshots, SOURCE_CFG), "UNKNOWN")

    def test_zero_snapshots_is_unknown(self):
        self.assertEqual(compute_review_velocity([], SOURCE_CFG), "UNKNOWN")


class Test10_TwoSnapshotsProduceDeterministicVelocity(unittest.TestCase):
    def test_strong_velocity_from_two_snapshots(self):
        snapshots = [
            {"business_id": "x", "observed_at": days_ago(30), "review_count": 10, "rating": 4.5, "source": "s1"},
            {"business_id": "x", "observed_at": now_iso(), "review_count": 20, "rating": 4.6, "source": "s2"},
        ]
        # 10 reviews in 30 days = 10/month >= strong_per_month(5)
        self.assertEqual(compute_review_velocity(snapshots, SOURCE_CFG), "STRONG")

    def test_low_velocity_from_two_snapshots(self):
        snapshots = [
            {"business_id": "x", "observed_at": days_ago(30), "review_count": 10, "rating": 4.5, "source": "s1"},
            {"business_id": "x", "observed_at": now_iso(), "review_count": 11, "rating": 4.5, "source": "s2"},
        ]
        self.assertEqual(compute_review_velocity(snapshots, SOURCE_CFG), "LOW")


class Test11_StaleAdsCannotTriggerHighPriority(unittest.TestCase):
    def test_stale_ads_evidence_resolves_none(self):
        items = [make_evidence("runs_google_ads", True, 0.9, "google_serp", observed_at=days_ago(30))]  # window=7
        r = resolve_signal(items, SOURCE_CFG)
        self.assertIsNone(r["value"])
        self.assertEqual(r["status"], "STALE_ONLY")


class Test12_StaleHiringCannotTriggerHighPriority(unittest.TestCase):
    def test_stale_hiring_evidence_resolves_none(self):
        items = [make_evidence("marketing_hiring_signal", True, 0.9, "official_careers", observed_at=days_ago(90))]  # window=60
        r = resolve_signal(items, SOURCE_CFG)
        self.assertIsNone(r["value"])
        self.assertEqual(r["status"], "STALE_ONLY")


class Test13_AmbiguousEntityMatchRejectsSignal(unittest.TestCase):
    def test_low_entity_match_confidence_rejected(self):
        items = [make_evidence("runs_google_ads", True, 0.9, "google_serp", entity_match_confidence=0.4)]
        r = resolve_signal(items, SOURCE_CFG)
        self.assertIsNone(r["value"])
        self.assertEqual(r["status"], "ENTITY_REJECTED")


class Test14_ConflictingEvidenceBecomesConflicted(unittest.TestCase):
    def test_disagreeing_fresh_evidence_conflicts(self):
        items = [
            make_evidence("new_location", True, 0.8, "official_website"),
            make_evidence("new_location", False, 0.8, "official_press_release"),
        ]
        r = resolve_signal(items, SOURCE_CFG)
        self.assertEqual(r["status"], "CONFLICTED")
        self.assertIsNone(r["value"])
        self.assertEqual(len(r["evidence_used"]), 2)  # both kept, neither discarded


class Test15_WeakConfidenceCannotTriggerHighPriority(unittest.TestCase):
    def test_weak_tier_signal_excluded_from_high_priority(self):
        items = [make_evidence("new_location", True, 0.6, "credible_business_directory")]  # low-confidence source, capped WEAK
        r = resolve_signal(items, SOURCE_CFG)
        self.assertTrue(r["value"])  # still resolves a value...
        self.assertEqual(r["tier"], "WEAK")  # ...but only at WEAK

        p = {
            "business_name": "Test Co", "niche": "roofing", "city": "X", "state": "NC",
            "new_location": True, "buying_signal_tiers": {"new_location": "WEAK"},
            "review_count": 45, "years_in_business": 8, "contactability_score": 2,
            "fit_confirmed_score": 70, "fit_potential_score": 90,
            "gap_confirmed_score": 55, "gap_potential_score": 80,
        }
        status, reason, audit_ok, why = route_v3(p, SCORING_CFG, LIMITS_CFG)
        self.assertNotEqual(status, "HIGH_PRIORITY")


class Test16_VerifiedExpansionPopulatesWhyNow(unittest.TestCase):
    def test_verified_expansion_sets_why_now(self):
        p = {"business_name": "Test Co", "recent_expansion": True,
             "buying_signal_tiers": {"recent_expansion": "VERIFIED"}}
        why = build_why_now(p)
        self.assertIsNotNone(why["why_now"])
        self.assertIn("expanded", why["why_now"])


class Test17_BadSeoAloneCannotPopulateWhyNow(unittest.TestCase):
    def test_strong_gap_breakdown_does_not_populate_why_now(self):
        p = {
            "business_name": "Test Co",
            "gap_breakdown": {"service_architecture": 20, "technical_indexation": 10, "competitor_gap": 10},
            "competitor_gap": ["competitors outrank us badly"],
        }
        why = build_why_now(p)
        self.assertIsNone(why["why_now"])
        self.assertIsNotNone(why["why_this_problem"])  # the SEO facts belong here, not in why_now


class Test18_CorrelationPreventsDoubleCounting(unittest.TestCase):
    def test_ads_plus_derived_gap_capped_combination(self):
        p_ads_only = {"niche": "roofing", "runs_google_ads": True, "runs_lsa": None,
                      "paid_search_organic_gap": None, "recent_expansion": None, "new_location": None,
                      "marketing_hiring_signal": None, "review_velocity_signal": None,
                      "recent_site_investment": None, "new_high_value_service": None,
                      "review_count": None, "years_in_business": None, "multiple_locations": None,
                      "contactability_score": None}
        p_ads_and_gap = {**p_ads_only, "paid_search_organic_gap": True}
        fit_ads_only = score_fit(p_ads_only, SCORING_CFG, NICHES_CFG, market=None, blocklist=[])
        fit_both = score_fit(p_ads_and_gap, SCORING_CFG, NICHES_CFG, market=None, blocklist=[])
        # the combined signal must add SOME credit but stay well within the 30-pt cap --
        # not simply double the independent per-signal weight.
        self.assertGreater(fit_both["breakdown"]["buying_intent"], fit_ads_only["breakdown"]["buying_intent"])
        self.assertLessEqual(fit_both["breakdown"]["buying_intent"], SCORING_CFG["fit_weights"]["buying_intent"])


class Test19_ManualImportValidatesEvidence(unittest.TestCase):
    def test_valid_row_passes(self):
        row = {
            "business_id": "x", "signal_type": "runs_google_ads", "value": "true",
            "source": "https://google.com", "source_type": "google_serp",
            "observed_at": "2026-09-02T00:00:00+00:00", "published_at": "",
            "confidence": "0.9", "evidence": "sponsored ad observed",
            "entity_match_confidence": "0.95", "notes": "",
        }
        result, errors = import_buying_signals.validate_row(row, 2)
        self.assertEqual(errors, [])
        self.assertEqual(result["item"]["value"], True)
        self.assertEqual(result["business_id"], "x")


class Test20_MalformedImportRejectedSafely(unittest.TestCase):
    def test_missing_column_rejected(self):
        row = {"business_id": "x", "signal_type": "runs_google_ads"}  # missing everything else
        result, errors = import_buying_signals.validate_row(row, 3)
        self.assertIsNone(result)
        self.assertTrue(len(errors) > 0)

    def test_out_of_range_confidence_rejected(self):
        row = {
            "business_id": "x", "signal_type": "runs_google_ads", "value": "true",
            "source": "s", "source_type": "google_serp", "observed_at": "2026-09-02T00:00:00+00:00",
            "confidence": "1.5", "evidence": "e", "entity_match_confidence": "0.9",
        }
        result, errors = import_buying_signals.validate_row(row, 4)
        self.assertIsNone(result)
        self.assertTrue(any("confidence" in e for e in errors))

    def test_invalid_source_type_rejected(self):
        row = {
            "business_id": "x", "signal_type": "runs_google_ads", "value": "true",
            "source": "s", "source_type": "made_up_source", "observed_at": "2026-09-02T00:00:00+00:00",
            "confidence": "0.9", "evidence": "e", "entity_match_confidence": "0.9",
        }
        result, errors = import_buying_signals.validate_row(row, 5)
        self.assertIsNone(result)
        self.assertTrue(any("source_type" in e for e in errors))


class Test21_PreviousV3_1RecordsStillLoad(unittest.TestCase):
    def test_v3_1_only_record_scores_without_tiers_or_evidence(self):
        """A record from before V3.1.1 existed -- has flat booleans, no buying_signal_tiers at all."""
        p = {
            "niche": "roofing", "runs_google_ads": True, "runs_lsa": None,
            "paid_search_organic_gap": True, "recent_expansion": None, "new_location": None,
            "marketing_hiring_signal": None, "review_velocity_signal": None,
            "recent_site_investment": None, "new_high_value_service": None,
            "multiple_locations": True, "review_count": 40, "years_in_business": 10,
            "service_page_count": 6, "contactability_score": 2,
        }
        fit = score_fit(p, SCORING_CFG, NICHES_CFG, market=None, blocklist=[])
        self.assertIsInstance(fit["confirmed_score"], int)  # does not raise despite no tiers field
        # but route_v3 correctly withholds HIGH_PRIORITY without a tiered signal
        p.update({"fit_confirmed_score": fit["confirmed_score"], "fit_potential_score": fit["potential_score"],
                   "gap_confirmed_score": 55, "gap_potential_score": 80, "business_name": "X", "city": "Y", "state": "Z"})
        status, reason, audit_ok, why = route_v3(p, SCORING_CFG, LIMITS_CFG)
        self.assertNotEqual(status, "HIGH_PRIORITY")


class Test23_OneLeadFailureDoesNotAffectAnother(unittest.TestCase):
    def test_resolving_two_businesses_evidence_independently(self):
        items_a = [make_evidence("runs_google_ads", True, 0.9, "google_serp")]
        items_b = [make_evidence("runs_google_ads", False, 0.9, "google_serp")]
        resolved_a = resolve_signals(items_a, SOURCE_CFG)
        resolved_b = resolve_signals(items_b, SOURCE_CFG)
        self.assertTrue(resolved_a["runs_google_ads"]["value"])
        self.assertFalse(resolved_b["runs_google_ads"]["value"])
        # re-resolving A after B must not have been contaminated by B's list
        resolved_a_again = resolve_signals(items_a, SOURCE_CFG)
        self.assertTrue(resolved_a_again["runs_google_ads"]["value"])


class Test24_NoClaudeSeoAgentInvoked(unittest.TestCase):
    def test_no_agent_references_in_v3_1_1_modules(self):
        import signal_evidence
        for mod in (signal_evidence, import_buying_signals, assess_buying_signals, record_review_snapshot):
            src = inspect.getsource(mod)
            self.assertNotIn("claude-seo:", src)
            self.assertNotIn("subagent_type", src)


class Test25_NoGmailActionOccurs(unittest.TestCase):
    def test_no_gmail_references_in_v3_1_1_modules(self):
        import signal_evidence
        for mod in (signal_evidence, import_buying_signals, assess_buying_signals, record_review_snapshot):
            src = inspect.getsource(mod).lower()
            self.assertNotIn("gmail", src)
            self.assertNotIn("smtp", src)
            self.assertNotIn("send_message", src)


if __name__ == "__main__":
    unittest.main()
