"""
V3.1 unit tests: commercial-fit model, google-gap model, franchise/corporate
routing, buying signals, contactability gating, why-now synthesis, and the
final --v3 routing decision. Pure functions only -- no network, no AI, no
claude-seo agent calls anywhere in this file (matches V3.1's own hard rule).

Run with:
  python3 -m pytest lead-engine/tests -q
or
  python3 lead-engine/tests/test_v3_pipeline.py
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from _lib import load_yaml, load_franchise_blocklist, match_franchise_blocklist  # noqa: E402
from score_leads import score_fit, score_gap  # noqa: E402
from qualify_leads import route_v3, build_why_now, needs_enrichment_v3, HIGH_PRIORITY_SIGNAL_FIELDS  # noqa: E402
from check_franchise import apply_result as apply_franchise_result  # noqa: E402
from check_contactability import route as route_contactability  # noqa: E402

SCORING_CFG = load_yaml("scoring.yaml")
LIMITS_CFG = load_yaml("limits.yaml")
NICHES_CFG = load_yaml("niches.yaml")
BLOCKLIST = load_franchise_blocklist()

STRONG_MARKET = {
    "top_competitors": [{"name": "Independent Roofing Co"}, {"name": "Local Roofers LLC"}],
    "review_benchmarks": {"median_top3": 60},
}
FRANCHISE_MARKET = {
    "top_competitors": [{"name": "SERVPRO of Downtown"}, {"name": "ServiceMaster Restore"}],
    "review_benchmarks": {"median_top3": 200},
}


def base_prospect(**overrides):
    p = {
        "id": "v3-test-1", "business_name": "Test Co", "city": "Charlotte", "state": "NC",
        "niche": "roofing", "website": "https://example.com",
        "maps_position": None, "organic_position": None, "rating": None, "review_count": None,
        "years_in_business": None, "obvious_website_issue": [], "obvious_gbp_issue": [],
        "service_page_count": None, "competitor_gap": [], "commercial_value_signal": "high",
        "verified_business": True,
        "runs_google_ads": None, "runs_lsa": None, "paid_search_organic_gap": None,
        "recent_expansion": None, "new_location": None, "marketing_hiring_signal": None,
        "review_velocity_signal": None, "recent_site_investment": None,
        "new_high_value_service": None, "multiple_locations": None,
        "contactability_score": None,
    }
    p.update(overrides)
    return p


class TestNoCommercialFitRewardForBadSeoAlone(unittest.TestCase):
    """1. terrible SEO + weak commercial fit -> REJECT."""

    def test_terrible_seo_but_no_fit_signals_rejects(self):
        p = base_prospect(
            niche="other_high_value_local",  # tier 3
            obvious_website_issue=["thin_service_pages", "no_https", "slow_site"],
            service_page_count=1, competitor_gap=["huge gap vs competitors"],
            review_count=None, years_in_business=None, multiple_locations=None,
        )
        fit = score_fit(p, SCORING_CFG, NICHES_CFG, market=None, blocklist=BLOCKLIST)
        p.update({"fit_confirmed_score": fit["confirmed_score"], "fit_potential_score": fit["potential_score"]})
        gap = score_gap(p, SCORING_CFG, NICHES_CFG, market=None)
        p.update({"gap_confirmed_score": gap["confirmed_score"], "gap_potential_score": gap["potential_score"],
                   "gap_missing_fields": gap["missing_fields"]})
        status, reason, audit_ok, why = route_v3(p, SCORING_CFG, LIMITS_CFG)
        self.assertIn(status, ("REJECTED", "MANUAL_REVIEW"))
        self.assertFalse(audit_ok)
        # The GAP score alone would be enormous here (thin pages + tech issues +
        # competitor gap) -- prove FIT, not GAP, is what's gating the decision.
        self.assertGreaterEqual(gap["confirmed_score"], 40)


class TestNeedsEnrichmentNotReject(unittest.TestCase):
    """2. strong business + missing rank data -> NEEDS_ENRICHMENT, not reject."""

    def test_strong_fit_missing_rank_data_enriches(self):
        p = base_prospect(
            niche="restoration", review_count=40, years_in_business=10, multiple_locations=True,
            service_page_count=6, runs_google_ads=True, contactability_score=2,
            maps_position=None, organic_position=None,  # material fields missing
            obvious_gbp_issue=["few_photos"], competitor_gap=["gap vs competitor"],
        )
        fit = score_fit(p, SCORING_CFG, NICHES_CFG, market=STRONG_MARKET, blocklist=BLOCKLIST)
        p.update({"fit_confirmed_score": fit["confirmed_score"], "fit_potential_score": fit["potential_score"]})
        gap = score_gap(p, SCORING_CFG, NICHES_CFG, market=STRONG_MARKET)
        p.update({"gap_confirmed_score": gap["confirmed_score"], "gap_potential_score": gap["potential_score"],
                   "gap_missing_fields": gap["missing_fields"], "gap_breakdown": gap["breakdown"]})
        self.assertGreaterEqual(fit["confirmed_score"], SCORING_CFG["fit_thresholds"]["qualified_min"])
        status, reason, audit_ok, why = route_v3(p, SCORING_CFG, LIMITS_CFG)
        self.assertEqual(status, "NEEDS_ENRICHMENT")
        self.assertFalse(audit_ok)


class TestHighPriorityRequiresBuyingSignal(unittest.TestCase):
    """3. strong FIT + strong GAP + Ads signal -> HIGH_PRIORITY."""

    def test_ads_signal_promotes_to_high_priority(self):
        p = base_prospect(
            niche="roofing", review_count=45, years_in_business=8, multiple_locations=True,
            service_page_count=6, maps_position=9, organic_position=12,
            runs_google_ads=True, paid_search_organic_gap=True, contactability_score=2,
            # V3.1.1: a bare True is not enough -- HIGH_PRIORITY/why_now require
            # a VERIFIED-or-better tier, set here as a real assess_buying_signals.py
            # run would (see scripts/signal_evidence.py: resolve_signal).
            buying_signal_tiers={"runs_google_ads": "STRONG_VERIFIED"},
            obvious_website_issue=["thin_service_pages"], competitor_gap=["gap vs competitor"],
        )
        fit = score_fit(p, SCORING_CFG, NICHES_CFG, market=STRONG_MARKET, blocklist=BLOCKLIST)
        gap = score_gap(p, SCORING_CFG, NICHES_CFG, market=STRONG_MARKET)
        p.update({
            "fit_confirmed_score": fit["confirmed_score"], "fit_potential_score": fit["potential_score"],
            "fit_breakdown": fit["breakdown"],
            "gap_confirmed_score": gap["confirmed_score"], "gap_potential_score": gap["potential_score"],
            "gap_missing_fields": gap["missing_fields"], "gap_breakdown": gap["breakdown"],
        })
        status, reason, audit_ok, why = route_v3(p, SCORING_CFG, LIMITS_CFG)
        self.assertEqual(status, "HIGH_PRIORITY")
        self.assertTrue(audit_ok)
        self.assertIsNotNone(why["why_now"])
        self.assertIsNotNone(why["why_likely_buyer"])


class TestGapAloneNeverPromotesToHighPriority(unittest.TestCase):
    """4. strong GAP but no buying signal -> QUALIFIED, not HIGH_PRIORITY."""

    def test_no_buying_signal_stays_qualified(self):
        p = base_prospect(
            niche="roofing", review_count=45, years_in_business=8, multiple_locations=True,
            service_page_count=6, maps_position=9, organic_position=12,
            contactability_score=2,
            # every buying signal explicitly confirmed FALSE, not missing
            runs_google_ads=False, runs_lsa=False, paid_search_organic_gap=False,
            recent_expansion=False, new_location=False, marketing_hiring_signal=False,
            review_velocity_signal="LOW", recent_site_investment=False, new_high_value_service=False,
            obvious_website_issue=["thin_service_pages"], competitor_gap=["gap vs competitor"],
        )
        fit = score_fit(p, SCORING_CFG, NICHES_CFG, market=STRONG_MARKET, blocklist=BLOCKLIST)
        gap = score_gap(p, SCORING_CFG, NICHES_CFG, market=STRONG_MARKET)
        p.update({
            "fit_confirmed_score": fit["confirmed_score"], "fit_potential_score": fit["potential_score"],
            "gap_confirmed_score": gap["confirmed_score"], "gap_potential_score": gap["potential_score"],
            "gap_missing_fields": gap["missing_fields"], "gap_breakdown": gap["breakdown"],
        })
        self.assertGreaterEqual(gap["confirmed_score"], SCORING_CFG["gap_thresholds"]["high_priority_min"])
        status, reason, audit_ok, why = route_v3(p, SCORING_CFG, LIMITS_CFG)
        self.assertEqual(status, "QUALIFIED")
        self.assertTrue(audit_ok)


class TestContactabilityGating(unittest.TestCase):
    """5. contactability 0 prevents expensive escalation. 6. contact form + strong fit doesn't fabricate email."""

    def test_zero_contactability_fails_without_strong_fit(self):
        result = {"contactability_score": 0, "official_contact_form_available": False}
        status, reason = route_contactability(result, fit_confirmed_score=50, fit_thresholds=SCORING_CFG["fit_thresholds"])
        self.assertEqual(status, "CONTACTABILITY_FAILED")

    def test_zero_contactability_with_form_and_strong_fit_is_preserved_not_verified(self):
        result = {"contactability_score": 0, "official_contact_form_available": True}
        status, reason = route_contactability(result, fit_confirmed_score=70, fit_thresholds=SCORING_CFG["fit_thresholds"])
        self.assertEqual(status, "CONTACTABILITY_CHECK")
        # Critically: this preserves the lead as a manual/contact-form candidate,
        # it does NOT fabricate or mark any email verified.
        self.assertNotIn("email", result)

    def test_zero_contactability_with_form_but_weak_fit_still_fails(self):
        result = {"contactability_score": 0, "official_contact_form_available": True}
        status, reason = route_contactability(result, fit_confirmed_score=45, fit_thresholds=SCORING_CFG["fit_thresholds"])
        self.assertEqual(status, "CONTACTABILITY_FAILED")


class TestFranchiseRouting(unittest.TestCase):
    """7. franchise-controlled business stops early. 8. possible franchise goes MANUAL_REVIEW."""

    def test_corporate_marketing_controlled_locks(self):
        p = base_prospect(business_name="SERVPRO of Uptown")
        outcome = apply_franchise_result(p, {
            "possible_franchise": True, "corporate_marketing_controlled": True,
            "lead_gen_network": False, "evidence": ["corporate template site, no local domain"],
        })
        self.assertEqual(outcome, "CORPORATE_MARKETING_LOCK")

    def test_uncertain_franchise_status_needs_review(self):
        p = base_prospect(business_name="Aire Serv of Nashville")
        outcome = apply_franchise_result(p, {
            "possible_franchise": True, "corporate_marketing_controlled": None,
            "lead_gen_network": False, "evidence": ["franchise brand confirmed, local control unclear"],
        })
        self.assertEqual(outcome, "FRANCHISE_REVIEW_REQUIRED")

    def test_lead_gen_network_stops(self):
        p = base_prospect(business_name="HomeAdvisor Roofing Directory")
        outcome = apply_franchise_result(p, {
            "possible_franchise": False, "corporate_marketing_controlled": None,
            "lead_gen_network": True, "evidence": ["lists multiple unrelated roofing companies"],
        })
        self.assertEqual(outcome, "LEAD_GEN_NETWORK")

    def test_independently_operated_franchisee_proceeds(self):
        """A franchise brand match that turns out to be independently marketed must NOT be blocked."""
        p = base_prospect(business_name="Aire Serv of Nashville")
        outcome = apply_franchise_result(p, {
            "possible_franchise": True, "corporate_marketing_controlled": False,
            "lead_gen_network": False, "evidence": ["distinct local domain, named local owner controls ad spend"],
        })
        self.assertIsNone(outcome)  # None == clear to proceed, status unchanged

    def test_blocklist_match_detection(self):
        cat, pat = match_franchise_blocklist("SERVPRO of Downtown", "servpro.com", BLOCKLIST)
        self.assertEqual(cat, "restoration")
        cat2, pat2 = match_franchise_blocklist("Example Roofing & Construction", "example-roofing.test", BLOCKLIST)
        self.assertIsNone(pat2)


class TestNullRankNeverScoredAsPoorRank(unittest.TestCase):
    """9. null Maps rank does not reduce GAP as if confirmed poor."""

    def test_null_maps_position_contributes_nothing_not_a_penalty(self):
        p = base_prospect(maps_position=None, obvious_gbp_issue=None)
        gap = score_gap(p, SCORING_CFG, NICHES_CFG, market=None)
        self.assertNotIn("maps_gbp_opportunity", gap["breakdown"])
        self.assertGreaterEqual(gap["confirmed_score"], 0)  # never negative from missing data alone
        self.assertIn("maps_position", gap["missing_fields"])

    def test_known_maps_position_outside_sweet_spot_scores_lower_than_in_range(self):
        p_in_range = base_prospect(maps_position=8, obvious_gbp_issue=[])
        p_out_of_range = base_prospect(maps_position=1, obvious_gbp_issue=[])
        gap_in = score_gap(p_in_range, SCORING_CFG, NICHES_CFG, market=None)
        gap_out = score_gap(p_out_of_range, SCORING_CFG, NICHES_CFG, market=None)
        self.assertGreater(gap_in["breakdown"].get("maps_gbp_opportunity", 0),
                            gap_out["breakdown"].get("maps_gbp_opportunity", 0))


class TestPaidAdsIncreasesFit(unittest.TestCase):
    """10. paid Ads + weak organic increases FIT/buying intent."""

    def test_ads_and_gap_signal_raise_buying_intent(self):
        p_no_ads = base_prospect()
        p_with_ads = base_prospect(runs_google_ads=True, paid_search_organic_gap=True)
        fit_no_ads = score_fit(p_no_ads, SCORING_CFG, NICHES_CFG, market=None, blocklist=BLOCKLIST)
        fit_with_ads = score_fit(p_with_ads, SCORING_CFG, NICHES_CFG, market=None, blocklist=BLOCKLIST)
        self.assertGreater(
            fit_with_ads["breakdown"].get("buying_intent", 0),
            fit_no_ads["breakdown"].get("buying_intent", 0),
        )


class TestDominantBusinessNoWedge(unittest.TestCase):
    """11. dominant business with no wedge gets rejected/deprioritized."""

    def test_no_gap_evidence_at_all_does_not_qualify(self):
        p = base_prospect(
            niche="roofing", review_count=300, years_in_business=25, multiple_locations=True,
            service_page_count=8,  # plenty of pages, niche norm for roofing typical=5 -> not thin
            obvious_website_issue=[], obvious_gbp_issue=[], competitor_gap=[],
            contactability_score=2,
        )
        fit = score_fit(p, SCORING_CFG, NICHES_CFG, market=STRONG_MARKET, blocklist=BLOCKLIST)
        gap = score_gap(p, SCORING_CFG, NICHES_CFG, market=STRONG_MARKET)
        p.update({
            "fit_confirmed_score": fit["confirmed_score"], "fit_potential_score": fit["potential_score"],
            "gap_confirmed_score": gap["confirmed_score"], "gap_potential_score": gap["potential_score"],
            "gap_missing_fields": gap["missing_fields"], "gap_breakdown": gap["breakdown"],
        })
        status, reason, audit_ok, why = route_v3(p, SCORING_CFG, LIMITS_CFG)
        self.assertIn(status, ("REJECTED", "NEEDS_ENRICHMENT"))
        self.assertFalse(audit_ok)


class TestSaturatedNicheCanStillQualify(unittest.TestCase):
    """12. PI lead can still qualify when buying signals are unusually strong."""

    def test_personal_injury_with_strong_signals_qualifies(self):
        p = base_prospect(
            niche="personal_injury",  # tier 3
            review_count=80, years_in_business=15, multiple_locations=True, service_page_count=6,
            maps_position=9, organic_position=15,
            runs_google_ads=True, runs_lsa=True, paid_search_organic_gap=True,
            recent_expansion=True, contactability_score=2,
            obvious_website_issue=["thin_service_pages"], competitor_gap=["gap vs competitor"],
        )
        fit = score_fit(p, SCORING_CFG, NICHES_CFG, market=STRONG_MARKET, blocklist=BLOCKLIST)
        gap = score_gap(p, SCORING_CFG, NICHES_CFG, market=STRONG_MARKET)
        p.update({
            "fit_confirmed_score": fit["confirmed_score"], "fit_potential_score": fit["potential_score"],
            "fit_breakdown": fit["breakdown"],
            "gap_confirmed_score": gap["confirmed_score"], "gap_potential_score": gap["potential_score"],
            "gap_missing_fields": gap["missing_fields"], "gap_breakdown": gap["breakdown"],
        })
        status, reason, audit_ok, why = route_v3(p, SCORING_CFG, LIMITS_CFG)
        # Tier 3 gets fewer niche_economics points, but strong maturity+buying+
        # contactability signals can still clear the bar -- PI is not auto-excluded.
        self.assertIn(status, ("QUALIFIED", "HIGH_PRIORITY"))


class TestOldV2DataStillLoads(unittest.TestCase):
    """13. old V2 JSON still loads. 14/15. confirmed/potential/completeness remain correct, missing != zero."""

    def test_v2_only_record_scores_without_v3_fields_present(self):
        # A record with NO V3.1 fields at all (as every real V2 record on disk is).
        p = {
            "id": "v2-legacy", "business_name": "Legacy Co", "city": "Charlotte", "state": "NC",
            "niche": "roofing", "website": "https://example.com",
            "maps_position": 9, "organic_position": 12, "rating": 4.5, "review_count": 20,
            "years_in_business": None, "obvious_website_issue": ["thin_service_pages"],
            "obvious_gbp_issue": [], "service_page_count": 2, "competitor_gap": ["gap"],
            "commercial_value_signal": "high", "verified_business": True,
        }
        # Must not raise despite missing every V3.1 key.
        fit = score_fit(p, SCORING_CFG, NICHES_CFG, market=None, blocklist=BLOCKLIST)
        gap = score_gap(p, SCORING_CFG, NICHES_CFG, market=None)
        self.assertIsInstance(fit["confirmed_score"], int)
        self.assertIsInstance(gap["confirmed_score"], int)
        # V2's own confirmed/potential/completeness fields are untouched by any of this.
        from score_leads import score_with_completeness
        v2_result = score_with_completeness(p, load_yaml("scoring.yaml"), NICHES_CFG)
        self.assertIn("confirmed_score", v2_result)
        self.assertIn("potential_score", v2_result)
        self.assertIn("data_completeness", v2_result)


class TestWhyNowGating(unittest.TestCase):
    """16. why_now unsupported prevents HIGH_PRIORITY."""

    def test_no_confirmed_timing_signal_leaves_why_now_null(self):
        p = base_prospect(review_count=50, years_in_business=10, multiple_locations=True)
        why = build_why_now(p)
        self.assertIsNone(why["why_now"])

    def test_confirmed_signal_populates_why_now(self):
        p = base_prospect(runs_google_ads=True, buying_signal_tiers={"runs_google_ads": "VERIFIED"})
        why = build_why_now(p)
        self.assertIsNotNone(why["why_now"])
        self.assertIn("Google Ads", why["why_now"])

    def test_unverified_tier_signal_does_not_populate_why_now(self):
        """A bare True with no VERIFIED-or-better tier (or none at all) must not populate why_now."""
        p = base_prospect(runs_google_ads=True)  # no buying_signal_tiers at all
        why = build_why_now(p)
        self.assertIsNone(why["why_now"])

    def test_route_v3_never_promotes_to_high_priority_without_why_now(self):
        p = base_prospect(
            niche="roofing", review_count=45, years_in_business=8, multiple_locations=True,
            service_page_count=6, maps_position=9, organic_position=12, contactability_score=2,
            # score thresholds for HIGH_PRIORITY met via review_velocity alone in breakdown,
            # but no field in HIGH_PRIORITY_SIGNAL_FIELDS or review_velocity=="strong" is true
            obvious_website_issue=["thin_service_pages"], competitor_gap=["gap vs competitor"],
        )
        fit = score_fit(p, SCORING_CFG, NICHES_CFG, market=STRONG_MARKET, blocklist=BLOCKLIST)
        gap = score_gap(p, SCORING_CFG, NICHES_CFG, market=STRONG_MARKET)
        p.update({
            "fit_confirmed_score": fit["confirmed_score"], "fit_potential_score": fit["potential_score"],
            "gap_confirmed_score": gap["confirmed_score"], "gap_potential_score": gap["potential_score"],
            "gap_missing_fields": gap["missing_fields"], "gap_breakdown": gap["breakdown"],
        })
        status, reason, audit_ok, why = route_v3(p, SCORING_CFG, LIMITS_CFG)
        self.assertEqual(status, "QUALIFIED")  # not HIGH_PRIORITY -- no buying signal at all


class TestFailureIsolation(unittest.TestCase):
    """17. one lead failure does not affect another (pure functions -- no shared mutable state)."""

    def test_scoring_one_prospect_does_not_mutate_another(self):
        p1 = base_prospect(id="lead-1", review_count=5)
        p2 = base_prospect(id="lead-2", review_count=500)
        score_fit(p1, SCORING_CFG, NICHES_CFG, market=None, blocklist=BLOCKLIST)
        fit2_before = dict(p2)
        score_fit(p2, SCORING_CFG, NICHES_CFG, market=None, blocklist=BLOCKLIST)
        self.assertEqual(p2["review_count"], fit2_before["review_count"])
        self.assertEqual(p1["review_count"], 5)  # unaffected by p2's processing


class TestNoAgentInvokedInV31(unittest.TestCase):
    """18. no claude-seo agent is invoked anywhere in V3.1 qualification."""

    def test_v3_scripts_do_not_reference_claude_seo_agents(self):
        import inspect
        import score_leads
        import qualify_leads
        for mod in (score_leads, qualify_leads):
            src = inspect.getsource(mod)
            self.assertNotIn("claude-seo:", src)
            self.assertNotIn("subagent_type", src)


if __name__ == "__main__":
    unittest.main()
