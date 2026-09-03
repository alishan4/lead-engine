#!/usr/bin/env python3
"""
V3.8.1 -- fully deterministic, zero-Claude "basic verification" for
discovery-only mode. Answers exactly the four questions
OPERATING-RULES.md's V3.8.1 update lists, using ONLY facts
scripts/discover_prospects.py's single shared-per-market-cell Claude call
already returned -- never a second, per-candidate Claude call:

  - Is this apparently a real business?       -> business_name present
  - Does the website/domain correspond to it? -> at least one real contact
    surface (website, phone, or a Google Business Profile URL) is present
    (this module cannot make a live network call to confirm a website
    actually resolves -- that would itself be a research operation; a
    later ChatGPT+user pass is exactly where that live check belongs)
  - What city/state does it serve?            -> city AND state present
  - What niche does it belong to?              -> niche present

This mirrors what scripts/discover_prospects.py's own filter_candidates()
ALREADY enforces deterministically before a candidate is even appended to
discovered.jsonl (independent ownership, commercial value, Google-
dependency evidence, dedup) -- this module is a defensive second gate, not
a duplicate research pass, catching the rare case where a required field
is still null/empty despite the schema saying required.

No import of claude_invoke, claude-seo, or any research module anywhere in
this file -- see tests/test_v3_8_1_discovery_only.py's static guard.
"""
from rescore_leads import domain_of

CANDIDATE_VERIFIED = "CANDIDATE_VERIFIED"
CANDIDATE_REJECTED = "CANDIDATE_REJECTED"


def verify_candidate_basic(prospect):
    """
    Pure: prospect is the already-persisted discovered.jsonl record for
    one freshly-discovered candidate (built by discover_prospects.py's
    existing to_prospect_record(), unmodified). Returns (verified: bool,
    reason: str).

    Never scores, never fabricates, never invents a fact -- purely checks
    presence of what discovery already claims to know. A candidate that
    fails this stays out of the CANDIDATES sheet and is recorded as a
    verification failure, never silently dropped.
    """
    if not (prospect.get("business_name") or "").strip():
        return False, "missing business_name"
    if not (prospect.get("niche") or "").strip():
        return False, "missing niche"
    if not (prospect.get("city") or "").strip() or not (prospect.get("state") or "").strip():
        return False, "missing city/state"
    has_contact_surface = bool(
        prospect.get("website") or prospect.get("google_business_profile_url")
    )
    if not has_contact_surface:
        return False, "no website or Google Business Profile URL -- no contact surface to verify against"
    return True, "business_name/niche/city/state present, at least one contact surface found"


def basic_business_facts(prospect):
    """
    Pure: the small, honest bag of extra facts discovery already returned
    cheaply -- exactly what CLAUDE.md/OPERATING-RULES.md call
    `basic_business_facts` on the lightweight candidate record. Never
    includes anything requiring FIT/GAP/ranking/buying-signal/contact-
    identity research (those fields simply aren't read here). Missing
    values stay null, never guessed.
    """
    return {
        "rating": prospect.get("rating"),
        "review_count": prospect.get("review_count"),
        "years_in_business": prospect.get("years_in_business"),
        "commercial_value_signal": prospect.get("commercial_value_signal"),
        "obvious_website_issue": prospect.get("obvious_website_issue") or [],
        "obvious_gbp_issue": prospect.get("obvious_gbp_issue") or [],
    }


def build_candidate_record(prospect, discovery_source, phone=None):
    """
    Pure: assembles the lightweight candidate record
    (schemas/candidate_record.schema.json) from an already-persisted,
    already basic-verified prospect record. `phone` is passed in
    separately because scripts/discover_prospects.py's to_prospect_record()
    does not currently carry it onto the shared prospect record (V2-era
    shape, unchanged here to avoid touching that well-tested code path) --
    the raw discovery candidate dict is the only place it lives before
    this point.
    """
    return {
        "lead_id": prospect["id"],
        "business_name": prospect.get("business_name"),
        "domain": domain_of(prospect.get("website")),
        "website": prospect.get("website"),
        "city": prospect.get("city"),
        "state": prospect.get("state"),
        "country": prospect.get("country") or "US",
        "niche": prospect.get("niche"),
        "phone": phone,
        "profile_url": prospect.get("google_business_profile_url"),
        "discovery_source": discovery_source,
        "discovered_at": prospect.get("discovered_at"),
        "verification_status": prospect.get("status"),
        "basic_business_facts": basic_business_facts(prospect),
    }
