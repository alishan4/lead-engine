"""
V2 unit tests: data-completeness model, NEEDS_ENRICHMENT routing, rescoring,
business-identity verification, contact verification, stale-finding
protection, and email-QA guards. No network, no AI -- all pure functions.

Run with:
  python3 -m pytest lead-engine/tests -q
or
  python3 lead-engine/tests/test_v2_pipeline.py
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import _lib  # noqa: E402
from _lib import load_yaml, check_freshness, now_iso, set_status_everywhere  # noqa: E402
from score_leads import (  # noqa: E402
    score_prospect, score_with_completeness, compute_completeness, compute_potential_score,
)
from qualify_leads import needs_enrichment  # noqa: E402
from verify_business import apply_verification  # noqa: E402
from verify_contact import apply_contact, NEVER_VERIFIED_SOURCE_TYPES  # noqa: E402
from rescore_leads import find_ranking_match, best_position, domain_of  # noqa: E402
from qa_email import apply_qa_guards  # noqa: E402

SCORING_CFG = load_yaml("scoring.yaml")
NICHES_CFG = load_yaml("niches.yaml")
LIMITS_CFG = load_yaml("limits.yaml")


def base_prospect(**overrides):
    p = {
        "id": "test-1", "business_name": "Test Co", "city": "Charlotte", "state": "NC",
        "niche": "roofing", "website": "https://example.com",
        "maps_position": None, "organic_position": None, "rating": None, "review_count": None,
        "obvious_website_issue": [], "obvious_gbp_issue": [], "service_page_count": None,
        "competitor_gap": [], "commercial_value_signal": "high", "verified_business": True,
    }
    p.update(overrides)
    return p


class TestUnknownRankIsNotBadRank(unittest.TestCase):
    """Missing data must never score the same as a confirmed weakness."""

    def test_missing_service_page_count_is_not_penalized(self):
        p = base_prospect(service_page_count=None)
        score, breakdown = score_prospect(p, SCORING_CFG, NICHES_CFG)
        self.assertNotIn("weak_service_pages", breakdown)

    def test_known_thin_service_pages_is_still_penalized(self):
        p = base_prospect(service_page_count=1)  # roofing typical=6, threshold=3
        score, breakdown = score_prospect(p, SCORING_CFG, NICHES_CFG)
        self.assertIn("weak_service_pages", breakdown)

    def test_missing_maps_and_organic_dont_subtract_points(self):
        p = base_prospect(maps_position=None, organic_position=None, service_page_count=6)
        score, breakdown = score_prospect(p, SCORING_CFG, NICHES_CFG)
        self.assertNotIn("maps_position_4_to_15", breakdown)
        self.assertNotIn("organic_position_5_to_30", breakdown)
        self.assertGreaterEqual(score, 0)  # never negative just from missing data


class TestDataCompleteness(unittest.TestCase):
    def test_fully_known_prospect_is_100_percent_complete(self):
        p = base_prospect(
            maps_position=8, organic_position=12, review_count=20, rating=4.5,
            service_page_count=3, competitor_gap=["gap"],
        )
        completeness, missing = compute_completeness(p, market={"review_benchmarks": {"median_top3": 40}})
        self.assertEqual(completeness, 100)
        self.assertEqual(missing, [])

    def test_mostly_unknown_prospect_has_low_completeness(self):
        p = base_prospect()  # everything null/empty
        completeness, missing = compute_completeness(p, market=None)
        self.assertLess(completeness, 50)
        self.assertIn("maps_position", missing)
        self.assertIn("organic_position", missing)

    def test_potential_score_assumes_favorable_missing_fields(self):
        p = base_prospect(service_page_count=None, maps_position=None, organic_position=None)
        confirmed, breakdown = score_prospect(p, SCORING_CFG, NICHES_CFG)
        _, missing = compute_completeness(p, market=None)
        potential, _ = compute_potential_score(breakdown, SCORING_CFG["weights"], missing)
        self.assertGreater(potential, confirmed)


class TestNeedsEnrichmentRouting(unittest.TestCase):
    def test_low_confirmed_high_potential_missing_rank_routes_to_enrichment(self):
        p = {"confirmed_score": 45, "potential_score": 85, "missing_fields": ["maps_position", "organic_position"]}
        self.assertTrue(needs_enrichment(p, SCORING_CFG["thresholds"]))

    def test_already_qualified_does_not_need_enrichment(self):
        p = {"confirmed_score": 75, "potential_score": 90, "missing_fields": ["maps_position"]}
        self.assertFalse(needs_enrichment(p, SCORING_CFG["thresholds"]))

    def test_low_potential_does_not_need_enrichment_even_if_missing_data(self):
        p = {"confirmed_score": 30, "potential_score": 50, "missing_fields": ["maps_position", "organic_position"]}
        self.assertFalse(needs_enrichment(p, SCORING_CFG["thresholds"]))

    def test_missing_non_material_field_does_not_trigger_enrichment(self):
        # potential clears the bar, but only via a field enrichment can't fix here (review_count)
        p = {"confirmed_score": 60, "potential_score": 80, "missing_fields": ["review_count"]}
        self.assertFalse(needs_enrichment(p, SCORING_CFG["thresholds"]))


class TestRescoreMatching(unittest.TestCase):
    def test_domain_of_normalizes_url(self):
        self.assertEqual(domain_of("https://www.Example.com/page"), "example.com")
        self.assertIsNone(domain_of(None))

    def test_find_ranking_match_by_domain(self):
        rows = [{"domain": "example.com", "business_name": "Something Else", "organic_position": "9"}]
        matches = find_ranking_match(rows, "Test Co", "https://example.com")
        self.assertEqual(len(matches), 1)

    def test_find_ranking_match_by_name_substring(self):
        rows = [{"domain": "unrelated.com", "business_name": "Test Co Roofing LLC"}]
        matches = find_ranking_match(rows, "Test Co", None)
        self.assertEqual(len(matches), 1)

    def test_best_position_takes_minimum(self):
        rows = [{"organic_position": "12"}, {"organic_position": "5"}, {"organic_position": ""}]
        self.assertEqual(best_position(rows, "organic_position"), 5)

    def test_best_position_ignores_unverified_absence_rows(self):
        """An exact_rank_verified: false row must never contribute a number, even if one is present."""
        rows = [{"organic_position": "21", "exact_rank_verified": "False"}]
        self.assertIsNone(best_position(rows, "organic_position"))

    def test_best_position_defaults_to_verified_for_legacy_rows(self):
        rows = [{"organic_position": "9"}]  # no exact_rank_verified column at all
        self.assertEqual(best_position(rows, "organic_position"), 9)

    def test_rescore_moves_lead_toward_qualification(self):
        p = base_prospect(
            service_page_count=1, competitor_gap=["gap"], verified_business=True,
            maps_position=None, organic_position=None,
        )
        before, _ = score_prospect(p, SCORING_CFG, NICHES_CFG)
        p["maps_position"] = 7
        p["organic_position"] = 12
        after, _ = score_prospect(p, SCORING_CFG, NICHES_CFG)
        self.assertGreater(after, before)


class TestBusinessVerification(unittest.TestCase):
    def test_unverified_business_is_rejected(self):
        p = base_prospect()
        outcome = apply_verification(p, {
            "business_verified": False, "identity_confidence": 0.9,
            "matched_fields": [], "conflicting_fields": [], "source_notes": [],
        }, LIMITS_CFG["min_identity_confidence"])
        self.assertEqual(outcome, "REJECTED")
        self.assertEqual(p["status"], "REJECTED")

    def test_low_confidence_with_conflict_goes_to_manual_review_not_scoring(self):
        """The 'Example Restoration' name-collision case: don't silently guess which business it is."""
        p = base_prospect()
        outcome = apply_verification(p, {
            "business_verified": True, "identity_confidence": 0.4,
            "matched_fields": ["phone"], "conflicting_fields": ["two businesses with this name in this state"],
            "source_notes": [],
        }, LIMITS_CFG["min_identity_confidence"])
        self.assertEqual(outcome, "MANUAL_REVIEW")

    def test_high_confidence_verified_business_proceeds(self):
        p = base_prospect()
        outcome = apply_verification(p, {
            "business_verified": True, "identity_confidence": 0.9,
            "matched_fields": ["phone", "address"], "conflicting_fields": [], "source_notes": [],
        }, LIMITS_CFG["min_identity_confidence"])
        self.assertEqual(outcome, "BUSINESS_VERIFIED")
        self.assertEqual(p["status"], "BUSINESS_VERIFIED")


class TestContactVerification(unittest.TestCase):
    def test_guessed_email_can_never_be_verified(self):
        contact = {
            "email": "info@example.com", "source_type": "guessed",
            "verification_confidence": 0.95, "contact_verified": True,
        }
        status = apply_contact(contact, LIMITS_CFG["min_contact_confidence"])
        self.assertEqual(status, "CONTACT_UNVERIFIED")
        self.assertFalse(contact["contact_verified"])

    def test_no_source_url_cannot_be_verified(self):
        contact = {
            "email": "owner@example.com", "source_type": "company_website",
            "source_url": None, "verification_confidence": 0.95, "contact_verified": True,
        }
        status = apply_contact(contact, LIMITS_CFG["min_contact_confidence"])
        self.assertEqual(status, "CONTACT_UNVERIFIED")

    def test_low_confidence_sourced_email_stays_unverified(self):
        contact = {
            "email": "owner@example.com", "source_type": "company_website",
            "source_url": "https://example.com/about", "verification_confidence": 0.5,
            "contact_verified": True,
        }
        status = apply_contact(contact, LIMITS_CFG["min_contact_confidence"])
        self.assertEqual(status, "CONTACT_UNVERIFIED")

    def test_sourced_high_confidence_email_is_verified(self):
        contact = {
            "email": "owner@example.com", "source_type": "company_website",
            "source_url": "https://example.com/about", "verification_confidence": 0.9,
            "contact_verified": True,
        }
        status = apply_contact(contact, LIMITS_CFG["min_contact_confidence"])
        self.assertEqual(status, "CONTACT_VERIFIED")

    def test_never_verified_source_types_are_guessed_and_none(self):
        self.assertEqual(NEVER_VERIFIED_SOURCE_TYPES, {"guessed", "none"})


class TestStaleFindingProtection(unittest.TestCase):
    def test_fresh_dossier_with_no_ranking_claim_passes(self):
        dossier = {"observed_at": now_iso(), "maps_position": None, "organic_position": None}
        is_fresh, reasons = check_freshness(dossier, LIMITS_CFG)
        self.assertTrue(is_fresh)
        self.assertEqual(reasons, [])

    def test_old_finding_is_stale(self):
        old = "2020-01-01T00:00:00+00:00"
        dossier = {"observed_at": old, "maps_position": None, "organic_position": None}
        is_fresh, reasons = check_freshness(dossier, LIMITS_CFG)
        self.assertFalse(is_fresh)
        self.assertTrue(any("finding" in r for r in reasons))

    def test_ranking_claim_without_dated_source_is_stale(self):
        dossier = {"observed_at": now_iso(), "maps_position": 7, "organic_position": None, "ranking_observed_at": None}
        is_fresh, reasons = check_freshness(dossier, LIMITS_CFG)
        self.assertFalse(is_fresh)
        self.assertTrue(any("ranking" in r for r in reasons))

    def test_ranking_claim_with_fresh_dated_source_passes(self):
        dossier = {
            "observed_at": now_iso(), "maps_position": 7, "organic_position": None,
            "ranking_observed_at": now_iso(),
        }
        is_fresh, reasons = check_freshness(dossier, LIMITS_CFG)
        self.assertTrue(is_fresh)


class TestEmailQaGuards(unittest.TestCase):
    def test_unverified_recipient_cannot_pass(self):
        verdict = {"verdict": "PASS", "checks": {}}
        result = apply_qa_guards(verdict, contact=None)
        self.assertEqual(result["verdict"], "REJECT")

    def test_unverified_recipient_reject_routes_to_contact_unverified_not_dossier(self):
        """A stale PASS re-checked against a since-failed contact verification must route back
        to CONTACT_UNVERIFIED (needs new contact research), not DOSSIER_READY (implies the
        evidence itself needs rework, which is wrong and was a real bug caught in production)."""
        verdict = {"verdict": "PASS", "checks": {"facts_supported": True}}
        result = apply_qa_guards(verdict, contact={"contact_verified": False})
        self.assertEqual(result["verdict"], "REJECT")
        self.assertEqual(result["_status_override"], "CONTACT_UNVERIFIED")

    def test_unsupported_claim_forces_reject_not_rewrite(self):
        verdict = {"verdict": "REWRITE", "checks": {"facts_supported": False}}
        contact = {"contact_verified": True}
        result = apply_qa_guards(verdict, contact)
        self.assertEqual(result["verdict"], "REJECT")

    def test_stale_finding_forces_reverify(self):
        verdict = {"verdict": "PASS", "checks": {"facts_supported": True, "finding_fresh": False}}
        contact = {"contact_verified": True}
        result = apply_qa_guards(verdict, contact)
        self.assertEqual(result["verdict"], "REVERIFY_REQUIRED")

    def test_unsourced_ranking_claim_forces_rewrite(self):
        verdict = {"verdict": "PASS", "checks": {
            "facts_supported": True, "finding_fresh": True, "ranking_claims_sourced_and_dated": False,
        }}
        contact = {"contact_verified": True}
        result = apply_qa_guards(verdict, contact)
        self.assertEqual(result["verdict"], "REWRITE")

    def test_clean_pass_survives_all_guards(self):
        verdict = {"verdict": "PASS", "checks": {
            "facts_supported": True, "finding_fresh": True, "ranking_claims_sourced_and_dated": True,
        }}
        contact = {"contact_verified": True}
        result = apply_qa_guards(verdict, contact)
        self.assertEqual(result["verdict"], "PASS")


class TestStatusSync(unittest.TestCase):
    """
    Regression coverage for a real production bug: verify_contact.py,
    qa_email.py, and check_freshness.py each independently reimplemented
    "update status" but only wrote discovered.jsonl, silently desyncing it
    from qualified.jsonl the moment a lead progressed past initial
    qualification. set_status_everywhere() is the fix; these tests pin the
    behavior so it can't regress the same way again.
    """

    def _write_jsonl(self, path, records):
        with open(path, "w") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

    def test_updates_status_in_every_file_the_id_appears_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            prospects_dir = Path(tmp)
            self._write_jsonl(prospects_dir / "discovered.jsonl", [{"id": "x-1", "status": "QUALIFIED"}])
            self._write_jsonl(prospects_dir / "qualified.jsonl", [{"id": "x-1", "status": "QUALIFIED"}])
            self._write_jsonl(prospects_dir / "manual_review.jsonl", [])
            self._write_jsonl(prospects_dir / "needs_enrichment.jsonl", [])

            with mock.patch.object(_lib, "PROSPECTS", prospects_dir):
                set_status_everywhere("x-1", "CONTACT_UNVERIFIED")

                discovered = json.loads((prospects_dir / "discovered.jsonl").read_text().splitlines()[0])
                qualified = json.loads((prospects_dir / "qualified.jsonl").read_text().splitlines()[0])
            self.assertEqual(discovered["status"], "CONTACT_UNVERIFIED")
            self.assertEqual(qualified["status"], "CONTACT_UNVERIFIED")

    def test_does_not_touch_files_the_id_is_absent_from(self):
        with tempfile.TemporaryDirectory() as tmp:
            prospects_dir = Path(tmp)
            self._write_jsonl(prospects_dir / "discovered.jsonl", [{"id": "x-1", "status": "QUALIFIED"}])
            self._write_jsonl(prospects_dir / "qualified.jsonl", [])
            self._write_jsonl(prospects_dir / "manual_review.jsonl", [])
            self._write_jsonl(prospects_dir / "needs_enrichment.jsonl", [])

            with mock.patch.object(_lib, "PROSPECTS", prospects_dir):
                set_status_everywhere("x-1", "REVERIFY_REQUIRED")
                qualified_text = (prospects_dir / "qualified.jsonl").read_text()
            self.assertEqual(qualified_text, "")  # untouched, not corrupted into a stray record


if __name__ == "__main__":
    unittest.main()
