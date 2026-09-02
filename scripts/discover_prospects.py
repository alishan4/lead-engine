#!/usr/bin/env python3
"""
V3.5 -- fresh-prospect discovery. The one stage that genuinely did not
exist before this phase (see run_daily.py's own historical comment: "no
deterministic discovery workflow exists in this codebase"). Follows the
same --print-prompt / --save contract as every other research stage.

Scope is always ONE bounded SUB-NICHE x CITY market cell at a time -- the
caller (acquisition_worker.py) is what enforces how many cells get explored
per run (config/acquisition.yaml: max_fresh_market_cells_per_run) and never
lets this script "search all of the US."

Usage:
  python3 scripts/discover_prospects.py --niche hvac --city Columbus --state OH --print-prompt
  python3 scripts/discover_prospects.py --niche hvac --city Columbus --state OH --save result.json
"""
import argparse
import json
import sys

from _lib import (
    ROOT, PROSPECTS, load_yaml, read_jsonl, append_jsonl, slugify, content_hash,
    now_iso, match_franchise_blocklist,
)

PROMPT_PATH = ROOT / "prompts" / "prospect-discovery.md"


def existing_business_keys(niche, records=None):
    """Business-name (lowercased) and website-domain keys already present in
    the pipeline for this niche -- discovery must never re-propose one of
    these. Spans discovered.jsonl (any status) + rejected.jsonl, since a
    previously-rejected business must not be silently rediscovered."""
    records = records if records is not None else (
        read_jsonl(PROSPECTS / "discovered.jsonl") + read_jsonl(PROSPECTS / "rejected.jsonl")
    )
    keys = set()
    for r in records:
        if r.get("niche") != niche:
            continue
        name = (r.get("business_name") or "").strip().lower()
        if name:
            keys.add(("name", name))
        site = (r.get("website") or "").lower()
        if site:
            domain = site.split("//")[-1].split("/")[0].replace("www.", "")
            keys.add(("domain", domain))
    return keys


def dedupe_key(candidate):
    name = (candidate.get("business_name") or "").strip().lower()
    site = (candidate.get("website") or "").lower()
    domain = site.split("//")[-1].split("/")[0].replace("www.", "") if site else None
    return name, domain


def filter_candidates(candidates, known_keys):
    """
    Pure function -- unit-testable without file I/O. Drops: explicit
    non-independent ownership (obvious chain/franchise the researcher
    already identified as non-independent), no commercial value, and
    anything matching an already-known business by name or domain. Returns
    (kept, dropped_with_reason).
    """
    kept, dropped = [], []
    for c in candidates:
        name, domain = dedupe_key(c)
        if not name:
            dropped.append((c, "missing business_name"))
            continue
        if c.get("independently_owned") is False:
            dropped.append((c, "researcher identified this as a non-independently-operated chain/franchise location"))
            continue
        if c.get("commercial_value_signal") == "none":
            dropped.append((c, "commercial_value_signal is none"))
            continue
        if not c.get("google_dependency_evidence"):
            dropped.append((c, "no stated Google-dependency evidence"))
            continue
        if ("name", name) in known_keys or (domain and ("domain", domain) in known_keys):
            dropped.append((c, "already present in the pipeline (discovered or rejected)"))
            continue
        kept.append(c)
    return kept, dropped


def to_prospect_record(candidate, niche, market_cell):
    business_name = candidate["business_name"]
    city, state = candidate.get("city"), candidate.get("state")
    pid = slugify(niche, city, state, business_name)
    franchise_category, franchise_pattern = match_franchise_blocklist(business_name, candidate.get("website"))
    return {
        "id": pid,
        "business_name": business_name,
        "city": city, "state": state, "country": "US",
        "niche": niche,
        "website": candidate.get("website"),
        "google_business_profile_url": candidate.get("google_business_profile_url"),
        "maps_position": None, "organic_position": None,
        "rating": candidate.get("rating"), "review_count": candidate.get("review_count"),
        "years_in_business": candidate.get("years_in_business"),
        "obvious_website_issue": candidate.get("obvious_website_issue") or [],
        "obvious_gbp_issue": candidate.get("obvious_gbp_issue") or [],
        "service_page_count": None,
        "competitor_gap": [],
        "commercial_value_signal": candidate.get("commercial_value_signal"),
        "verified_business": None,
        "source_notes": f"V3.5 fresh discovery ({market_cell}): {candidate.get('source_notes', '')}",
        "discovered_at": now_iso(),
        "last_audited_at": None,
        "content_hash": content_hash(business_name, candidate.get("website"), now_iso()),
        "status": "DISCOVERED",
        "possible_franchise": bool(franchise_category) or None,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--niche", required=True)
    ap.add_argument("--city", required=True)
    ap.add_argument("--state", required=True)
    ap.add_argument("--print-prompt", action="store_true")
    ap.add_argument("--save")
    args = ap.parse_args()

    market_cell = f"{args.niche} / {args.city}, {args.state}"

    if args.save:
        raw = sys.stdin.read() if args.save == "-" else open(args.save).read()
        result = json.loads(raw)
        candidates = result.get("candidates", [])
        known = existing_business_keys(args.niche)
        kept, dropped = filter_candidates(candidates, known)

        added = []
        for c in kept:
            record = to_prospect_record(c, args.niche, market_cell)
            append_jsonl(PROSPECTS / "discovered.jsonl", record)
            added.append(record["id"])

        print(f"{market_cell}: {len(added)} new DISCOVERED prospect(s) added, "
              f"{len(dropped)} candidate(s) dropped (dedupe/ineligible), "
              f"{result.get('excluded_count', 0)} excluded by the researcher before returning candidates")
        for pid in added:
            print(f"  + {pid}")
        for c, reason in dropped:
            print(f"  - {c.get('business_name', '<unnamed>')}: {reason}")
        return

    known = sorted({k for kind, k in existing_business_keys(args.niche) if kind == "name"})
    prompt = PROMPT_PATH.read_text()
    print(prompt)
    print(f"\n---\n## market cell\n\nniche: {args.niche}\ncity: {args.city}\nstate: {args.state}\n")
    print("\n## already-known businesses for this niche -- do not re-list any of these\n")
    print(json.dumps(known, indent=2))


if __name__ == "__main__":
    main()
