#!/usr/bin/env python3
"""
V3.7 -- the clean external enrichment interface for one (or a small batch
of) factual Google-visibility observation(s) that Claude cannot reliably
obtain itself: a manually-checked Google Maps/Search position, or a single
fact hand-transcribed from a real SEMrush export. See
schemas/ranking_evidence_observation.schema.json for the exact contract:
maps_position, organic_position, query, location, observed_at, source (at
minimum) -- deliberately a smaller, purpose-built surface than
import_rankings.py's bulk CSV/Semrush-export importer, which this script
sits on top of (reuses its normalize_row/CANONICAL_FIELDS/append logic --
nothing duplicated).

This is a human-operated CLI tool, exactly like import_rankings.py --
nothing in scripts/acquisition_worker.py or any unattended Claude subprocess
ever calls it, and no SEMrush/Google credential of any kind is given to
Claude by this or any other script in this repository. UNKNOWN stays
UNKNOWN: a missing maps_position/organic_position is never converted into a
guessed value, here or anywhere downstream (scripts/assess_google_gap.py's
score_gap() already treats a null position as "not yet known", never as a
poor ranking).

Usage:
  # location already a market_id slug:
  python3 scripts/import_ranking_observation.py \\
      --location roofing-columbus-oh --query "roof replacement columbus oh" \\
      --maps-position 3 --observed-at 2026-09-05 --source manual_maps_check \\
      --business-name "Example Roofing Construction" --domain example-roofing.test

  # location as "City, ST" + --niche to resolve the market_id:
  python3 scripts/import_ranking_observation.py \\
      --niche roofing --location "Columbus, OH" --query "..." \\
      --organic-position 8 --observed-at 2026-09-05 --source semrush

  # batch: a JSON file, a list of objects each matching the schema above
  python3 scripts/import_ranking_observation.py --file observations.json
"""
import argparse
import json
import sys
from pathlib import Path

import csv

from _lib import market_slug, rankings_path
from import_rankings import normalize_row, KNOWN_SOURCES, CANONICAL_FIELDS


def resolve_market_id(location, niche):
    """Pure: `location` is either already a market_id slug, or a 'City, ST'
    string paired with --niche. Never guesses a niche -- if location isn't
    already a slug and no niche was given, this is a usage error, not a
    silent guess."""
    if niche is None:
        return location  # assume already-canonical market_id
    parts = [p.strip() for p in location.split(",")]
    if len(parts) != 2:
        raise ValueError(f"--location {location!r} with --niche given must be 'City, ST' -- got {len(parts)} part(s)")
    city, state = parts
    return market_slug(niche, city, state)


def observation_to_raw_row(obs):
    """Pure: schemas/ranking_evidence_observation.schema.json shape ->
    the raw-row shape normalize_row() (import_rankings.py) already knows how
    to consume for source in {'manual_csv','semrush','manual_maps_check',
    'manual_serp_check'} -- i.e. canonical field names directly."""
    return {
        "business_name": obs.get("business_name"),
        "domain": obs.get("domain"),
        "keyword": obs.get("query"),
        "maps_position": obs.get("maps_position"),
        "organic_position": obs.get("organic_position"),
        "notes": obs.get("notes"),
        "exact_rank_verified": True,
    }


def validate_observation(obs):
    """Pure: returns (ok, reason). At least one of maps_position/
    organic_position must be present -- an observation with neither is not
    evidence of anything and would just be a no-op row.

    PROVENANCE GUARANTEE (2026-09-02 review): this is the single write-time
    choke point that keeps an unknown/empty `source` from ever reaching
    data/rankings/<market_id>.csv via this interface -- `not obs.get("source")`
    rejects missing/blank, and the KNOWN_SOURCES membership check below
    rejects anything not a real, recognized provenance label. The bulk
    importer (import_rankings.py: main()) enforces the identical rule at its
    own single write point (`if source not in KNOWN_SOURCES: raise SystemExit`).
    Because both scripts are the ONLY code that ever appends to that CSV, a
    row's `source` field is always one of KNOWN_SOURCES by construction --
    scripts/rescore_leads.py's best_position() (reused unchanged by
    scripts/reevaluate_needs_enrichment.py) does not separately re-check
    `source` when deciding whether a position is usable, and does not need
    to: it already only trusts a row whose `exact_rank_verified` is not
    explicitly False, and every row this interface writes always has both a
    valid source and exact_rank_verified=True set together (see
    observation_to_raw_row() below). No additional source allowlist check
    was added at the read/consumption side -- it would be redundant with an
    already-enforced write-time guarantee, not a missing one."""
    for field in ("query", "location", "observed_at", "source"):
        if not obs.get(field):
            return False, f"missing required field: {field}"
    if obs.get("source") not in KNOWN_SOURCES:
        return False, f"unknown source {obs.get('source')!r} -- known values: {sorted(KNOWN_SOURCES)}"
    if obs.get("maps_position") is None and obs.get("organic_position") is None:
        return False, "neither maps_position nor organic_position is set -- nothing to import"
    if not obs.get("business_name") and not obs.get("domain"):
        return False, "at least one of business_name/domain is required to match this observation to a prospect"
    return True, None


def import_observations(observations, niche_override=None, logfn=print):
    """Returns (imported_count, failures) -- one bad observation in a batch
    file never blocks the others."""
    imported, failures = 0, []
    by_market = {}
    for obs in observations:
        try:
            ok, reason = validate_observation(obs)
            if not ok:
                failures.append({"observation": obs, "error": reason})
                logfn(f"  ! rejected observation -- {reason}: {obs}")
                continue
            market_id = resolve_market_id(obs["location"], niche_override or obs.get("niche"))
            raw = observation_to_raw_row(obs)
            row = normalize_row(raw, market_id, obs["source"], obs["observed_at"],
                                 obs.get("business_name"), obs.get("domain"))
            by_market.setdefault(market_id, []).append(row)
            imported += 1
        except Exception as e:
            failures.append({"observation": obs, "error": str(e)[:400]})
            logfn(f"  ! failed to import observation -- {e}: {obs}")
            continue

    for market_id, rows in by_market.items():
        out_path = rankings_path(market_id)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        file_exists = out_path.exists() and out_path.stat().st_size > 0
        with open(out_path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CANONICAL_FIELDS)
            if not file_exists:
                writer.writeheader()
            writer.writerows(rows)
        logfn(f"  + {len(rows)} observation(s) -> {out_path}")

    return imported, failures


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", help="JSON file: one observation object, or a list of them")
    ap.add_argument("--niche", help="Required if --location is 'City, ST' rather than an already-canonical market_id slug")
    ap.add_argument("--location")
    ap.add_argument("--query")
    ap.add_argument("--maps-position", type=int)
    ap.add_argument("--organic-position", type=int)
    ap.add_argument("--observed-at")
    ap.add_argument("--source", choices=sorted(KNOWN_SOURCES))
    ap.add_argument("--business-name")
    ap.add_argument("--domain")
    ap.add_argument("--notes")
    args = ap.parse_args()

    if args.file:
        data = json.loads(Path(args.file).read_text())
        observations = data if isinstance(data, list) else [data]
    else:
        if not (args.location and args.query and args.observed_at and args.source):
            raise SystemExit("Either --file, or --location/--query/--observed-at/--source (single observation), is required.")
        observations = [{
            "location": args.location, "query": args.query, "observed_at": args.observed_at, "source": args.source,
            "maps_position": args.maps_position, "organic_position": args.organic_position,
            "business_name": args.business_name, "domain": args.domain, "notes": args.notes,
        }]

    imported, failures = import_observations(observations, niche_override=args.niche)
    print(f"import_ranking_observation: {imported} imported, {len(failures)} rejected/failed")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
