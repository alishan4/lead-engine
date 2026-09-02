#!/usr/bin/env python3
"""
V3.7 -- deterministic re-evaluation for an existing V3.1+ NEEDS_ENRICHMENT
prospect once new ranking evidence has been imported
(scripts/import_ranking_observation.py or scripts/import_rankings.py) into
data/rankings/<market_id>.csv. Never re-discovers or re-researches the
company -- reuses every already-verified fact on the prospect record
(identity, buying signals, contactability, all untouched) and only fills in
whichever maps_position/organic_position the imported evidence newly
supports, matched by domain then business name using
scripts/rescore_leads.py's own matching functions (imported directly here,
never duplicated).

Note: scripts/rescore_leads.py itself is the pre-existing V2 rescore path
(score_leads.score_with_completeness, status -> RESCORED) and is NOT
V3.1-track aware -- a V3.1+ NEEDS_ENRICHMENT prospect (fit_confirmed_score/
gap_confirmed_score, routed by qualify_leads.py --v3) was never picked up
by it, and a raw "RESCORED" status has no V3.1 re-routing path. This script
is the V3-track-compatible equivalent: it never invents a new scoring
formula -- it just gets the record back to a status the EXISTING,
UNCHANGED V3.1/V3.2 deterministic chain (assess_google_gap.py ->
assess_commercial_fit.py -> qualify_leads.py --v3) already knows how to
carry forward, exactly as if the ranking data had been available on day
one.

Never overwrites an existing maps_position/organic_position (only fills a
currently-null field). Prior evidence (qualification_v3.json's identity/
buying_signals/contactability/gap/fit sections) is never deleted or
replaced -- this script only ever APPENDS a new "ranking_reevaluations"
entry recording provenance (source, observed_at, fields added, matched row
count, when this run happened).

No Claude call anywhere in this script -- fully deterministic, like
rescore_leads.py, assess_google_gap.py, and assess_commercial_fit.py, all
of which it calls unchanged.

Usage:
  python3 scripts/reevaluate_needs_enrichment.py --id <slug>
  python3 scripts/reevaluate_needs_enrichment.py --market roofing-columbus-oh   # every NEEDS_ENRICHMENT lead in that market
  python3 scripts/reevaluate_needs_enrichment.py --all                          # every NEEDS_ENRICHMENT lead
"""
import argparse
import subprocess
import sys

from _lib import (
    PROSPECTS, LEADS, ROOT, read_jsonl, load_json, write_json, market_slug,
    set_status_everywhere, now_iso,
)
from rescore_leads import load_rankings, find_ranking_match, best_position, log_enrichment_attempt

SCRIPTS = ROOT / "scripts"
RANKING_FIELDS = ("maps_position", "organic_position")


def get_needs_enrichment(id_filter=None, market_filter=None):
    records = read_jsonl(PROSPECTS / "needs_enrichment.jsonl")
    if id_filter:
        return [r for r in records if r["id"] == id_filter]
    if market_filter:
        return [r for r in records if market_slug(r.get("niche"), r.get("city"), r.get("state")) == market_filter]
    return records


def apply_new_ranking_fields(p, matches):
    """
    Pure-ish (only reads `matches`/`p`, returns the fields to add -- caller
    persists): never overwrites an existing value, only fills a currently-
    null field. Returns {field: new_value} for fields actually added (a
    subset of RANKING_FIELDS, possibly empty).
    """
    added = {}
    for field in RANKING_FIELDS:
        if p.get(field) is not None:
            continue  # never overwrite an existing, already-established value
        new_value = best_position(matches, field)
        if new_value is not None:
            added[field] = new_value
    return added


def record_provenance(prospect_id, fields_added, matches, market_id):
    """Appends (never overwrites) a ranking_reevaluations entry onto the
    lead's qualification_v3.json -- every prior section (fit/gap/buying_signals/
    franchise/etc.) is left completely untouched.

    PROVENANCE GUARANTEE (2026-09-02 review): `sources` below is built only
    from matched rows' real `source` field (never defaulted/fabricated) --
    unlike scripts/rescore_leads.py's V2 enrichment_source, which falls
    back to the string literal "manual_csv" when no matched row has a
    truthy source, this records an honest empty list `[]` in that
    (currently unreachable in practice) case rather than presenting a
    specific, unverified provenance label as if it were real. In practice
    `source` is never actually missing on a matched row: both scripts that
    can ever write to data/rankings/<market_id>.csv (import_rankings.py and
    import_ranking_observation.py) reject a missing/unknown source before
    writing at all -- see import_ranking_observation.py: validate_observation
    for the full write-time enforcement this depends on."""
    qual_path = LEADS / prospect_id / "qualification_v3.json"
    qual = load_json(qual_path) or {"prospect_id": prospect_id}
    entries = qual.get("ranking_reevaluations") or []
    sources = sorted({m["source"] for m in matches if m.get("source")})
    observed_ats = sorted({m["observed_at"] for m in matches if m.get("observed_at")})
    entries.append({
        "market_id": market_id,
        "fields_added": fields_added,
        "matched_rows": len(matches),
        "sources": sources,
        "observed_at": max(observed_ats) if observed_ats else None,
        "reevaluated_at": now_iso(),
    })
    qual["ranking_reevaluations"] = entries
    write_json(qual_path, qual)


def run_deterministic_stage(script_name, prospect_id, logfn):
    proc = subprocess.run([sys.executable, script_name, "--id", prospect_id], cwd=SCRIPTS,
                           capture_output=True, text=True, timeout=60)
    logfn(f"  $ {script_name} --id {prospect_id} -> exit {proc.returncode}")
    if proc.stdout.strip():
        logfn(f"    {proc.stdout.strip().splitlines()[-1]}")
    return proc.returncode == 0


def reevaluate_one(p, logfn=print):
    """Returns one of: 'no_matching_rows', 'matched_but_no_usable_position',
    'fields_added', 'stage_failed'. Never raises for a single lead's
    problem -- callers processing multiple leads must isolate failures the
    same way run_daily.py/acquisition_worker.py already do elsewhere."""
    pid = p["id"]
    market_id = market_slug(p.get("niche"), p.get("city"), p.get("state"))
    rows = load_rankings(market_id)
    if not rows:
        log_enrichment_attempt(pid, market_id, "no_matching_rows")
        logfn(f"{pid}: no ranking data imported yet for {market_id} -- left at NEEDS_ENRICHMENT.")
        return "no_matching_rows"

    matches = find_ranking_match(rows, p.get("business_name"), p.get("website"))
    if not matches:
        log_enrichment_attempt(pid, market_id, "no_matching_rows")
        logfn(f"{pid}: ranking data exists for {market_id} but none matched this business's name/domain "
              "-- left at NEEDS_ENRICHMENT.")
        return "no_matching_rows"

    fields_added = apply_new_ranking_fields(p, matches)
    if not fields_added:
        log_enrichment_attempt(pid, market_id, "matched_but_no_usable_position", matched_rows=len(matches))
        logfn(f"{pid}: matched {len(matches)} ranking row(s) but no new, usable maps_position/organic_position "
              "-- left at NEEDS_ENRICHMENT (never overwrites an existing value, never guesses).")
        return "matched_but_no_usable_position"

    # Persist the new field(s) wherever this lead currently lives, exactly
    # like every other stage in this pipeline -- reuses set_status_everywhere
    # so discovered.jsonl and needs_enrichment.jsonl never desync.
    set_status_everywhere(pid, p["status"], extra_fields=fields_added)
    record_provenance(pid, fields_added, matches, market_id)
    log_enrichment_attempt(pid, market_id, "fields_added", fields_added=list(fields_added), matched_rows=len(matches))
    logfn(f"{pid}: new ranking evidence applied -- {fields_added} (source(s): "
          f"{sorted({m.get('source') for m in matches if m.get('source')})}). Re-running the deterministic chain...")

    # Re-run the EXACT same deterministic chain a first-time pass would have
    # used, unchanged: GAP depends on maps_position/organic_position, FIT
    # doesn't but only advances once GAP has run (assess_commercial_fit.py's
    # own advance_from gate already includes GOOGLE_GAP_ASSESSED).
    ok1 = run_deterministic_stage("assess_google_gap.py", pid, logfn)
    ok2 = run_deterministic_stage("assess_commercial_fit.py", pid, logfn) if ok1 else False
    if not (ok1 and ok2):
        logfn(f"{pid}: ranking evidence was applied, but the deterministic re-scoring chain failed to complete -- "
              "re-run this script for this lead once fixed. New evidence is preserved either way.")
        return "stage_failed"
    return "fields_added"


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--id")
    g.add_argument("--market", help="market_id, e.g. roofing-columbus-oh")
    g.add_argument("--all", action="store_true")
    args = ap.parse_args()

    if args.id:
        targets = get_needs_enrichment(id_filter=args.id)
        if not targets:
            raise SystemExit(f"{args.id} not found in needs_enrichment.jsonl (already re-routed, or never was NEEDS_ENRICHMENT?)")
    elif args.market:
        targets = get_needs_enrichment(market_filter=args.market)
    else:
        targets = get_needs_enrichment()

    outcomes = {"no_matching_rows": 0, "matched_but_no_usable_position": 0, "fields_added": 0, "stage_failed": 0}
    failures = []
    for p in targets:
        try:
            outcome = reevaluate_one(p)
            outcomes[outcome] += 1
        except Exception as e:
            failures.append({"prospect_id": p.get("id"), "error": str(e)[:400], "timestamp": now_iso()})
            print(f"  ! {p.get('id')}: re-evaluation failed -- {e}")
            continue

    if outcomes["fields_added"] > 0:
        proc = subprocess.run([sys.executable, "qualify_leads.py", "--v3"], cwd=SCRIPTS, capture_output=True, text=True, timeout=60)
        print(f"  $ qualify_leads.py --v3 -> exit {proc.returncode}")
        if proc.stdout.strip():
            print(f"    {proc.stdout.strip().splitlines()[-1]}")

    print(f"reevaluate_needs_enrichment: {len(targets)} lead(s) checked -- {outcomes}, {len(failures)} failure(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
