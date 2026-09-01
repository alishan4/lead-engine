#!/usr/bin/env python3
"""
Merge imported ranking data (data/rankings/<market_id>.csv, from
import_rankings.py) into the market cache (data/markets/<market_id>/market.json).
Purely deterministic aggregation -- no AI, no network. This is what lets
score_leads.py / rescore_leads.py see a market's top organic/Maps competitors
and search-volume/CPC/KD data without re-researching it per lead.

Usage:
  python3 scripts/enrich_market.py --market hvac-nashville-tn
"""
import argparse
import csv

from _lib import MARKETS, rankings_path, load_json, write_json, now_iso


def load_rankings(market_id):
    path = rankings_path(market_id)
    if not path.exists():
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def top_n_by_position(rows, position_field, n=3):
    scored = []
    for r in rows:
        pos = r.get(position_field)
        name = r.get("business_name") or r.get("domain")
        if not pos or not name:
            continue
        try:
            pos = int(float(pos))
        except ValueError:
            continue
        scored.append((pos, name))
    scored.sort(key=lambda t: t[0])
    seen = set()
    out = []
    for _, name in scored:
        if name not in seen:
            seen.add(name)
            out.append(name)
        if len(out) >= n:
            break
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", required=True)
    args = ap.parse_args()

    rows = load_rankings(args.market)
    if not rows:
        raise SystemExit(
            f"No ranking data at {rankings_path(args.market)}. "
            "Run scripts/import_rankings.py first."
        )

    market_path = MARKETS / args.market / "market.json"
    market = load_json(market_path) or {
        "niche": None, "city": None, "state": None,
        "target_keywords": [], "top_competitors": [],
        "last_updated": None, "source_notes": "",
    }

    market["top_organic_competitors"] = top_n_by_position(rows, "organic_position")
    market["top_maps_competitors"] = top_n_by_position(rows, "maps_position")
    market["ranking_urls"] = sorted({r["ranking_url"] for r in rows if r.get("ranking_url")})

    search_volume, cpc, kd = {}, {}, {}
    for r in rows:
        kw = r.get("keyword")
        if not kw:
            continue
        if r.get("search_volume") and kw not in search_volume:
            search_volume[kw] = int(float(r["search_volume"]))
        if r.get("cpc") and kw not in cpc:
            cpc[kw] = float(r["cpc"])
        if r.get("kd") and kw not in kd:
            kd[kw] = float(r["kd"])
    if search_volume:
        market["search_volume"] = search_volume
    if cpc:
        market["cpc"] = cpc
    if kd:
        market["kd"] = kd

    sources = {r["source"] for r in rows if r.get("source")}
    market["source"] = ",".join(sorted(sources)) if sources else market.get("source")
    market["observed_at"] = max((r["observed_at"] for r in rows if r.get("observed_at")), default=now_iso())
    market["last_updated"] = market["observed_at"][:10]

    write_json(market_path, market)
    print(f"Enriched {market_path} from {len(rows)} ranking row(s): "
          f"top_organic={market['top_organic_competitors']}, top_maps={market['top_maps_competitors']}")


if __name__ == "__main__":
    main()
