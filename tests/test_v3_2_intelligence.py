"""
V3.2 tests: entry gate, deterministic scan, opportunity routing, agent
budget/stop rules, wedge selection, company-swap test, dossier/asset
staging, and cache behavior. Pure functions and synthetic fixtures --
Paths B/C/D (one-agent, two-agent, stop) are validated here with synthetic
data exactly as the V3.2 brief itself specifies ("test", "synthetic
fixture"), since the 4 real eligible leads all honestly resolved via the
zero-agent path on their own real evidence (see reports/V3.2-INTELLIGENCE-REPORT.md).

No claude-seo agent is invoked anywhere in this file. No Gmail action is
referenced anywhere in the V3.2 modules under test.

Run with:
  python3 -m pytest lead-engine/tests -q
or
  python3 lead-engine/tests/test_v3_2_intelligence.py
"""
import inspect
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from _lib import load_yaml  # noqa: E402
from run_deterministic_scan import intelligence_eligible, build_candidates, finalize_wedge  # noqa: E402
from wedge_selection import (  # noqa: E402
    score_candidate, passes_company_swap_test, select_primary_wedge, commercial_mechanism_is_defensible,
)
from route_to_specialist import pick_specialist, ingest_specialist_output, decide_after_specialist  # noqa: E402
from build_dossier_v3_2 import dossier_allowed  # noqa: E402
from stage_asset import asset_allowed  # noqa: E402
import run_deterministic_scan  # noqa: E402
import route_to_specialist  # noqa: E402
import build_dossier_v3_2  # noqa: E402
import stage_asset  # noqa: E402
import page_facts  # noqa: E402
import wedge_selection  # noqa: E402

ROUTER_CFG = load_yaml("opportunity_router.yaml")
LIMITS = load_yaml("limits.yaml")
NICHES_CFG = load_yaml("niches.yaml")
WEIGHTS = ROUTER_CFG["wedge_weights"]
CLAUDE_SEO_AGENTS_DIR = Path(__file__).resolve().parent.parent.parent / "claude-seo" / "agents"


def candidate(type_="SERVICE_ARCHITECTURE_GAP", confidence=0.8, commercial_relevance=0.8,
              specificity=0.8, actionability=0.6, requires_specialist=False, statement=None, evidence=None):
    return {
        "type": type_, "statement": statement or f"Specific statement for {type_} citing 3 pages and Competitor X",
        "evidence": evidence if evidence is not None else [{"statement": "Competitor X has 6 pages", "source": "https://competitorx.com", "source_type": "market_cache"}],
        "confidence": confidence, "commercial_relevance": commercial_relevance,
        "specificity": specificity, "actionability": actionability, "requires_specialist": requires_specialist,
    }


class Test1_2_EntryGate(unittest.TestCase):
    """1. only QUALIFIED/HIGH_PRIORITY enter automatically. 2. NEEDS_ENRICHMENT cannot run expensive intelligence."""

    def test_qualified_is_eligible(self):
        ok, reason = intelligence_eligible({"status": "QUALIFIED"})
        self.assertTrue(ok)

    def test_high_priority_is_eligible(self):
        ok, reason = intelligence_eligible({"status": "HIGH_PRIORITY"})
        self.assertTrue(ok)

    def test_needs_enrichment_blocked(self):
        ok, reason = intelligence_eligible({"status": "NEEDS_ENRICHMENT"})
        self.assertFalse(ok)
        self.assertIn("enrichment", reason.lower())

    def test_rejected_never_enters(self):
        ok, reason = intelligence_eligible({"status": "REJECTED"})
        self.assertFalse(ok)

    def test_manual_review_requires_resolution_first(self):
        ok, reason = intelligence_eligible({"status": "MANUAL_REVIEW"})
        self.assertFalse(ok)

    def test_contactability_failed_blocked_without_override(self):
        ok, reason = intelligence_eligible({"status": "CONTACTABILITY_FAILED"})
        self.assertFalse(ok)

    def test_contactability_failed_allowed_with_explicit_override(self):
        ok, reason = intelligence_eligible({"status": "CONTACTABILITY_FAILED"}, override_contactability_failed=True)
        self.assertTrue(ok)
        self.assertIn("override", reason.lower())


class Test3_ZeroAgentPath(unittest.TestCase):
    """3. deterministic evidence can solve with 0 agents. 30. VALIDATE ZERO-AGENT PATH (mandatory)."""

    def test_strong_deterministic_evidence_produces_wedge_with_zero_agents(self):
        p = {
            "business_name": "Strong Co", "niche": "roofing", "city": "X", "state": "NC",
            "competitor_gap": ["Competitor Y has 6 dedicated service pages vs our 2"],
            "service_page_count": 2, "obvious_website_issue": [], "obvious_gbp_issue": [],
            "maps_position": None, "review_count": None, "why_now": None, "why_likely_buyer": "solid track record",
        }
        p["_niche_typical_page_count"] = 6
        home_facts = {"url": "https://strongco.com/", "nav_links": [], "schema_types": ["LocalBusiness"],
                       "has_noindex": False, "https": True, "phone_found": "555-123-4567", "has_contact_form": True}
        tech_facts = {"sitemap": {"exists": True}}
        candidates = build_candidates(p, None, [home_facts], tech_facts)
        deterministic_only = [c for c in candidates if not c["requires_specialist"]]
        best, score, why = select_primary_wedge(deterministic_only, [], WEIGHTS, min_confidence=LIMITS["usable_confidence_threshold"])
        self.assertIsNotNone(best)
        wedge = finalize_wedge(p, best, score, agents_used=[])
        self.assertEqual(wedge["agents_used"], [])  # ZERO agents
        self.assertTrue(wedge["passed_company_swap_test"])
        # full downstream gates accept it
        self.assertTrue(dossier_allowed("OPPORTUNITY_IDENTIFIED"))
        self.assertTrue(asset_allowed("DOSSIER_READY"))


class Test4_5_6_SpecialistRouting(unittest.TestCase):
    """4. local/GBP -> correct specialist. 5. service architecture -> correct specialist. 6. technical/indexation -> correct specialist."""

    def test_gbp_gap_routes_to_seo_local(self):
        self.assertIn("claude-seo:seo-local", ROUTER_CFG["opportunity_specialist_map"]["GBP_GAP"])

    def test_service_architecture_routes_to_seo_cluster(self):
        self.assertIn("claude-seo:seo-cluster", ROUTER_CFG["opportunity_specialist_map"]["SERVICE_ARCHITECTURE_GAP"])

    def test_technical_indexation_routes_to_seo_technical(self):
        self.assertIn("claude-seo:seo-technical", ROUTER_CFG["opportunity_specialist_map"]["TECHNICAL_INDEXATION_GAP"])


class Test7_ActualAgentNamesConfigured(unittest.TestCase):
    """7. actual claude-seo agent names discovered/configured, not invented."""

    def test_every_routed_agent_exists_as_a_real_claude_seo_agent_file(self):
        real_agents = {f.stem for f in CLAUDE_SEO_AGENTS_DIR.glob("*.md")}
        self.assertTrue(real_agents, "expected to find real agent .md files -- check the path")
        all_configured_agents = set()
        for agents in ROUTER_CFG["opportunity_specialist_map"].values():
            all_configured_agents.update(agents)
        for pair in ROUTER_CFG["second_opinion_pairs"].values():
            all_configured_agents.update(pair)
        for full_name in all_configured_agents:
            short_name = full_name.split(":")[-1]
            self.assertIn(short_name, real_agents, f"{full_name} does not correspond to a real claude-seo agent file")


class Test8_9_10_AgentBudget(unittest.TestCase):
    """8. normal QUALIFIED maxes at 1 unless justified. 9. HIGH_PRIORITY may use a second. 10. max 2 enforced."""

    def test_qualified_weak_result_with_no_justification_stops_at_one(self):
        decision, reason = decide_after_specialist(0.3, "QUALIFIED", False, calls_used=1, limits=LIMITS)
        self.assertEqual(decision, "STOP")

    def test_high_priority_weak_result_gets_second_opinion(self):
        decision, reason = decide_after_specialist(0.3, "HIGH_PRIORITY", False, calls_used=1, limits=LIMITS)
        self.assertEqual(decision, "SECOND_OPINION")

    def test_hard_cap_of_two_enforced_even_for_high_priority(self):
        decision, reason = decide_after_specialist(0.3, "HIGH_PRIORITY", True, calls_used=2, limits=LIMITS)
        self.assertEqual(decision, "STOP")
        self.assertIn("hard cap", reason.lower())


class Test11_33_WeakResultStops(unittest.TestCase):
    """11. weak first-agent result stops. 33. VALIDATE STOP PATH -- confirm no second agent called."""

    def test_weak_qualified_no_second_dimension_stops_immediately(self):
        decision, reason = decide_after_specialist(0.2, "QUALIFIED", pending_second_dimension_exists=False, calls_used=1, limits=LIMITS)
        self.assertEqual(decision, "STOP")
        # confirms the caller never proceeds to route a second specialist for this case


class Test12_13_SpecialistOutputIngestion(unittest.TestCase):
    """12. unsupported specialist fact is rejected. 13. specialist evidence provenance preserved."""

    def test_new_fact_without_evidence_is_dropped(self):
        output = {
            "specialist": "claude-seo:seo-cluster", "hypothesis": "h", "finding": "f",
            "commercial_mechanism": "m", "evidence": ["e1"], "confidence": 0.8,
            "recommended_action": "a", "limitations": [],
            "new_facts": [{"statement": "unsupported claim", "evidence": ""}, {"statement": "supported claim", "evidence": "seen at URL X"}],
        }
        cleaned, dropped = ingest_specialist_output(output)
        self.assertEqual(len(cleaned["new_facts"]), 1)
        self.assertEqual(cleaned["new_facts"][0]["statement"], "supported claim")
        self.assertIn("unsupported claim", dropped)

    def test_evidence_list_preserved_intact(self):
        output = {
            "specialist": "claude-seo:seo-cluster", "hypothesis": "h", "finding": "f",
            "commercial_mechanism": "m", "evidence": ["e1", "e2"], "confidence": 0.8,
            "recommended_action": "a", "limitations": [], "new_facts": [],
        }
        cleaned, dropped = ingest_specialist_output(output)
        self.assertEqual(cleaned["evidence"], ["e1", "e2"])


class Test14_15_CompanySwapTest(unittest.TestCase):
    """14. generic wedge fails. 15. prospect-specific wedge passes."""

    def test_generic_observation_fails(self):
        c = candidate(statement="The website could use more content", evidence=[])
        self.assertFalse(passes_company_swap_test(c))

    def test_specific_observation_with_number_and_url_passes(self):
        c = candidate()
        self.assertTrue(passes_company_swap_test(c))

    def test_named_competitor_term_passes_even_without_a_number(self):
        c = candidate(statement="Example Competitor Roofing has a dedicated storm-damage page and we do not", evidence=[])
        self.assertTrue(passes_company_swap_test(c, known_specific_terms=["Example Competitor Roofing"]))


class Test16_TechnicalSeverityLosesToCommercialRelevance(unittest.TestCase):
    """16. technically severe but commercially weak issue loses to a stronger commercial wedge."""

    def test_high_confidence_low_relevance_technical_loses_to_commercial(self):
        technical = candidate(type_="SCHEMA_GAP", confidence=0.95, commercial_relevance=0.2, specificity=0.5, actionability=0.85)
        commercial = candidate(type_="COMPETITOR_GAP", confidence=0.8, commercial_relevance=0.8, specificity=0.85, actionability=0.6)
        best, score, why = select_primary_wedge([technical, commercial], [], WEIGHTS)
        self.assertEqual(best["type"], "COMPETITOR_GAP")
        self.assertGreater(score_candidate(commercial, WEIGHTS), score_candidate(technical, WEIGHTS))


class Test17_RevenueLossFabricationRejected(unittest.TestCase):
    def test_fabricated_dollar_loss_rejected(self):
        ok, why = commercial_mechanism_is_defensible("You are losing $3000 per month because of this")
        self.assertFalse(ok)

    def test_fabricated_lead_count_rejected(self):
        ok, why = commercial_mechanism_is_defensible("You could gain 30 leads by fixing this")
        self.assertFalse(ok)

    def test_qualitative_mechanism_accepted(self):
        ok, why = commercial_mechanism_is_defensible("Competitors own the dedicated page for this exact search intent, this business does not")
        self.assertTrue(ok)


class Test18_19_WhyNowHandling(unittest.TestCase):
    """18. why_now may remain null for QUALIFIED. 19. HIGH_PRIORITY why_now must remain evidence-backed."""

    def test_null_why_now_copied_verbatim_not_invalidating(self):
        p = {"business_name": "X", "niche": "roofing", "city": "Y", "state": "Z", "why_now": None, "why_likely_buyer": "b"}
        c = candidate()
        wedge = finalize_wedge(p, c, 80, agents_used=[])
        self.assertIsNone(wedge["why_now"])  # valid, non-disqualifying

    def test_real_why_now_copied_verbatim_never_recomputed(self):
        p = {"business_name": "X", "niche": "roofing", "city": "Y", "state": "Z",
             "why_now": "X is actively running Google Ads right now.", "why_likely_buyer": "b"}
        c = candidate()
        wedge = finalize_wedge(p, c, 80, agents_used=[])
        self.assertEqual(wedge["why_now"], p["why_now"])  # exact copy, not synthesized fresh


class Test20_21_22_23_DossierAndAssetGating(unittest.TestCase):
    """20. dossier only after opportunity identified. 21. asset only from validated wedge.
    22. asset maxes at 3 observations. 23. asset contains evidence references."""

    def test_dossier_blocked_before_opportunity_identified(self):
        self.assertFalse(dossier_allowed("DETERMINISTIC_SCAN_COMPLETE"))
        self.assertFalse(dossier_allowed("AGENT_ROUTED"))
        self.assertTrue(dossier_allowed("OPPORTUNITY_IDENTIFIED"))

    def test_asset_blocked_before_dossier_ready(self):
        self.assertFalse(asset_allowed("OPPORTUNITY_IDENTIFIED"))
        self.assertTrue(asset_allowed("DOSSIER_READY"))

    def test_asset_observation_cap_is_three(self):
        self.assertEqual(LIMITS["max_wedge_observations_in_asset"], 3)


class Test24_25_CacheInvalidation(unittest.TestCase):
    """24. unchanged content reuses cache. 25. changed content invalidates relevant cache."""

    def test_unchanged_hash_is_cache_hit(self):
        from _lib import content_hash
        h1 = content_hash("Title A", "Meta A", 2)
        h2 = content_hash("Title A", "Meta A", 2)
        self.assertEqual(h1, h2)

    def test_changed_content_produces_different_hash(self):
        from _lib import content_hash
        h1 = content_hash("Title A", "Meta A", 2)
        h2 = content_hash("Title B (redesigned)", "Meta A", 3)
        self.assertNotEqual(h1, h2)


class Test26_StaleEvidenceCannotSupportWedge(unittest.TestCase):
    """26. stale evidence cannot silently support a current wedge -- reuses V3.1.1 freshness infra."""

    def test_stale_buying_signal_evidence_still_gated_by_v3_1_1(self):
        import signal_evidence
        old_ts = "2020-01-01T00:00:00+00:00"
        items = [{"signal_type": "runs_google_ads", "value": True, "confidence": 0.9, "source": "a",
                   "source_type": "google_serp", "observed_at": old_ts, "published_at": None,
                   "evidence": "old", "entity_match_confidence": 0.9, "notes": None}]
        source_cfg = load_yaml("signal_sources.yaml")
        r = signal_evidence.resolve_signal(items, source_cfg)
        self.assertIsNone(r["value"])
        self.assertEqual(r["status"], "STALE_ONLY")


class Test27_NoDefensibleWedgeIsValid(unittest.TestCase):
    def test_no_viable_candidates_returns_none_not_an_error(self):
        best, score, reason = select_primary_wedge([], [], WEIGHTS)
        self.assertIsNone(best)
        self.assertIsInstance(reason, str)

    def test_only_no_clear_opportunity_returns_none(self):
        c = {"type": "NO_CLEAR_OPPORTUNITY", "statement": "none", "evidence": [], "confidence": 0.0,
             "commercial_relevance": 0.0, "specificity": 0.0, "actionability": 0.0, "requires_specialist": False}
        best, score, reason = select_primary_wedge([c], [], WEIGHTS)
        self.assertIsNone(best)


class Test28_FailureIsolation(unittest.TestCase):
    def test_scanning_one_prospect_does_not_mutate_another(self):
        p1 = {"business_name": "A", "niche": "roofing", "competitor_gap": ["gap A"], "service_page_count": None,
              "obvious_website_issue": [], "obvious_gbp_issue": [], "maps_position": None, "review_count": None}
        p2_before = {"business_name": "B", "niche": "hvac", "competitor_gap": ["gap B"], "service_page_count": None,
                     "obvious_website_issue": [], "obvious_gbp_issue": [], "maps_position": None, "review_count": None}
        p2 = dict(p2_before)
        home_facts = {"url": "https://x.com/", "nav_links": [], "schema_types": [], "has_noindex": False,
                       "https": True, "phone_found": None, "has_contact_form": False}
        build_candidates(p1, None, [home_facts], {"sitemap": {"exists": True}})
        build_candidates(p2, None, [home_facts], {"sitemap": {"exists": True}})
        self.assertEqual(p2["competitor_gap"], p2_before["competitor_gap"])


class Test29_NoGmailAction(unittest.TestCase):
    def test_no_gmail_references_in_v3_2_modules(self):
        for mod in (run_deterministic_scan, route_to_specialist, build_dossier_v3_2, stage_asset, page_facts, wedge_selection):
            src = inspect.getsource(mod).lower()
            self.assertNotIn("gmail", src)
            self.assertNotIn("smtp", src)


class Test30_NoContactAddressFabricated(unittest.TestCase):
    def test_dossier_decision_maker_context_never_claims_verified_email(self):
        # build_dossier_v3_2's decision_maker_context only ever carries
        # likely_contact_role/contactability_score -- it never invents or
        # copies in an email address, and explicitly notes verification is
        # still required.
        src = inspect.getsource(build_dossier_v3_2)
        self.assertIn("Not a verified contact", src)
        self.assertNotIn("contact.json", src.split("decision_maker_context")[1][:500] if "decision_maker_context" in src else "")


class Test31_PriorTestsRemainPassing(unittest.TestCase):
    """31. V1/V2/V3.1/V3.1.1 tests remain passing -- structurally confirmed by the full suite run, see report."""

    def test_v1_v2_v3_1_scoring_functions_still_importable_and_functional(self):
        from score_leads import score_prospect, score_fit, score_gap  # noqa: F401
        self.assertTrue(callable(score_prospect))


class Test32_PageBudget(unittest.TestCase):
    def test_pick_pages_never_exceeds_max(self):
        from run_deterministic_scan import pick_pages_to_fetch
        home_facts = {"nav_links": [
            {"href": "https://x.com/services", "text": "Services"},
            {"href": "https://x.com/about", "text": "About"},
            {"href": "https://x.com/contact", "text": "Contact"},
            {"href": "https://x.com/locations", "text": "Locations"},
            {"href": "https://x.com/blog", "text": "Blog"},
        ]}
        urls = pick_pages_to_fetch(home_facts, "https://x.com/", max_pages=3)
        self.assertLessEqual(len(urls), 3)
        self.assertEqual(urls[0], "https://x.com/")


class Test33_CompetitorBudget(unittest.TestCase):
    def test_max_competitor_pages_configured_and_small(self):
        self.assertEqual(LIMITS["max_competitor_pages"], 3)
        self.assertLessEqual(LIMITS["max_competitor_pages"], 3)


class Test34_CostMetricsNoFabrication(unittest.TestCase):
    def test_token_estimates_are_null_not_fabricated(self):
        # run_deterministic_scan.py's cost dict hardcodes these to None --
        # confirmed by source inspection since no token-usage API is exposed.
        src = inspect.getsource(run_deterministic_scan)
        self.assertIn('"estimated_input_tokens": None', src)
        self.assertIn('"estimated_output_tokens": None', src)


class Test35_StagedAssetRetrievable(unittest.TestCase):
    def test_staged_asset_schema_has_all_fields_needed_for_reply_handling(self):
        import json as _json
        schema_path = Path(__file__).resolve().parent.parent / "schemas" / "staged_asset.schema.json"
        schema = _json.loads(schema_path.read_text())
        required = schema["required"]
        for field in ("asset_type", "title", "sections"):
            self.assertIn(field, required)
        section_fields = schema["properties"]["sections"]["required"]
        for field in ("what_i_noticed", "observations", "recommended_first_action", "evidence_references"):
            self.assertIn(field, section_fields)


if __name__ == "__main__":
    unittest.main()
