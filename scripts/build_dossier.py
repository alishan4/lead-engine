#!/usr/bin/env python3
"""
Merge a prospect + its quick-audit output + its opportunity selection into
one compact dossier (schemas/dossier.schema.json) at
data/leads/<slug>/dossier.json. This is also the cache/early-stop gate:

  - If a dossier already exists, the prospect's content_hash hasn't changed,
    and it's within config/limits.yaml: cache_days -> CACHE HIT, nothing is
    rebuilt and no AI is called.
  - If opportunity.confidence < min_opportunity_confidence, or
    opportunity.reject_lead is true -> the lead is REJECTED here and no
    dossier/email is produced (early stop, Phase 9).

Inputs expected on disk (produced by Claude running the corresponding prompt,
not by this script):
  data/leads/<slug>/quick_audit.json    (prompts/quick-audit.md output)
  data/leads/<slug>/opportunity.json    (prompts/opportunity-selector.md output)

Usage:
  python3 scripts/build_dossier.py --id roofing-charlotte-nc-kingdom-roofing
  python3 scripts/build_dossier.py --id ... --agents-used claude-seo:seo-local claude-seo:seo-content
"""
import argparse
from datetime import datetime, timedelta, timezone

from _lib import (
    PROSPECTS, LEADS, load_yaml, read_jsonl, write_jsonl, append_jsonl,
    lead_dir, load_json, write_json,
)

CACHE_LOG = LEADS / "_cache_log.jsonl"


def log_cache_event(prospect_id, event):
    append_jsonl(CACHE_LOG, {
        "prospect_id": prospect_id,
        "event": event,  # "hit" or "miss"
        "at": datetime.now(timezone.utc).isoformat(),
    })


def move_status(prospect_id, new_status, reason=None):
    discovered_path = PROSPECTS / "discovered.jsonl"
    qualified_path = PROSPECTS / "qualified.jsonl"
    rejected_path = PROSPECTS / "rejected.jsonl"

    discovered = read_jsonl(discovered_path)
    for r in discovered:
        if r["id"] == prospect_id:
            r["status"] = new_status
            if reason:
                r["reject_reason"] = reason
    write_jsonl(discovered_path, discovered)

    qualified = read_jsonl(qualified_path)
    updated_p = None
    remaining = []
    for r in qualified:
        if r["id"] == prospect_id:
            r["status"] = new_status
            if reason:
                r["reject_reason"] = reason
            updated_p = r
            if new_status == "REJECTED":
                continue  # drop from qualified.jsonl
        remaining.append(r)
    write_jsonl(qualified_path, remaining)

    if new_status == "REJECTED" and updated_p:
        append_jsonl(rejected_path, updated_p)

    return updated_p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    ap.add_argument("--agents-used", nargs="*", default=[])
    ap.add_argument("--force", action="store_true", help="ignore cache and rebuild")
    args = ap.parse_args()

    limits = load_yaml("limits.yaml")
    ldir = lead_dir(args.id)

    qualified = {r["id"]: r for r in read_jsonl(PROSPECTS / "qualified.jsonl")}
    p = qualified.get(args.id)
    if not p:
        raise SystemExit(f"Prospect {args.id} not found in qualified.jsonl (already rejected/moved?)")

    dossier_path = ldir / "dossier.json"
    existing = load_json(dossier_path)
    if existing and not args.force and p.get("content_hash"):
        created_at = existing.get("created_at")
        same_hash = existing.get("_content_hash") == p.get("content_hash")
        fresh_enough = True
        if created_at:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(created_at)
            fresh_enough = age <= timedelta(days=limits["cache_days"])
        if same_hash and fresh_enough:
            log_cache_event(args.id, "hit")
            print(f"CACHE HIT: {dossier_path} is fresh (content_hash unchanged, "
                  f"within {limits['cache_days']} days). No agents called, no rebuild.")
            return

    log_cache_event(args.id, "miss")
    quick_audit = load_json(ldir / "quick_audit.json")
    opportunity = load_json(ldir / "opportunity.json")
    if not quick_audit or not opportunity:
        raise SystemExit(
            f"Missing quick_audit.json and/or opportunity.json in {ldir}. "
            "Run the quick-audit and opportunity-selector prompts first."
        )

    min_conf = limits["min_opportunity_confidence"]
    if opportunity.get("reject_lead") or opportunity.get("confidence", 0) < min_conf:
        move_status(args.id, "REJECTED", reason=f"low_opportunity_confidence(<{min_conf})")
        print(f"REJECTED {args.id}: opportunity confidence "
              f"{opportunity.get('confidence')} below threshold {min_conf}, or reject_lead flagged. "
              "No dossier/email produced.")
        return

    now = datetime.now(timezone.utc).isoformat()
    dossier = {
        "business": p["business_name"],
        "city": p.get("city"),
        "state": p.get("state"),
        "niche": p.get("niche"),
        "website": p.get("website"),
        "maps_position": p.get("maps_position"),
        "organic_position": p.get("organic_position"),
        "rating": p.get("rating"),
        "reviews": p.get("review_count"),
        "money_keyword": (quick_audit.get("money_keyword")
                           or opportunity.get("money_keyword")
                           or ""),
        "problem_type": opportunity["primary_opportunity"],
        "strongest_finding": quick_audit["strongest_finding"],
        "evidence": opportunity.get("supporting_evidence") or quick_audit.get("evidence"),
        "evidence_items": opportunity.get("evidence", []),
        "business_impact": opportunity.get("business_consequence") or quick_audit.get("business_impact"),
        "free_value": opportunity.get("free_recommendation") or quick_audit.get("free_actionable_recommendation"),
        "competitors": p.get("competitor_gap") or [],
        "confidence": opportunity["confidence"],
        "contact_verified": bool(p.get("verified_business")),
        "sources": opportunity.get("sources", []),
        "agents_used": args.agents_used,
        "pages_checked": opportunity.get("pages_checked") or quick_audit.get("pages_checked") or [],
        "observed_at": opportunity.get("observed_at") or now,
        "ranking_observed_at": p.get("enrichment_observed_at"),
        "created_at": now,
        "_content_hash": p.get("content_hash"),
    }
    write_json(dossier_path, dossier)
    move_status(args.id, "DOSSIER_READY")
    print(f"DOSSIER_READY: wrote {dossier_path}")


if __name__ == "__main__":
    main()
