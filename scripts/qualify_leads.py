#!/usr/bin/env python3
"""
Apply score thresholds (config/scoring.yaml: thresholds) to every INITIAL_SCORE
(or RESCORED) prospect and route it: QUALIFIED -> data/prospects/qualified.jsonl,
REJECTED -> data/prospects/rejected.jsonl, MANUAL_REVIEW -> manual_review.jsonl,
NEEDS_ENRICHMENT -> needs_enrichment.jsonl. No AI is called for any of these
outcomes -- this script and score_leads.py are the entire pre-AI gate.

V2 NEEDS_ENRICHMENT rule (the fix for the V1 bottleneck where good leads
stalled at MANUAL_REVIEW purely for lack of ranking data): if the confirmed
score falls short of qualified_min but the potential score clears it, AND at
least one of the missing fields is a "material" one (maps_position or
organic_position -- the two heaviest-weighted, hardest-to-guess signals),
route to NEEDS_ENRICHMENT instead of MANUAL_REVIEW/REJECTED. This is a
routing decision, not a scoring change -- the qualified_min/manual_review_min
thresholds themselves are untouched.

V3.1 addition (`--v3` flag, everything above is unchanged V2 behavior and
untouched by this flag): routes FIT_SCORED records using the FIT/GAP model
instead of V2's single blended score. FIT answers "would this be a
commercially attractive client" -- a business is never QUALIFIED just
because its GAP is bad; it must also show FIT (niche economics, maturity,
buying intent, contactability, market attractiveness). See
config/scoring.yaml: fit_thresholds/gap_thresholds and
docs/LEAD-ENGINE.md for the full reasoning.

Usage:
  python3 scripts/qualify_leads.py
  python3 scripts/qualify_leads.py --v3
"""
import argparse

from _lib import PROSPECTS, LEADS, load_yaml, read_jsonl, write_jsonl, append_jsonl, set_status_everywhere, lead_dir, load_json, write_json, now_iso

# Buying/timing signals that can justify HIGH_PRIORITY -- any ONE true value
# (or a "strong" review-velocity signal) is enough; HIGH_PRIORITY is never
# granted on GAP size alone.
HIGH_PRIORITY_SIGNAL_FIELDS = (
    "runs_google_ads", "runs_lsa", "recent_expansion", "new_location",
    "marketing_hiring_signal", "recent_site_investment", "new_high_value_service",
)

# Missing data in these two fields is what "could materially change the
# decision" -- they're the two heaviest scoring weights and the ones a plain
# WebSearch discovery pass usually can't fill.
MATERIAL_ENRICHMENT_FIELDS = {"maps_position", "organic_position"}


def needs_enrichment(p, cfg):
    confirmed = p.get("confirmed_score")
    potential = p.get("potential_score")
    missing = set(p.get("missing_fields") or [])
    if confirmed is None or potential is None:
        return False
    if confirmed >= cfg["qualified_min"]:
        return False  # already qualified on confirmed evidence alone
    if potential < cfg["qualified_min"]:
        return False  # even the best case doesn't clear the bar
    return bool(missing & MATERIAL_ENRICHMENT_FIELDS)


def main():
    cfg = load_yaml("scoring.yaml")["thresholds"]
    discovered_path = PROSPECTS / "discovered.jsonl"
    qualified_path = PROSPECTS / "qualified.jsonl"
    rejected_path = PROSPECTS / "rejected.jsonl"
    manual_path = PROSPECTS / "manual_review.jsonl"
    enrichment_path = PROSPECTS / "needs_enrichment.jsonl"

    records = read_jsonl(discovered_path)
    existing_qualified = {r["id"] for r in read_jsonl(qualified_path)}
    existing_rejected = {r["id"] for r in read_jsonl(rejected_path)}
    existing_enrichment = {r["id"] for r in read_jsonl(enrichment_path)}

    counts = {"qualified": 0, "manual_review": 0, "rejected": 0, "needs_enrichment": 0, "skipped": 0}

    for p in records:
        if p.get("status") == "REJECTED":
            if p["id"] not in existing_rejected:
                append_jsonl(rejected_path, p)
                existing_rejected.add(p["id"])
            counts["rejected"] += 1
            continue
        if p.get("status") not in ("SCORED", "INITIAL_SCORE", "RESCORED"):
            counts["skipped"] += 1
            continue

        if needs_enrichment(p, cfg):
            p["status"] = "NEEDS_ENRICHMENT"
            if p["id"] not in existing_enrichment:
                append_jsonl(enrichment_path, p)
                existing_enrichment.add(p["id"])
            counts["needs_enrichment"] += 1
            continue

        score = p.get("confirmed_score", p.get("score", 0))
        if score >= cfg["qualified_min"]:
            p["status"] = "QUALIFIED"
            if p["id"] not in existing_qualified:
                append_jsonl(qualified_path, p)
                existing_qualified.add(p["id"])
            counts["qualified"] += 1
        elif score >= cfg["manual_review_min"]:
            p["status"] = "MANUAL_REVIEW"
            append_jsonl(manual_path, p)
            counts["manual_review"] += 1
        else:
            p["status"] = "REJECTED"
            p["reject_reason"] = p.get("reject_reason") or "score_below_threshold"
            if p["id"] not in existing_rejected:
                append_jsonl(rejected_path, p)
                existing_rejected.add(p["id"])
            counts["rejected"] += 1

    write_jsonl(discovered_path, records)
    print(
        f"Qualified: {counts['qualified']} | Needs enrichment: {counts['needs_enrichment']} | "
        f"Manual review: {counts['manual_review']} | Rejected (no AI spend): {counts['rejected']} | "
        f"Skipped (not yet scored): {counts['skipped']}"
    )


# ============================================================================
# V3.1 ADDITIONS
# ============================================================================

def build_why_now(p):
    """
    Deterministic, evidence-only synthesis -- every field either cites a
    confirmed value already on the record, or is left null. Never invents
    urgency: why_now/why_likely_buyer are null unless a real confirmed
    signal backs them, which is exactly what keeps a lead at QUALIFIED
    instead of being promoted to HIGH_PRIORITY.
    """
    business = p.get("business_name") or "This business"
    niche_label = p.get("niche")
    city, state = p.get("city"), p.get("state")

    why_this_company = None
    if niche_label and city and state:
        why_this_company = f"{business} is a {niche_label.replace('_', ' ')} business in {city}, {state}."

    why_this_problem = None
    gap_breakdown = p.get("gap_breakdown") or {}
    if gap_breakdown:
        top_component = max(gap_breakdown, key=gap_breakdown.get)
        evidence_line = None
        if p.get("competitor_gap"):
            evidence_line = p["competitor_gap"][0]
        elif top_component == "service_architecture" and p.get("service_page_count") is not None:
            evidence_line = f"only {p['service_page_count']} service pages found"
        elif top_component == "gbp_review_gap" and p.get("review_count") is not None:
            evidence_line = f"{p['review_count']} reviews, below the market benchmark"
        if evidence_line:
            why_this_problem = f"{top_component.replace('_', ' ')}: {evidence_line}"

    # V3.1.1: why_now may ONLY be populated from VERIFIED/STRONG_VERIFIED,
    # fresh evidence -- a bare `True` with no tier (or a WEAK/stale one) is
    # not enough. "Bad SEO" / "competitors rank better" / "high-value niche"
    # explain WHY THIS PROBLEM (see why_this_problem above), never WHY NOW --
    # they must never appear here.
    why_now = None
    tiers = p.get("buying_signal_tiers") or {}
    signal_phrases = [
        ("runs_google_ads", "actively running Google Ads"),
        ("runs_lsa", "actively running Local Services Ads"),
        ("recent_expansion", "recently expanded"),
        ("new_location", "recently opened a new location"),
        ("marketing_hiring_signal", "actively hiring for marketing"),
        ("recent_site_investment", "recently invested in a site redesign"),
        ("new_high_value_service", "recently added a new high-value service line"),
    ]
    for field, phrase in signal_phrases:
        if p.get(field) is True and tiers.get(field) in ("VERIFIED", "STRONG_VERIFIED"):
            why_now = f"{business} is {phrase} right now."
            break
    if why_now is None and p.get("review_velocity_signal") == "STRONG":
        why_now = f"{business} has a verified, recent review-acceleration signal (computed from timestamped snapshots)."

    # why_likely_buyer: commercial maturity + verified acquisition/investment
    # behavior + contactability + scale. Never claims "can afford SEO" --
    # states the indicators, not the conclusion.
    why_likely_buyer = None
    buyer_facts = []
    if p.get("review_count") is not None and p["review_count"] >= 10:
        buyer_facts.append(f"{p['review_count']} reviews")
    if p.get("years_in_business") is not None and p["years_in_business"] >= 3:
        buyer_facts.append(f"{p['years_in_business']} years in business")
    if p.get("multiple_locations"):
        buyer_facts.append("multiple locations")
    for field, phrase in signal_phrases:
        if p.get(field) is True and tiers.get(field) in ("VERIFIED", "STRONG_VERIFIED"):
            buyer_facts.append(phrase)
    # Contactability is supporting context, never the sole basis -- only
    # appended once a real maturity/buying-intent fact already justifies
    # why_likely_buyer at all.
    if buyer_facts and p.get("contactability_score"):
        buyer_facts.append(f"contactability score {p['contactability_score']}/2")
    if buyer_facts:
        why_likely_buyer = f"Commercial-readiness indicators for {business} include: " + ", ".join(buyer_facts) + "."

    return {
        "why_this_company": why_this_company, "why_this_problem": why_this_problem,
        "why_now": why_now, "why_likely_buyer": why_likely_buyer,
    }


def needs_enrichment_v3(p, gap_thresholds):
    gap_c, gap_p = p.get("gap_confirmed_score"), p.get("gap_potential_score")
    if gap_c is None or gap_p is None:
        return False
    if gap_c >= gap_thresholds["qualified_min"]:
        return False
    if gap_p < gap_thresholds["qualified_min"]:
        return False
    return bool(set(p.get("gap_missing_fields") or []) & MATERIAL_ENRICHMENT_FIELDS)


def route_v3(p, scoring_cfg, limits_cfg):
    """
    Pure routing decision -- unit-testable without file I/O. Returns
    (status, reason, expensive_audit_permitted, why_now_object_or_None).
    """
    ft, gt = scoring_cfg["fit_thresholds"], scoring_cfg["gap_thresholds"]
    fit_c, fit_p = p.get("fit_confirmed_score"), p.get("fit_potential_score")
    gap_c = p.get("gap_confirmed_score")

    if fit_c is None or gap_c is None:
        return ("MANUAL_REVIEW", "FIT/GAP not both computed yet -- cannot route automatically", False, None)

    if fit_c <= ft["reject_max"]:
        return ("REJECTED", f"FIT confirmed {fit_c} <= reject_max {ft['reject_max']} -- "
                             "weak commercial fit; a business is not qualified merely because its GAP is bad", False, None)

    if fit_c <= ft["manual_review_max"]:
        return ("MANUAL_REVIEW", f"FIT confirmed {fit_c} in the borderline "
                                  f"{ft['reject_max'] + 1}-{ft['manual_review_max']} band -- human judgment call on commercial fit", False, None)

    # fit_c >= fit_thresholds.qualified_min from here
    if gap_c < gt["qualified_min"]:
        if needs_enrichment_v3(p, gt):
            return ("NEEDS_ENRICHMENT", f"FIT qualifies ({fit_c}) but GAP evidence is incomplete "
                                        f"(confirmed {gap_c}, potential {p.get('gap_potential_score')}) -- "
                                        "material ranking data missing, not confirmed absent", False, None)
        return ("REJECTED", f"FIT qualifies ({fit_c}) but no defensible GAP was found "
                             f"(confirmed {gap_c}, potential {p.get('gap_potential_score')}) -- "
                             "a commercially fine business with nothing defensible to contact them about", False, None)

    # fit_c >= 40 and gap_c >= 40: at least QUALIFIED. Check HIGH_PRIORITY.
    contactability = p.get("contactability_score") or 0
    # V3.1.1: the qualifying signal must be VERIFIED or STRONG_VERIFIED (see
    # config/signal_sources.yaml, scripts/signal_evidence.py) -- a boolean
    # true with no tier info (legacy V3.1 data) or a WEAK/stale tier can
    # never independently trigger HIGH_PRIORITY. review_velocity_signal is
    # exempt from tiering -- it's directly computed from real timestamped
    # snapshots (compute_review_velocity), not confidence-scored research.
    tiers = p.get("buying_signal_tiers") or {}
    has_buying_signal = (
        any(p.get(f) is True and tiers.get(f) in ("VERIFIED", "STRONG_VERIFIED")
            for f in HIGH_PRIORITY_SIGNAL_FIELDS)
        or p.get("review_velocity_signal") == "STRONG"
    )
    meets_hp_scores = (
        fit_c >= ft["high_priority_min"] and gap_c >= gt["high_priority_min"]
        and contactability >= limits_cfg["min_contactability_for_expensive_work"]
        and has_buying_signal
    )
    why = build_why_now(p)
    if meets_hp_scores and why["why_now"] and why["why_likely_buyer"]:
        return ("HIGH_PRIORITY", f"FIT {fit_c} >= {ft['high_priority_min']}, GAP {gap_c} >= {gt['high_priority_min']}, "
                                  "contactability and a real buying/timing signal all confirmed, "
                                  "and why_now/why_likely_buyer are both evidence-backed", True, why)
    if meets_hp_scores:
        return ("QUALIFIED", "meets HIGH_PRIORITY score thresholds but why_now/why_likely_buyer "
                              "could not be evidence-backed -- never invent urgency, stays QUALIFIED", True, why)
    return ("QUALIFIED", f"FIT {fit_c} and GAP {gap_c} both clear qualified_min "
                          "but HIGH_PRIORITY thresholds or a buying/timing signal are not met", True, why)


OUTCOME_FILES = {
    "QUALIFIED": "qualified.jsonl", "HIGH_PRIORITY": "qualified.jsonl",
    "MANUAL_REVIEW": "manual_review.jsonl", "NEEDS_ENRICHMENT": "needs_enrichment.jsonl",
    "REJECTED": "rejected.jsonl",
}


def move_prospect_to_outcome_file(pid, merged_record, target_status):
    """
    Places pid's record in exactly the ONE outcome file matching
    target_status, removing it from every other outcome file it might be
    sitting in from a prior run -- e.g. a lead that was NEEDS_ENRICHMENT
    under V2 and is now QUALIFIED under V3.1 must not leave a stale,
    duplicate copy behind in needs_enrichment.jsonl (a real bug caught
    during this implementation: main_v3's first draft only ever appended,
    never removed, leaving exactly this kind of stale duplicate).
    """
    target_file = OUTCOME_FILES[target_status]
    for fname in set(OUTCOME_FILES.values()):
        path = PROSPECTS / fname
        recs = [r for r in read_jsonl(path) if r["id"] != pid]
        if fname == target_file:
            recs.append(merged_record)
        write_jsonl(path, recs)


def main_v3():
    scoring_cfg = load_yaml("scoring.yaml")
    limits_cfg = load_yaml("limits.yaml")
    discovered_path = PROSPECTS / "discovered.jsonl"
    records = read_jsonl(discovered_path)

    counts = {"qualified": 0, "high_priority": 0, "manual_review": 0, "rejected": 0,
              "needs_enrichment": 0, "skipped": 0}

    for p in records:
        if p.get("status") != "FIT_SCORED":
            counts["skipped"] += 1
            continue

        pid = p["id"]
        set_status_everywhere(pid, "GAP_SCORED")  # real, if momentary, transition -- both scores are final at this point
        status, reason, expensive_audit_permitted, why = route_v3(p, scoring_cfg, limits_cfg)

        extra = {}
        if why:
            extra.update(why)
        if status == "REJECTED":
            extra["reject_reason"] = reason
        set_status_everywhere(pid, status, extra_fields=extra)

        merged = {**p, "status": status, **extra}
        move_prospect_to_outcome_file(pid, merged, status)
        counts["high_priority" if status == "HIGH_PRIORITY" else status.lower()] += 1

        qual = load_json(lead_dir(pid) / "qualification_v3.json") or {"prospect_id": pid}
        qual["why_now_object"] = why or {}
        qual["routing"] = {"status": status, "reason": reason, "expensive_audit_permitted": expensive_audit_permitted}
        qual["generated_at"] = now_iso()
        write_json(lead_dir(pid) / "qualification_v3.json", qual)

        print(f"{pid}: {status} -- {reason}")

    # NOTE: no final write_jsonl(discovered_path, records) here -- every
    # status/field mutation for a processed record already went through
    # set_status_everywhere(), which re-reads and re-writes discovered.jsonl
    # (and the other prospect files) fresh each call. Writing the original
    # in-memory `records` list here would silently overwrite all of that
    # with stale pre-routing data (status still "FIT_SCORED" on every one)
    # -- a real bug caught during implementation, not shipped.
    print(
        f"\nHIGH_PRIORITY: {counts['high_priority']} | Qualified: {counts['qualified']} | "
        f"Needs enrichment: {counts['needs_enrichment']} | Manual review: {counts['manual_review']} | "
        f"Rejected: {counts['rejected']} | Skipped (not yet FIT_SCORED): {counts['skipped']}"
    )


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--v3", action="store_true", help="Run V3.1 FIT/GAP routing instead of V2's blended-score routing")
    args, _ = ap.parse_known_args()
    if args.v3:
        main_v3()
    else:
        main()
