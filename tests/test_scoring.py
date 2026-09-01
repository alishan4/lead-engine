"""
Unit tests for the rule-based scorer. Run with:
  python3 -m pytest lead-engine/tests -q
or
  python3 lead-engine/tests/test_scoring.py
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from score_leads import score_prospect, hard_reject_reason, is_dominant  # noqa: E402
from _lib import load_yaml  # noqa: E402

SCORING_CFG = load_yaml("scoring.yaml")
NICHES_CFG = load_yaml("niches.yaml")


def base_prospect(**overrides):
    p = {
        "id": "test-1",
        "business_name": "Test Co",
        "city": "Charlotte",
        "state": "NC",
        "niche": "roofing",
        "website": "https://example.com",
        "maps_position": None,
        "organic_position": None,
        "rating": None,
        "review_count": None,
        "obvious_website_issue": [],
        "obvious_gbp_issue": [],
        "service_page_count": None,
        "competitor_gap": [],
        "commercial_value_signal": "high",
        "verified_business": True,
    }
    p.update(overrides)
    return p


class TestHardReject(unittest.TestCase):
    def test_no_commercial_intent_rejects(self):
        p = base_prospect(commercial_value_signal="none")
        self.assertEqual(hard_reject_reason(p, SCORING_CFG["reject_if"]), "no_commercial_intent")

    def test_site_down_rejects(self):
        p = base_prospect(obvious_website_issue=["site_down"])
        self.assertEqual(hard_reject_reason(p, SCORING_CFG["reject_if"]), "broken_or_non_legitimate")

    def test_healthy_business_not_rejected(self):
        p = base_prospect()
        self.assertIsNone(hard_reject_reason(p, SCORING_CFG["reject_if"]))


class TestDominance(unittest.TestCase):
    def test_dominant_business_flagged(self):
        p = base_prospect(maps_position=1, organic_position=1, rating=4.9, review_count=200)
        self.assertTrue(is_dominant(p))

    def test_weak_maps_position_not_dominant(self):
        p = base_prospect(maps_position=8, organic_position=1, rating=4.9, review_count=200)
        self.assertFalse(is_dominant(p))


class TestScoring(unittest.TestCase):
    def test_ideal_prospect_scores_qualified(self):
        p = base_prospect(
            maps_position=7,
            organic_position=12,
            obvious_website_issue=["thin_service_pages", "weak_cta"],
            service_page_count=2,
            competitor_gap=["no service-area pages vs top competitor"],
            review_count=15,
            verified_business=True,
        )
        score, breakdown = score_prospect(p, SCORING_CFG, NICHES_CFG)
        self.assertGreaterEqual(score, SCORING_CFG["thresholds"]["qualified_min"])
        self.assertIn("maps_position_4_to_15", breakdown)
        self.assertIn("weak_service_pages", breakdown)
        self.assertIn("high_value_niche", breakdown)

    def test_dominant_business_scores_low(self):
        p = base_prospect(
            maps_position=1,
            organic_position=1,
            rating=4.9,
            review_count=300,
            service_page_count=10,
        )
        score, breakdown = score_prospect(p, SCORING_CFG, NICHES_CFG)
        self.assertIn("already_dominant", breakdown)
        self.assertLess(score, SCORING_CFG["thresholds"]["qualified_min"])

    def test_weak_signals_score_low(self):
        p = base_prospect(
            niche="other_high_value_local",
            maps_position=None,
            organic_position=None,
            service_page_count=8,
            review_count=200,
            verified_business=False,
        )
        score, breakdown = score_prospect(p, SCORING_CFG, NICHES_CFG)
        self.assertLess(score, SCORING_CFG["thresholds"]["manual_review_min"])


if __name__ == "__main__":
    unittest.main()
