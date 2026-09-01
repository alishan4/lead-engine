#!/usr/bin/env python3
"""
V3.1.1 core evidence model: confidence banding, freshness, entity-match
gating, conflict detection, and deterministic resolution of one or more
SignalEvidence items (schemas/signal_evidence.schema.json) into the flat
value V3.1's score_fit/route_v3 already consume.

This module is the single place that knows what "STRONG_VERIFIED" means,
what counts as stale, and what counts as a conflict -- score_leads.py and
qualify_leads.py never touch a SignalEvidence item directly, they only ever
read the resolved output (a plain True/False/None plus a confidence tier).

UNKNOWN != FALSE, everywhere in this file: a signal with no usable evidence
resolves to value=None, never False. False is reserved for evidence that
POSITIVELY confirmed the signal's absence.
"""
from datetime import datetime, timezone

from _lib import load_yaml, now_iso, days_since

TIERS = ("UNUSABLE", "WEAK", "VERIFIED", "STRONG_VERIFIED")


def confidence_tier(confidence, cfg):
    bands = cfg["confidence_bands"]
    if confidence >= bands["strong_verified_min"]:
        return "STRONG_VERIFIED"
    if confidence >= bands["verified_min"]:
        return "VERIFIED"
    if confidence >= bands["weak_min"]:
        return "WEAK"
    return "UNUSABLE"


def is_verified_or_better(tier):
    return tier in ("VERIFIED", "STRONG_VERIFIED")


def _freshness_days_for(signal_type, cfg):
    return cfg["freshness_days"].get(signal_type)  # None = no configured window (e.g. review_velocity_signal)


def evidence_age_days(item, now=None):
    ts = item.get("observed_at")
    if not ts:
        return None
    if now is None:
        return days_since(ts)
    try:
        observed = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    return (now - observed).total_seconds() / 86400.0


def is_fresh(item, cfg, now=None):
    """True if this item has no configured freshness window (always fresh
    by definition) OR its age is within that window. Never fabricates a
    date -- an item with no observed_at is never considered fresh."""
    window = _freshness_days_for(item["signal_type"], cfg)
    if window is None:
        return True
    age = evidence_age_days(item, now=now)
    if age is None:
        return False
    return age <= window


def passes_entity_match(item, cfg):
    conf = item.get("entity_match_confidence")
    if conf is None:
        return False
    return conf >= cfg["min_entity_match_confidence"]


def effective_confidence(item, cfg):
    """
    Applies the source-hierarchy cap: an item whose source_type is listed
    under a signal's low_confidence_sources can never back more than WEAK,
    regardless of its stated confidence -- e.g. a single directory listing
    must never alone prove a NEW location.
    """
    rules = (cfg.get("signal_source_rules") or {}).get(item["signal_type"], {})
    low_sources = set(rules.get("low_confidence_sources") or [])
    conf = item["confidence"]
    if item.get("source_type") in low_sources:
        cap = cfg["confidence_bands"]["weak_min"] + 0.001
        conf = min(conf, max(0.0, cap - 0.001))  # stays inside the WEAK band, never reaches verified_min
    return conf


def resolve_signal(items, cfg, now=None):
    """
    Resolve every SignalEvidence item for ONE signal_type into a single
    decision. Returns:
      {
        "value": True/False/None,
        "tier": "STRONG_VERIFIED"|"VERIFIED"|"WEAK"|"UNUSABLE"|None,
        "status": "RESOLVED"|"CONFLICTED"|"STALE_ONLY"|"NO_EVIDENCE"|"ENTITY_REJECTED",
        "evidence_used": [...], "evidence_rejected": [...],
      }
    """
    if now is None:
        now = datetime.now(timezone.utc)

    accepted, rejected = [], []
    for item in items:
        if not passes_entity_match(item, cfg):
            rejected.append({**item, "_reject_reason": "entity_match_ambiguous"})
            continue
        eff_conf = effective_confidence(item, cfg)
        tier = confidence_tier(eff_conf, cfg)
        if tier == "UNUSABLE":
            rejected.append({**item, "_reject_reason": "unusable_confidence", "_tier": tier})
            continue
        accepted.append({**item, "_tier": tier, "_effective_confidence": eff_conf})

    if not accepted:
        if any(r.get("_reject_reason") == "entity_match_ambiguous" for r in rejected):
            return {"value": None, "tier": None, "status": "ENTITY_REJECTED",
                    "evidence_used": [], "evidence_rejected": rejected}
        return {"value": None, "tier": None, "status": "NO_EVIDENCE",
                "evidence_used": [], "evidence_rejected": rejected}

    fresh = [i for i in accepted if is_fresh(i, cfg, now=now)]
    fresh_ids = {id(i) for i in fresh}
    stale = [i for i in accepted if id(i) not in fresh_ids]

    if not fresh:
        return {"value": None, "tier": None, "status": "STALE_ONLY",
                "evidence_used": [], "evidence_rejected": rejected + stale}

    values = {i["value"] for i in fresh}
    if len(values) > 1:
        return {"value": None, "tier": None, "status": "CONFLICTED",
                "evidence_used": fresh, "evidence_rejected": rejected + stale}

    best = max(fresh, key=lambda i: (TIERS.index(i["_tier"]), i["_effective_confidence"]))
    return {"value": best["value"], "tier": best["_tier"], "status": "RESOLVED",
            "evidence_used": fresh, "evidence_rejected": rejected + stale}


def resolve_signals(all_items, cfg, now=None):
    """Group by signal_type and resolve each independently."""
    by_type = {}
    for item in all_items:
        by_type.setdefault(item["signal_type"], []).append(item)
    return {sig_type: resolve_signal(items, cfg, now=now) for sig_type, items in by_type.items()}


def derive_paid_search_organic_gap(runs_ads_value, runs_lsa_value, organic_position):
    """
    paid_search_organic_gap is ALWAYS derived, never independently
    evidenced (see schemas/signal_evidence.schema.json). It may only become
    true when BOTH a paid signal AND a verified organic-visibility figure
    exist -- never derived from an unknown/null rank.
    """
    paid = (runs_ads_value is True) or (runs_lsa_value is True)
    if not paid:
        return None if (runs_ads_value is None and runs_lsa_value is None) else False
    if organic_position is None:
        return None  # paid confirmed, but no verified organic figure to compare against
    return organic_position >= 5  # not already dominant organically despite paying


def _parse_ts(ts):
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def compute_review_velocity(snapshots, cfg):
    """
    Deterministic review-velocity classification from >=2 timestamped
    snapshots (data/leads/<id>/review_snapshots.jsonl). Returns
    "STRONG"/"MODERATE"/"LOW"/"UNKNOWN". Fewer than 2 snapshots, or a
    snapshot missing review_count/observed_at, always returns UNKNOWN --
    total review count alone can never produce a velocity, by design.
    """
    usable = [s for s in snapshots if s.get("review_count") is not None and s.get("observed_at")]
    if len(usable) < 2:
        return "UNKNOWN"
    usable.sort(key=lambda s: s["observed_at"])
    t0, t1 = usable[0], usable[-1]
    try:
        delta_days = (_parse_ts(t1["observed_at"]) - _parse_ts(t0["observed_at"])).total_seconds() / 86400.0
    except ValueError:
        return "UNKNOWN"
    if delta_days <= 0:
        return "UNKNOWN"
    delta_reviews = t1["review_count"] - t0["review_count"]
    reviews_per_month = max(0.0, delta_reviews) / delta_days * 30
    thresholds = cfg["review_velocity_thresholds"]
    if reviews_per_month >= thresholds["strong_per_month"]:
        return "STRONG"
    if reviews_per_month >= thresholds["moderate_per_month"]:
        return "MODERATE"
    return "LOW"


if __name__ == "__main__":
    import sys
    print("signal_evidence.py is a library module -- import its functions, it has no CLI.", file=sys.stderr)
    sys.exit(1)
