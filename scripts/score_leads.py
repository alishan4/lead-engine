#!/usr/bin/env python3
"""
Rule-based lead scoring. Runs BEFORE any claude-seo agent is invoked.

Reads data/prospects/discovered.jsonl (or --in), scores every record with
status DISCOVERED or BUSINESS_VERIFIED (V2: business verification should run
first, see verify_business.py) using config/scoring.yaml, and writes the
result back with status INITIAL_SCORE (or REJECTED for hard-reject rules)
plus score/score_breakdown/confirmed_score/potential_score/data_completeness/
missing_fields. No network calls, no AI calls -- pure arithmetic over the
prospect record and any cached market file.

V2 data-completeness model (see reports/V2-*.md): `score` /
`score_breakdown` (via score_prospect) only ever count fields that are
actually KNOWN -- a null maps_position contributes zero points, it never
counts as a strike against the lead. `potential_score` answers "what could
this lead score if every currently-missing high-value field turned out
favorable?" -- qualify_leads.py uses the gap between confirmed and potential
to decide whether a lead is genuinely weak vs. simply under-measured
(NEEDS_ENRICHMENT).

Usage:
  python3 scripts/score_leads.py
  python3 scripts/score_leads.py --in data/prospects/discovered.jsonl
"""
import argparse
import sys

from _lib import (
    PROSPECTS, load_yaml, read_jsonl, write_jsonl, load_market, now_iso,
    match_franchise_blocklist, load_franchise_blocklist,
)


def is_dominant(p):
    return (
        (p.get("maps_position") or 99) <= 3
        and (p.get("organic_position") or 99) <= 4
        and (p.get("rating") or 0) >= 4.7
        and (p.get("review_count") or 0) >= 50
    )


def hard_reject_reason(p, rules):
    issues = p.get("obvious_website_issue") or []
    if rules.get("broken_or_non_legitimate") and (
        "site_down" in issues or p.get("verified_business") is False
    ):
        return "broken_or_non_legitimate"
    if rules.get("no_commercial_intent") and p.get("commercial_value_signal") == "none":
        return "no_commercial_intent"
    return None


def score_prospect(p, cfg, niches_cfg):
    weights = cfg["weights"]
    penalties = cfg["penalties"]
    breakdown = {}
    score = 0

    maps_pos = p.get("maps_position")
    if maps_pos is not None and 4 <= maps_pos <= 15:
        breakdown["maps_position_4_to_15"] = weights["maps_position_4_to_15"]

    org_pos = p.get("organic_position")
    if org_pos is not None and 5 <= org_pos <= 30:
        breakdown["organic_position_5_to_30"] = weights["organic_position_5_to_30"]

    issues = set(p.get("obvious_website_issue") or [])
    niche_cfg = (niches_cfg.get("niches") or {}).get(p.get("niche") or "", {})
    typical_pages = len(niche_cfg.get("typical_service_pages") or [])
    spc = p.get("service_page_count")
    # IMPORTANT (V2 fix): only compare against the niche norm when
    # service_page_count is actually KNOWN. `None` means "not measured yet",
    # not "zero pages" -- treating it as 0 would silently penalize missing
    # data as if it were a confirmed weakness (see data-completeness model).
    weak_pages = "thin_service_pages" in issues or (
        bool(typical_pages) and spc is not None and spc < max(2, typical_pages // 2)
    )
    if weak_pages:
        breakdown["weak_service_pages"] = weights["weak_service_pages"]

    conversion_issues = {"no_online_booking", "weak_cta", "no_visible_phone", "no_contact_form"}
    if issues & conversion_issues:
        breakdown["weak_website_conversion"] = weights["weak_website_conversion"]

    if p.get("competitor_gap"):
        breakdown["clear_competitor_gap"] = weights["clear_competitor_gap"]

    market = load_market(p.get("niche"), p.get("city"), p.get("state"))
    review_benchmark = None
    if market:
        review_benchmark = market.get("review_benchmarks", {}).get("median_top3")
    low_reviews = False
    if review_benchmark and p.get("review_count") is not None:
        low_reviews = p["review_count"] < review_benchmark
    elif p.get("review_count") is not None:
        low_reviews = p["review_count"] < 40  # fallback heuristic, no market cache yet
    if low_reviews:
        breakdown["low_moderate_reviews"] = weights["low_moderate_reviews"]

    if niche_cfg.get("high_value"):
        breakdown["high_value_niche"] = weights["high_value_niche"]

    if p.get("verified_business"):
        breakdown["verified_business"] = weights["verified_business"]

    if "public_contact_discoverable" not in issues and (p.get("website") or p.get("google_business_profile_url")):
        breakdown["public_contact_discoverable"] = weights["public_contact_discoverable"]

    if is_dominant(p):
        breakdown["already_dominant"] = penalties["already_dominant"]

    score = sum(breakdown.values())
    score = max(0, min(100, score))
    return score, breakdown


# Fields tracked by the V2 data-completeness model. Each missing one both
# lowers `data_completeness` and is a candidate to bump `potential_score`
# (see compute_potential_score) if it maps to a scoring weight.
COMPLETENESS_FIELDS = [
    "maps_position", "organic_position", "review_count", "rating",
    "service_page_count", "competitor_gap", "competitor_benchmark",
    "contact_information",
]


def compute_completeness(p, market):
    missing = []
    for field in ("maps_position", "organic_position", "review_count", "rating", "service_page_count"):
        if p.get(field) is None:
            missing.append(field)
    if not p.get("competitor_gap"):
        missing.append("competitor_gap")
    if not (market and (market.get("review_benchmarks") or {}).get("median_top3")):
        missing.append("competitor_benchmark")
    if not (p.get("website") or p.get("google_business_profile_url")):
        missing.append("contact_information")

    present = len(COMPLETENESS_FIELDS) - len(missing)
    completeness = round(100 * present / len(COMPLETENESS_FIELDS))
    return completeness, missing


def compute_potential_score(breakdown, weights, missing_fields):
    """
    Best-case score if every currently-missing high-value field turned out
    favorable (e.g. maps_position lands 4-15, review_count turns out low
    relative to the market). This is what qualify_leads.py compares against
    confirmed_score to distinguish "genuinely weak" from "under-measured".
    """
    potential = dict(breakdown)
    field_to_weight = {
        "maps_position": "maps_position_4_to_15",
        "organic_position": "organic_position_5_to_30",
        "service_page_count": "weak_service_pages",
        "review_count": "low_moderate_reviews",
        "competitor_gap": "clear_competitor_gap",
        "contact_information": "public_contact_discoverable",
    }
    for field in missing_fields:
        weight_key = field_to_weight.get(field)
        if weight_key and weight_key not in potential:
            potential[weight_key] = weights[weight_key]
    score = max(0, min(100, sum(potential.values())))
    return score, potential


def score_with_completeness(p, cfg, niches_cfg):
    """Full V2 scoring result: confirmed + potential + completeness, in one call."""
    confirmed_score, breakdown = score_prospect(p, cfg, niches_cfg)
    market = load_market(p.get("niche"), p.get("city"), p.get("state"))
    completeness, missing_fields = compute_completeness(p, market)
    potential_score, potential_breakdown = compute_potential_score(breakdown, cfg["weights"], missing_fields)
    return {
        "confirmed_score": confirmed_score,
        "potential_score": potential_score,
        "data_completeness": completeness,
        "missing_fields": missing_fields,
        "score_breakdown": breakdown,
        "potential_breakdown": potential_breakdown,
    }


# ============================================================================
# V3.1 ADDITIONS -- FIT and GAP scoring. Separate axes from the V2 `score`/
# `confirmed_score`/`potential_score` above, used only by
# scripts/assess_commercial_fit.py, scripts/assess_google_gap.py, and
# `qualify_leads.py --v3`. Nothing above this line is changed or affected.
#
# FIT answers "would this be a commercially attractive client?" -- it does
# NOT reward a lead merely because its SEO/GAP is bad. GAP answers "is
# there a defensible Google/local acquisition opportunity?" using the same
# confirmed/potential/completeness discipline as V2: unknown ranking data
# is never scored as poor ranking.
# ============================================================================

TECHNICAL_ISSUE_TAGS = {"no_https", "slow_site", "no_schema_markup", "broken_links"}


def score_fit(p, cfg, niches_cfg, market, blocklist=None):
    """Commercial-fit score: niche economics + maturity + buying intent + contactability + market attractiveness."""
    weights = cfg["fit_weights"]
    tier_points = cfg["niche_tier_points"]
    breakdown = {}
    missing = []
    blocklist = blocklist if blocklist is not None else load_franchise_blocklist()

    # 1. Niche economics -- tier lookup from niches.yaml, essentially always
    # known once a niche is assigned at discovery time.
    niche_cfg = (niches_cfg.get("niches") or {}).get(p.get("niche") or "")
    if niche_cfg and niche_cfg.get("tier") in tier_points:
        breakdown["niche_economics"] = tier_points[niche_cfg["tier"]]
    else:
        missing.append("niche_tier")

    # 2. Business maturity -- sum of independently-known signals, each only
    # contributing when its field is actually known (never penalize null).
    maturity_pts = 0
    maturity_known_any = False
    if p.get("review_count") is not None:
        maturity_known_any = True
        if p["review_count"] >= 10:
            maturity_pts += 6
    else:
        missing.append("review_count")
    if p.get("years_in_business") is not None:
        maturity_known_any = True
        if p["years_in_business"] >= 3:
            maturity_pts += 6
    else:
        missing.append("years_in_business")
    if p.get("multiple_locations") is not None:
        maturity_known_any = True
        if p["multiple_locations"]:
            maturity_pts += 4
    else:
        missing.append("multiple_locations")
    if p.get("service_page_count") is not None:
        maturity_known_any = True
        if p["service_page_count"] >= 3:
            maturity_pts += 4
    else:
        missing.append("service_page_count")
    if maturity_known_any:
        breakdown["business_maturity"] = min(weights["business_maturity"], maturity_pts)

    # 3. Buying intent -- from assess_buying_signals.py's fields, already on
    # the prospect record by the time this runs in the normal pipeline order.
    buying_pts = 0
    buying_known_any = False
    if p.get("runs_google_ads") is not None or p.get("runs_lsa") is not None:
        buying_known_any = True
        if p.get("runs_google_ads") or p.get("runs_lsa"):
            buying_pts += 14
    else:
        missing.append("runs_google_ads")
    if p.get("paid_search_organic_gap") is not None:
        buying_known_any = True
        if p["paid_search_organic_gap"]:
            buying_pts += 6
    else:
        missing.append("paid_search_organic_gap")
    if p.get("recent_expansion") is not None or p.get("new_location") is not None:
        buying_known_any = True
        if p.get("recent_expansion") or p.get("new_location"):
            buying_pts += 4
    else:
        missing.append("recent_expansion")
    if p.get("marketing_hiring_signal") is not None:
        buying_known_any = True
        if p["marketing_hiring_signal"]:
            buying_pts += 2
    else:
        missing.append("marketing_hiring_signal")
    if p.get("review_velocity_signal") is not None:
        buying_known_any = True
        if p["review_velocity_signal"] == "STRONG":
            buying_pts += 2
    else:
        missing.append("review_velocity_signal")
    if p.get("recent_site_investment") is not None:
        buying_known_any = True
        if p["recent_site_investment"]:
            buying_pts += 1
    else:
        missing.append("recent_site_investment")
    if p.get("new_high_value_service") is not None:
        buying_known_any = True
        if p["new_high_value_service"]:
            buying_pts += 1
    else:
        missing.append("new_high_value_service")
    if buying_known_any:
        breakdown["buying_intent"] = min(weights["buying_intent"], buying_pts)

    # 4. Contactability -- from check_contactability.py's contactability_score (0/1/2).
    if p.get("contactability_score") is not None:
        breakdown["contactability"] = round(weights["contactability"] * (p["contactability_score"] / 2))
    else:
        missing.append("contactability_score")

    # 5. Market attractiveness -- deterministic from the cached market file:
    # a market whose known top competitors are mostly national franchises
    # (already cached, zero extra research) is less attractive to enter.
    if market:
        names = []
        for c in (market.get("top_competitors") or []):
            if isinstance(c, dict) and c.get("name"):
                names.append(c["name"])
        names += [n for n in (market.get("top_organic_competitors") or []) if n]
        names += [n for n in (market.get("top_maps_competitors") or []) if n]
        if names:
            franchise_hits = sum(1 for n in names if match_franchise_blocklist(n, None, blocklist)[1])
            fraction_franchise = franchise_hits / len(names)
            breakdown["market_attractiveness"] = round(weights["market_attractiveness"] * (1 - fraction_franchise))
        else:
            missing.append("market_competitor_data")
    else:
        missing.append("market_cache")

    confirmed = max(0, min(100, sum(breakdown.values())))

    # Potential: best case if every currently-missing field turned out
    # favorable, same discipline as V2's compute_potential_score.
    potential_breakdown = dict(breakdown)
    maturity_sub_missing = {"review_count", "years_in_business", "multiple_locations", "service_page_count"} & set(missing)
    buying_sub_missing = {
        "runs_google_ads", "paid_search_organic_gap", "recent_expansion",
        "marketing_hiring_signal", "review_velocity_signal", "recent_site_investment",
        "new_high_value_service",
    } & set(missing)

    if "niche_tier" in missing:
        potential_breakdown["niche_economics"] = tier_points.get(1, weights["niche_economics"])
    # Composite components (maturity, buying_intent) can be PARTIALLY confirmed
    # (e.g. review_count known but years_in_business missing) -- any remaining
    # missing sub-field means potential is still uncapped at the full weight,
    # not just whatever's already confirmed.
    if maturity_sub_missing:
        potential_breakdown["business_maturity"] = weights["business_maturity"]
    if buying_sub_missing:
        potential_breakdown["buying_intent"] = weights["buying_intent"]
    if "contactability_score" in missing:
        potential_breakdown["contactability"] = weights["contactability"]
    if "market_competitor_data" in missing or "market_cache" in missing:
        potential_breakdown["market_attractiveness"] = weights["market_attractiveness"]
    potential = max(0, min(100, sum(potential_breakdown.values())))

    tracked_fields = 8  # niche_tier, review_count, years_in_business, multiple_locations,
    # service_page_count, buying-intent-block, contactability_score, market data
    # (buying-intent sub-fields counted once as a block to avoid over-penalizing completeness
    # for a single missing minor signal like new_high_value_service)
    buying_block_missing = all(
        f in missing for f in
        ("runs_google_ads", "paid_search_organic_gap", "recent_expansion", "marketing_hiring_signal",
         "review_velocity_signal", "recent_site_investment", "new_high_value_service")
    )
    completeness_missing = []
    if "niche_tier" in missing:
        completeness_missing.append("niche_tier")
    if "review_count" in missing:
        completeness_missing.append("review_count")
    if "years_in_business" in missing:
        completeness_missing.append("years_in_business")
    if "multiple_locations" in missing:
        completeness_missing.append("multiple_locations")
    if "service_page_count" in missing:
        completeness_missing.append("service_page_count")
    if buying_block_missing:
        completeness_missing.append("buying_signals")
    if "contactability_score" in missing:
        completeness_missing.append("contactability_score")
    if "market_competitor_data" in missing or "market_cache" in missing:
        completeness_missing.append("market_data")
    completeness = round(100 * (tracked_fields - len(completeness_missing)) / tracked_fields)

    return {
        "confirmed_score": confirmed,
        "potential_score": potential,
        "completeness": completeness,
        "missing_fields": completeness_missing,
        "breakdown": breakdown,
    }


def score_gap(p, cfg, niches_cfg, market):
    """Google/local acquisition-opportunity score. Unknown ranking data is never scored as poor ranking."""
    weights = cfg["gap_weights"]
    breakdown = {}
    missing = []

    # 1. Maps/GBP opportunity
    maps_known = False
    maps_pts = 0
    if p.get("maps_position") is not None:
        maps_known = True
        if 4 <= p["maps_position"] <= 15:
            maps_pts += round(weights["maps_gbp_opportunity"] * 0.6)
    else:
        missing.append("maps_position")
    if p.get("obvious_gbp_issue") is not None:
        maps_known = True
        if p["obvious_gbp_issue"]:
            maps_pts += weights["maps_gbp_opportunity"] - round(weights["maps_gbp_opportunity"] * 0.6)
    else:
        missing.append("obvious_gbp_issue")
    if maps_known:
        breakdown["maps_gbp_opportunity"] = min(weights["maps_gbp_opportunity"], maps_pts)

    # 2. Organic visibility opportunity
    if p.get("organic_position") is not None:
        if 5 <= p["organic_position"] <= 30:
            breakdown["organic_visibility_opportunity"] = weights["organic_visibility_opportunity"]
    else:
        missing.append("organic_position")

    # 3. Service/practice architecture (reuses V2's weak-pages logic exactly)
    issues = set(p.get("obvious_website_issue") or [])
    niche_cfg = (niches_cfg.get("niches") or {}).get(p.get("niche") or "", {})
    typical_pages = len(niche_cfg.get("typical_service_pages") or [])
    spc = p.get("service_page_count")
    if "thin_service_pages" in issues:
        breakdown["service_architecture"] = weights["service_architecture"]
    elif spc is not None:
        if bool(typical_pages) and spc < max(2, typical_pages // 2):
            breakdown["service_architecture"] = weights["service_architecture"]
    else:
        missing.append("service_page_count")

    # 4. GBP/review gap (reuses V2's low_moderate_reviews logic)
    review_benchmark = (market or {}).get("review_benchmarks", {}).get("median_top3")
    if p.get("review_count") is not None:
        if review_benchmark:
            if p["review_count"] < review_benchmark:
                breakdown["gbp_review_gap"] = weights["gbp_review_gap"]
        elif p["review_count"] < 40:
            breakdown["gbp_review_gap"] = weights["gbp_review_gap"]
    else:
        missing.append("review_count")
    if not review_benchmark:
        missing.append("competitor_benchmark")

    # 5. Technical/indexation
    if p.get("obvious_website_issue") is not None:
        if issues & TECHNICAL_ISSUE_TAGS:
            breakdown["technical_indexation"] = weights["technical_indexation"]
    else:
        missing.append("obvious_website_issue")

    # 6. Competitor gap
    if p.get("competitor_gap") is not None:
        if p["competitor_gap"]:
            breakdown["competitor_gap"] = weights["competitor_gap"]
    else:
        missing.append("competitor_gap")

    confirmed = max(0, min(100, sum(breakdown.values())))

    potential_breakdown = dict(breakdown)
    field_to_component = {
        "maps_position": "maps_gbp_opportunity", "obvious_gbp_issue": "maps_gbp_opportunity",
        "organic_position": "organic_visibility_opportunity",
        "service_page_count": "service_architecture",
        "review_count": "gbp_review_gap", "competitor_benchmark": "gbp_review_gap",
        "obvious_website_issue": "technical_indexation",
        "competitor_gap": "competitor_gap",
    }
    for f in missing:
        comp = field_to_component.get(f)
        if comp and comp not in potential_breakdown:
            potential_breakdown[comp] = weights[comp]
    potential = max(0, min(100, sum(potential_breakdown.values())))

    tracked = ("maps_position", "obvious_gbp_issue", "organic_position", "service_page_count",
               "review_count", "competitor_benchmark", "obvious_website_issue", "competitor_gap")
    missing_unique = [f for f in tracked if f in missing]
    completeness = round(100 * (len(tracked) - len(missing_unique)) / len(tracked))

    # Directional classification: which side of the gap is actually evidenced.
    maps_side = breakdown.get("maps_gbp_opportunity", 0) + breakdown.get("gbp_review_gap", 0)
    web_side = (breakdown.get("organic_visibility_opportunity", 0)
                + breakdown.get("service_architecture", 0) + breakdown.get("technical_indexation", 0))
    if maps_side and web_side:
        gap_type = "BOTH"
    elif maps_side:
        gap_type = "MAPS"
    elif web_side:
        gap_type = "WEBSITE_SEO"
    else:
        gap_type = "UNKNOWN"

    return {
        "confirmed_score": confirmed,
        "potential_score": potential,
        "completeness": completeness,
        "missing_fields": missing_unique,
        "breakdown": breakdown,
        "gap_type": gap_type,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="infile", default=str(PROSPECTS / "discovered.jsonl"))
    args = ap.parse_args()

    cfg = load_yaml("scoring.yaml")
    niches_cfg = load_yaml("niches.yaml")
    records = read_jsonl(args.infile)
    if not records:
        print(f"No records found in {args.infile}", file=sys.stderr)
        return

    now = now_iso()
    scored = 0
    rejected = 0
    for p in records:
        # V2: business verification (verify_business.py) should run before
        # scoring, but DISCOVERED is still accepted directly for simple/manual
        # entries that skip identity verification (documented tradeoff).
        if p.get("status") not in ("DISCOVERED", "BUSINESS_VERIFIED"):
            continue
        reason = hard_reject_reason(p, cfg["reject_if"])
        if reason:
            p["status"] = "REJECTED"
            p["reject_reason"] = reason
            p["score"] = 0
            p["confirmed_score"] = 0
            p["potential_score"] = 0
            p["score_breakdown"] = {}
            p["data_completeness"] = None
            p["missing_fields"] = []
            rejected += 1
            continue
        result = score_with_completeness(p, cfg, niches_cfg)
        p["score"] = result["confirmed_score"]  # backward-compatible alias
        p["confirmed_score"] = result["confirmed_score"]
        p["potential_score"] = result["potential_score"]
        p["data_completeness"] = result["data_completeness"]
        p["missing_fields"] = result["missing_fields"]
        p["score_breakdown"] = result["score_breakdown"]
        p["status"] = "INITIAL_SCORE"
        p["last_audited_at"] = p.get("last_audited_at") or now
        scored += 1

    write_jsonl(args.infile, records)
    print(f"Scored {scored} prospect(s), hard-rejected {rejected} before any AI call. "
          f"Wrote {args.infile}")


if __name__ == "__main__":
    main()
