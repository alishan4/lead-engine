#!/usr/bin/env python3
"""
Deterministic rescore step -- Phase 7 of the V2 pipeline. No Claude agent is
used here. Takes one NEEDS_ENRICHMENT prospect, looks up its business in the
market's imported ranking data (data/rankings/<market_id>.csv, populated by
import_rankings.py from a Semrush/manual CSV or JSON snapshot -- never a
live paid-API call), fills in whatever fields that data confirms, and
recomputes the score from scratch with score_leads.score_with_completeness.

Usage:
  python3 scripts/rescore_leads.py --id hvac-nashville-tn-example-co
"""
import argparse
import csv

from _lib import (
    PROSPECTS, MARKETS, LEADS, load_yaml, read_jsonl, write_jsonl, append_jsonl,
    market_slug, rankings_path, now_iso,
)
from score_leads import score_with_completeness

ENRICHMENT_LOG = LEADS / "_enrichment_log.jsonl"


def log_enrichment_attempt(prospect_id, market_id, outcome, fields_added=None, matched_rows=0):
    """
    Durable record of every rescore attempt, including no-op ones. Without
    this, a lead whose only available data (e.g. a Semrush keyword-count
    summary, or a 'not present in captured results' observation) doesn't
    resolve maps_position/organic_position would silently look never-tried
    on the next run -- wasting effort re-attempting the same fruitless
    enrichment, or worse, someone assuming it's still unenriched.
    """
    append_jsonl(ENRICHMENT_LOG, {
        "prospect_id": prospect_id, "market_id": market_id, "outcome": outcome,
        "fields_added": fields_added or [], "matched_rows": matched_rows, "at": now_iso(),
    })


def load_rankings(market_id):
    path = rankings_path(market_id)
    if not path.exists():
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def domain_of(url):
    if not url:
        return None
    return (
        url.replace("https://", "").replace("http://", "")
        .split("/")[0].replace("www.", "").lower()
    )


def find_ranking_match(rows, business_name, website):
    domain = domain_of(website)
    name_lower = (business_name or "").lower()
    matches = []
    for r in rows:
        row_domain = domain_of(r.get("domain") or r.get("ranking_url"))
        row_name = (r.get("business_name") or "").lower()
        if domain and row_domain and domain == row_domain:
            matches.append(r)
        elif name_lower and row_name and (name_lower in row_name or row_name in name_lower):
            matches.append(r)
    return matches


def best_position(matches, field):
    """
    Only ever returns a position from a row explicitly marked
    exact_rank_verified (defaults to true for older rows without the field,
    for backward compatibility -- but any row recording an absence
    observation must set it to false, and this function must never treat
    that as a usable number no matter what value happens to sit in `field`).
    """
    positions = []
    for m in matches:
        verified = m.get("exact_rank_verified")
        if verified in ("False", "false", False):
            continue
        v = m.get(field)
        if v not in (None, ""):
            try:
                positions.append(int(float(v)))
            except ValueError:
                continue
    return min(positions) if positions else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    args = ap.parse_args()

    enrichment_path = PROSPECTS / "needs_enrichment.jsonl"
    records = read_jsonl(enrichment_path)
    p = next((r for r in records if r["id"] == args.id), None)
    if not p:
        raise SystemExit(f"Prospect {args.id} not found in {enrichment_path}")

    market_id = market_slug(p.get("niche"), p.get("city"), p.get("state"))
    rows = load_rankings(market_id)
    if not rows:
        raise SystemExit(
            f"No ranking data at {rankings_path(market_id)} for market {market_id}. "
            "Run scripts/import_rankings.py (and optionally enrich_market.py) first."
        )

    matches = find_ranking_match(rows, p["business_name"], p.get("website"))
    if not matches:
        log_enrichment_attempt(args.id, market_id, "no_matching_rows")
        print(f"No ranking rows matched business_name/website for {args.id} in {market_id}'s "
              f"import -- leaving prospect in NEEDS_ENRICHMENT. Check the import for a name/domain match.")
        return

    fields_added = []
    new_maps = best_position(matches, "maps_position")
    new_organic = best_position(matches, "organic_position")
    if new_maps is not None and p.get("maps_position") is None:
        p["maps_position"] = new_maps
        fields_added.append("maps_position")
    if new_organic is not None and p.get("organic_position") is None:
        p["organic_position"] = new_organic
        fields_added.append("organic_position")

    if not fields_added:
        log_enrichment_attempt(args.id, market_id, "matched_but_no_usable_position", matched_rows=len(matches))
        print(f"Ranking data matched {args.id} ({len(matches)} row(s)) but contained no new, "
              f"exact_rank_verified maps_position/organic_position value -- leaving prospect in "
              f"NEEDS_ENRICHMENT. See {ENRICHMENT_LOG} for the attempt record.")
        return

    log_enrichment_attempt(args.id, market_id, "fields_added", fields_added=fields_added, matched_rows=len(matches))

    sources = {m["source"] for m in matches if m.get("source")}
    observed_ats = [m["observed_at"] for m in matches if m.get("observed_at")]

    score_cfg = load_yaml("scoring.yaml")
    niches_cfg = load_yaml("niches.yaml")
    result = score_with_completeness(p, score_cfg, niches_cfg)

    p["score_before_enrichment"] = p.get("confirmed_score")
    p["score_after_enrichment"] = result["confirmed_score"]
    p["enrichment_fields_added"] = fields_added
    p["enrichment_source"] = ",".join(sorted(sources)) if sources else "manual_csv"
    p["enrichment_observed_at"] = max(observed_ats) if observed_ats else now_iso()

    p["score"] = result["confirmed_score"]
    p["confirmed_score"] = result["confirmed_score"]
    p["potential_score"] = result["potential_score"]
    p["data_completeness"] = result["data_completeness"]
    p["missing_fields"] = result["missing_fields"]
    p["score_breakdown"] = result["score_breakdown"]
    p["status"] = "RESCORED"

    # Remove from needs_enrichment.jsonl -- qualify_leads.py will route the
    # RESCORED record to qualified/manual_review/rejected next.
    remaining = [r for r in records if r["id"] != args.id]
    write_jsonl(enrichment_path, remaining)

    discovered_path = PROSPECTS / "discovered.jsonl"
    discovered = read_jsonl(discovered_path)
    for r in discovered:
        if r["id"] == args.id:
            r.update(p)
    write_jsonl(discovered_path, discovered)

    thresholds = score_cfg["thresholds"]
    verdict = (
        "would now QUALIFY" if result["confirmed_score"] >= thresholds["qualified_min"]
        else "still below qualified_min -- run qualify_leads.py to route to MANUAL_REVIEW/REJECTED"
    )
    print(
        f"{args.id}: score {p['score_before_enrichment']} -> {p['score_after_enrichment']} "
        f"(+{fields_added}, source={p['enrichment_source']}). {verdict}. "
        f"Run `python3 scripts/qualify_leads.py` next to finalize routing."
    )


if __name__ == "__main__":
    main()
