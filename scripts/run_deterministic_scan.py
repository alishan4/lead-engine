#!/usr/bin/env python3
"""
V3.2 entry gate + deterministic opportunity scan. No claude-seo agent call
happens in this script -- it uses only already-verified V1/V2/V3.1/V3.1.1
evidence plus real, live, zero-LLM-cost page fetches (scripts/page_facts.py)
to build a set of OpportunityCandidate objects. If the evidence is already
strong enough (Path A), this script resolves the wedge itself and stops at
OPPORTUNITY_IDENTIFIED with ZERO agent calls. Otherwise it stops at
DETERMINISTIC_SCAN_COMPLETE / AGENT_ROUTED for route_to_specialist.py to
pick up, or NO_DEFENSIBLE_WEDGE if nothing is worth pursuing further.

Usage:
  python3 scripts/run_deterministic_scan.py --id <slug>
  python3 scripts/run_deterministic_scan.py --id <slug> --override-contactability-failed
"""
import argparse
import time
from urllib.parse import urlparse

from _lib import (
    PROSPECTS, LEADS, load_yaml, read_jsonl, write_jsonl, load_market,
    lead_dir, load_json, write_json, set_status_everywhere, now_iso, content_hash,
)
from page_facts import extract_facts, check_robots_txt, check_sitemap
from wedge_selection import select_primary_wedge, score_candidate

ELIGIBLE_STATUSES = ("QUALIFIED", "HIGH_PRIORITY")
IN_PROGRESS_STATUSES = (
    "INTELLIGENCE_ELIGIBLE", "DETERMINISTIC_SCAN_COMPLETE", "AGENT_NOT_REQUIRED",
    "AGENT_ROUTED", "SPECIALIST_ANALYSIS_COMPLETE", "SECOND_OPINION_REQUIRED",
)
BLOCKED_REASONS = {
    "NEEDS_ENRICHMENT": "required qualification evidence not yet resolved -- see V3.1 needs_enrichment.jsonl",
    "REJECTED": "REJECTED leads never enter intelligence",
    "MANUAL_REVIEW": "requires explicit human resolution first",
}


def intelligence_eligible(p, override_contactability_failed=False):
    status = p.get("status")
    if status in ELIGIBLE_STATUSES or status in IN_PROGRESS_STATUSES:
        return True, "eligible"
    if status == "CONTACTABILITY_FAILED":
        if override_contactability_failed:
            return True, "explicit override for an unusually valuable account"
        return False, "CONTACTABILITY_FAILED -- no realistic contact path; requires --override-contactability-failed"
    if status in BLOCKED_REASONS:
        return False, BLOCKED_REASONS[status]
    return False, f"status {status!r} is not eligible for V3.2 intelligence"


def domain_root(website):
    parsed = urlparse(website)
    return f"{parsed.scheme}://{parsed.netloc}"


def pick_pages_to_fetch(homepage_facts, base_url, max_pages):
    """Homepage + up to (max_pages-1) more, prioritizing an obvious service/practice hub page from real nav links."""
    urls = [base_url]
    seen = {base_url.rstrip("/")}
    candidates = []
    for link in homepage_facts.get("nav_links", []):
        href = link["href"].rstrip("/")
        if href in seen or not href.startswith(base_url.rstrip("/")):
            continue
        text = (link.get("text") or "").lower()
        priority = 0
        if any(kw in text for kw in ("service", "practice", "what we do")):
            priority = 3
        elif any(kw in text for kw in ("location", "areas we serve", "service area")):
            priority = 2
        elif any(kw in text for kw in ("about", "contact")):
            priority = 1
        if priority:
            candidates.append((priority, href))
    candidates.sort(key=lambda t: -t[0])
    for _, href in candidates:
        if href in seen:
            continue
        urls.append(href)
        seen.add(href)
        if len(urls) >= max_pages:
            break
    return urls[:max_pages]


def build_candidates(p, market, pages_facts, tech_facts):
    """
    Deterministic candidate generation. Only creates a candidate when there
    is a real, already-verified or freshly-observed reason to -- never
    merely because a check failed. Confidence/commercial_relevance/
    specificity/actionability are each justified inline, not black-box.
    """
    candidates = []
    niche = p.get("niche")
    home = pages_facts[0] if pages_facts else {}

    # COMPETITOR_GAP -- often the strongest, cheapest candidate: it's already
    # a named, specific, verified fact from V1/V2 discovery, zero new fetches.
    if p.get("competitor_gap"):
        stmt = p["competitor_gap"][0]
        candidates.append({
            "type": "COMPETITOR_GAP", "statement": stmt,
            "evidence": [{"statement": stmt, "source": None, "source_type": "market_cache", "observed_at": p.get("last_audited_at")}],
            "confidence": 0.8, "commercial_relevance": 0.75, "specificity": 0.85, "actionability": 0.6,
            "requires_specialist": False, "source_stage": "deterministic_scan",
        })

    # SERVICE_ARCHITECTURE_GAP / PRACTICE_AREA_GAP -- corroborate the stored
    # service_page_count against a REAL, fresh nav-link count from the
    # homepage just fetched.
    service_like_links = [l for l in home.get("nav_links", []) if l.get("text")]
    real_nav_count = len({l["href"] for l in service_like_links})
    niche_norm = p.get("_niche_typical_page_count")
    spc = p.get("service_page_count")
    # Matches V1/V2's exact weak_service_pages logic (score_leads.score_prospect):
    # either the explicit tag OR the numeric threshold, never numeric-only --
    # a lead already manually tagged thin_service_pages must not be skipped
    # just because its raw count sits above the generic niche-norm cutoff.
    tagged_thin = "thin_service_pages" in (p.get("obvious_website_issue") or [])
    below_norm = bool(niche_norm) and spc is not None and spc < max(2, niche_norm // 2)
    if spc is not None and (tagged_thin or below_norm):
        opp_type = "PRACTICE_AREA_GAP" if niche in ("family_law", "immigration_law", "estate_law", "personal_injury", "criminal_defense") else "SERVICE_ARCHITECTURE_GAP"
        norm_note = f" against a {niche_norm}-page norm for this niche" if niche_norm else ""
        stmt = (f"Homepage navigation shows {real_nav_count} links; only {spc} dedicated "
                f"service/practice pages exist{norm_note}.")
        candidates.append({
            "type": opp_type, "statement": stmt,
            "evidence": [
                {"statement": f"service_page_count={spc} (V1/V2 discovery, tagged thin_service_pages={tagged_thin})", "source": None, "source_type": "market_cache", "observed_at": p.get("last_audited_at")},
                {"statement": f"live homepage nav shows {real_nav_count} distinct links", "source": home.get("url"), "source_type": "deterministic_page_fetch", "observed_at": now_iso()},
            ],
            "confidence": 0.8, "commercial_relevance": 0.8, "specificity": 0.7, "actionability": 0.65,
            "requires_specialist": False, "source_stage": "deterministic_scan",
        })

    # MAPS_GAP -- only when maps_position is actually KNOWN (never invent).
    if p.get("maps_position") is not None and 4 <= p["maps_position"] <= 15:
        stmt = f"Verified Maps position #{p['maps_position']} for the primary money keyword -- inside the competitive-but-not-dominant band."
        candidates.append({
            "type": "MAPS_GAP", "statement": stmt,
            "evidence": [{"statement": stmt, "source": None, "source_type": "ranking_import", "observed_at": p.get("enrichment_observed_at")}],
            "confidence": 0.75, "commercial_relevance": 0.8, "specificity": 0.6, "actionability": 0.4,
            # Maps competitive interpretation usually benefits from seo-local's judgment.
            "requires_specialist": True, "source_stage": "deterministic_scan",
        })

    # GBP_GAP / LOCAL_AUTHORITY_GAP -- from already-flagged GBP issues.
    if p.get("obvious_gbp_issue"):
        stmt = f"GBP completeness gaps observed: {', '.join(p['obvious_gbp_issue'])}."
        candidates.append({
            "type": "GBP_GAP", "statement": stmt,
            "evidence": [{"statement": stmt, "source": p.get("google_business_profile_url"), "source_type": "market_cache", "observed_at": p.get("last_audited_at")}],
            "confidence": 0.7, "commercial_relevance": 0.6, "specificity": 0.55, "actionability": 0.7,
            "requires_specialist": False, "source_stage": "deterministic_scan",
        })

    # REVIEW_GAP -- from review_count vs market benchmark (already computed in GAP breakdown).
    benchmark = (market or {}).get("review_benchmarks", {}).get("median_top3")
    if p.get("review_count") is not None and benchmark and p["review_count"] < benchmark:
        stmt = f"{p['review_count']} reviews vs. a {benchmark}-review market benchmark for top local competitors."
        candidates.append({
            "type": "REVIEW_GAP", "statement": stmt,
            "evidence": [{"statement": stmt, "source": None, "source_type": "market_cache", "observed_at": (market or {}).get("observed_at")}],
            "confidence": 0.75, "commercial_relevance": 0.5, "specificity": 0.75, "actionability": 0.3,
            "requires_specialist": False, "source_stage": "deterministic_scan",
        })

    # TECHNICAL_INDEXATION_GAP -- real, fresh, deterministic checks. Kept
    # deliberately LOWER commercial_relevance than an architecture/competitor
    # wedge even when confidently true -- technical severity != commercial
    # wedge strength (explicit spec example: a missing sitemap).
    tech_issues = []
    if not tech_facts["sitemap"]["exists"]:
        tech_issues.append("no sitemap.xml found")
    if home.get("has_noindex"):
        tech_issues.append("homepage carries a noindex directive")
    if not home.get("https"):
        tech_issues.append("homepage is not served over HTTPS")
    if tech_issues:
        stmt = "; ".join(tech_issues)
        candidates.append({
            "type": "TECHNICAL_INDEXATION_GAP", "statement": stmt,
            "evidence": [{"statement": stmt, "source": home.get("url"), "source_type": "deterministic_page_fetch", "observed_at": now_iso()}],
            "confidence": 0.9, "commercial_relevance": 0.35, "specificity": 0.6, "actionability": 0.8,
            "requires_specialist": False, "source_stage": "deterministic_scan",
        })

    # SCHEMA_GAP -- real, fresh, deterministic.
    if not home.get("schema_types"):
        stmt = "No schema.org structured data (JSON-LD) detected on the homepage."
        candidates.append({
            "type": "SCHEMA_GAP", "statement": stmt,
            "evidence": [{"statement": stmt, "source": home.get("url"), "source_type": "deterministic_page_fetch", "observed_at": now_iso()}],
            "confidence": 0.9, "commercial_relevance": 0.25, "specificity": 0.5, "actionability": 0.85,
            "requires_specialist": False, "source_stage": "deterministic_scan",
        })

    # CONVERSION_GAP -- real, fresh, deterministic phone/form presence check.
    if not home.get("phone_found") and not home.get("has_contact_form"):
        stmt = "No phone number or contact form detected on the homepage."
        candidates.append({
            "type": "CONVERSION_GAP", "statement": stmt,
            "evidence": [{"statement": stmt, "source": home.get("url"), "source_type": "deterministic_page_fetch", "observed_at": now_iso()}],
            "confidence": 0.7, "commercial_relevance": 0.65, "specificity": 0.55, "actionability": 0.7,
            "requires_specialist": False, "source_stage": "deterministic_scan",
        })

    # PAID_OWNED_VISIBILITY_GAP -- only from a real, VERIFIED+ resolved
    # V3.1.1 signal, never derived here independently.
    if p.get("paid_search_organic_gap") is True:
        stmt = "Confirmed paid acquisition (Ads/LSA) running alongside a verified organic-visibility gap."
        candidates.append({
            "type": "PAID_OWNED_VISIBILITY_GAP", "statement": stmt,
            "evidence": [{"statement": stmt, "source": None, "source_type": "buying_signal_evidence", "observed_at": None}],
            "confidence": 0.75, "commercial_relevance": 0.85, "specificity": 0.7, "actionability": 0.5,
            "requires_specialist": True, "source_stage": "deterministic_scan",
        })

    if not candidates:
        candidates.append({
            "type": "NO_CLEAR_OPPORTUNITY", "statement": "No deterministic-evidence-backed opportunity found.",
            "evidence": [], "confidence": 0.0, "commercial_relevance": 0.0, "specificity": 0.0,
            "actionability": 0.0, "requires_specialist": False, "source_stage": "deterministic_scan",
        })
    return candidates


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    ap.add_argument("--override-contactability-failed", action="store_true")
    args = ap.parse_args()

    discovered = read_jsonl(PROSPECTS / "discovered.jsonl")
    p = next((r for r in discovered if r["id"] == args.id), None)
    if not p:
        raise SystemExit(f"Prospect {args.id} not found in discovered.jsonl")

    eligible, reason = intelligence_eligible(p, args.override_contactability_failed)
    if not eligible:
        print(f"{args.id}: NOT ELIGIBLE for V3.2 intelligence -- {reason}")
        return

    # Remember the ORIGINAL qualification tier (QUALIFIED vs HIGH_PRIORITY)
    # before status moves through the V3.2 states -- route_to_specialist.py's
    # agent-budget/second-opinion gate needs this even after `status` itself
    # has advanced past it.
    qualification_tier = p["status"] if p["status"] in ELIGIBLE_STATUSES else p.get("qualification_tier", "QUALIFIED")
    set_status_everywhere(args.id, "INTELLIGENCE_ELIGIBLE", extra_fields={"qualification_tier": qualification_tier})

    limits = load_yaml("limits.yaml")
    niches_cfg = load_yaml("niches.yaml")
    router_cfg = load_yaml("opportunity_router.yaml")
    market = load_market(p.get("niche"), p.get("city"), p.get("state"))

    if not p.get("website"):
        set_status_everywhere(args.id, "INTELLIGENCE_FAILED")
        print(f"{args.id}: INTELLIGENCE_FAILED -- no website on record, cannot scan")
        return

    base = domain_root(p["website"])
    t0 = time.time()

    home_facts = extract_facts(base + "/")
    if home_facts["status"] != 200:
        set_status_everywhere(args.id, "INTELLIGENCE_FAILED")
        print(f"{args.id}: INTELLIGENCE_FAILED -- homepage unreachable (status={home_facts['status']}, error={home_facts['fetch_error']})")
        return

    new_hash = content_hash(home_facts.get("title"), home_facts.get("meta_description"), len(home_facts.get("h1") or []))
    cache_hit = (
        p.get("intelligence_content_hash") == new_hash
        and load_json(lead_dir(args.id) / "opportunity_candidates.json") is not None
    )

    if cache_hit:
        candidates = load_json(lead_dir(args.id) / "opportunity_candidates.json")["candidates"]
        pages_fetched = 1  # homepage was fetched once to compute/compare the content hash
        print(f"{args.id}: intelligence CACHE HIT (content unchanged) -- reusing prior scan, "
              f"only the homepage was re-fetched to confirm the hash")
    else:
        urls = pick_pages_to_fetch(home_facts, base + "/", limits["max_intelligence_pages"])
        pages_facts = [home_facts] + [extract_facts(u) for u in urls[1:]]
        pages_fetched = len(pages_facts)
        tech_facts = {"robots": check_robots_txt(base), "sitemap": check_sitemap(base)}

        niche_cfg = (niches_cfg.get("niches") or {}).get(p.get("niche") or "", {})
        p["_niche_typical_page_count"] = len(niche_cfg.get("typical_service_pages") or [])
        candidates = build_candidates(p, market, pages_facts, tech_facts)

    duration = round(time.time() - t0, 2)
    set_status_everywhere(args.id, "DETERMINISTIC_SCAN_COMPLETE", extra_fields={
        "intelligence_content_hash": new_hash, "intelligence_cache_hit": cache_hit,
    })

    cost = {
        "deterministic_checks_count": len(candidates), "pages_fetched": pages_fetched,
        "competitor_pages_fetched": 0, "specialist_calls": 0, "second_opinion_calls": 0,
        "cache_hits": 1 if cache_hit else 0, "cache_misses": 0 if cache_hit else 1,
        "estimated_input_tokens": None, "estimated_output_tokens": None,
        "intelligence_duration_seconds": duration,
    }

    qual = load_json(lead_dir(args.id) / "qualification_v3.json") or {"prospect_id": args.id}
    qual["intelligence_cost"] = cost
    write_json(lead_dir(args.id) / "qualification_v3.json", qual)
    write_json(lead_dir(args.id) / "opportunity_candidates.json", {"prospect_id": args.id, "candidates": candidates, "generated_at": now_iso()})

    # Routing decision: Path A (deterministic-only pool) vs Path B (needs a specialist) vs NO_DEFENSIBLE_WEDGE.
    deterministic_only = [c for c in candidates if not c.get("requires_specialist")]
    known_terms = [c["name"] for c in (market or {}).get("top_competitors", []) if isinstance(c, dict) and c.get("name")]
    weights = router_cfg["wedge_weights"]

    best, score, why = select_primary_wedge(deterministic_only, known_terms, weights, min_confidence=limits["usable_confidence_threshold"])

    if best:
        set_status_everywhere(args.id, "AGENT_NOT_REQUIRED")
        wedge = finalize_wedge(p, best, score, agents_used=[])
        write_json(lead_dir(args.id) / "primary_wedge.json", wedge)
        set_status_everywhere(args.id, "OPPORTUNITY_IDENTIFIED", extra_fields={
            "primary_wedge_type": wedge["opportunity_type"], "primary_wedge_confidence": wedge["confidence"],
            "intelligence_agents_used": [],
        })
        print(f"{args.id}: AGENT_NOT_REQUIRED -> OPPORTUNITY_IDENTIFIED (0 agents) -- {wedge['opportunity_type']}, wedge_score={score}")
        return

    needs_specialist = [c for c in candidates if c.get("requires_specialist")]
    if needs_specialist:
        set_status_everywhere(args.id, "AGENT_ROUTED")
        print(f"{args.id}: DETERMINISTIC_SCAN_COMPLETE -- deterministic evidence alone was insufficient ({why}). "
              f"AGENT_ROUTED: run scripts/route_to_specialist.py --id {args.id} --print-context next.")
        return

    set_status_everywhere(args.id, "NO_DEFENSIBLE_WEDGE", extra_fields={"no_defensible_wedge_reason": why})
    print(f"{args.id}: NO_DEFENSIBLE_WEDGE -- {why}. No agent was called; nothing to route.")


DEFAULT_RECOMMENDED_ACTION = {
    "COMPETITOR_GAP": "Publish a dedicated landing page for the specific service/intent the named competitors already cover.",
    "SERVICE_ARCHITECTURE_GAP": "Split the highest-value service off the generic page into its own dedicated, indexable page.",
    "PRACTICE_AREA_GAP": "Split the highest-value practice area off the generic page into its own dedicated, indexable page.",
    "MAPS_GAP": "Complete and actively maintain the Google Business Profile listing for this location.",
    "GBP_GAP": "Fill in the specific missing GBP fields/photos/categories identified in the evidence.",
    "REVIEW_GAP": "Start a lightweight post-job review-request workflow to close the gap with top competitors.",
    "TECHNICAL_INDEXATION_GAP": "Add the specific missing technical element (sitemap, HTTPS, indexability directive) identified in the evidence.",
    "SCHEMA_GAP": "Add LocalBusiness/Service schema.org markup to the homepage and key service pages.",
    "CONVERSION_GAP": "Make the phone number and/or a contact form clearly visible above the fold on the homepage.",
    "ENTITY_NAP_GAP": "Ensure name/address/phone are consistent and machine-readable across the site and directory listings.",
    "PAID_OWNED_VISIBILITY_GAP": "Redirect a portion of paid spend toward the specific organic page gap identified.",
    "ORGANIC_VISIBILITY_GAP": "Target the specific underperforming page/keyword identified with focused on-page improvements.",
    "LOCAL_AUTHORITY_GAP": "Pursue the specific local citation/authority gap identified relative to named competitors.",
}


def finalize_wedge(p, candidate, score, agents_used):
    from wedge_selection import commercial_mechanism_is_defensible
    mechanism = candidate.get("commercial_mechanism") or candidate["statement"]
    ok, why = commercial_mechanism_is_defensible(mechanism)
    if not ok:
        mechanism = candidate["statement"]  # fall back to the raw, already-vetted statement
    return {
        "opportunity_type": candidate["type"],
        "observation": candidate["statement"],
        "commercial_mechanism": mechanism,
        "recommended_first_action": candidate.get("recommended_action")
            or DEFAULT_RECOMMENDED_ACTION.get(candidate["type"], "Address the specific gap identified in the evidence above."),
        "why_this_company": f"{p.get('business_name')} is a {p.get('niche')} business in {p.get('city')}, {p.get('state')}.",
        "why_this_problem": candidate["statement"],
        "why_now": p.get("why_now"),  # copied verbatim from V3.1.1 -- never recomputed here
        "why_likely_buyer": p.get("why_likely_buyer") or "Commercial-readiness indicators from qualification apply.",
        "evidence": candidate["evidence"],
        "confidence": candidate["confidence"],
        "specificity_score": candidate["specificity"],
        "commercial_relevance_score": candidate["commercial_relevance"],
        "wedge_score": score,
        "agents_used": agents_used,
        "passed_company_swap_test": True,
        "selected_at": now_iso(),
    }


if __name__ == "__main__":
    main()
