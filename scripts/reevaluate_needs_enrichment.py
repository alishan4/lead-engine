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

V3.7.1: when multiple real, distinct-query ranking observations exist for
one field (e.g. three different service-line keywords), the representative
value written to the prospect record is chosen by
select_representative_position() -- NOT rescore_leads.py's best_position()
(min() across everything, still used unchanged by the V2 track), which
would silently erase a genuine per-query Maps/organic opportunity whenever
the same business also ranks strongly for an unrelated query. See that
function's docstring for the real case (Example Restoration, 2026-09-02)
that exposed this, and docs/LEAD-ENGINE.md for the full diagnosis.

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
from rescore_leads import load_rankings, find_ranking_match, log_enrichment_attempt

SCRIPTS = ROOT / "scripts"
RANKING_FIELDS = ("maps_position", "organic_position")

# V3.7.1 -- must match scripts/score_leads.py: score_gap()'s hardcoded
# opportunity bands exactly (4 <= maps_position <= 15, 5 <= organic_position
# <= 30). Duplicated here rather than imported because score_gap() itself
# is frozen scoring logic this patch does not touch; these two tuples are
# the ONLY place that duplication lives, and both are covered by a static
# test (test_opportunity_bands_match_score_gap) that fails loudly if
# score_leads.py's literals ever change without this being updated too.
OPPORTUNITY_BANDS = {"maps_position": (4, 15), "organic_position": (5, 30)}


def real_positions(matches, field):
    """Pure: every (position, query) pair from `matches` with a real,
    exact_rank_verified position for `field` -- one entry per matched row,
    nothing deduped or blended. This is the full per-query evidence, always
    preserved regardless of which single value (if any) ends up on the
    prospect record's scalar field."""
    out = []
    for m in matches:
        verified = m.get("exact_rank_verified")
        if verified in ("False", "false", False):
            continue
        v = m.get(field)
        if v in (None, ""):
            continue
        try:
            out.append((int(float(v)), m.get("keyword")))
        except (TypeError, ValueError):
            continue
    return out


def select_representative_position(matches, field):
    """
    V3.7.1 fix -- pure. Replaces the previous call to
    scripts/rescore_leads.py: best_position() for this (V3.1+ re-evaluation)
    path only; best_position() itself, and the V2 track that still uses it,
    are unchanged.

    best_position() reduces every matched row to min(), regardless of which
    query each row is actually for. That is correct for its original use
    case (multiple SOURCES/snapshots confirming the SAME keyword's rank --
    matching is by business identity only, and score_leads.py's V2 scorer
    never contemplated more than one tracked keyword per business). It is
    the wrong tool once genuinely distinct queries are tracked for the same
    business (V3.7's import_ranking_observation.py made that trivial): a
    real case (Example Restoration, 2026-09-02) had min() reduce three real,
    independently verified positions -- "water damage restoration" #6,
    "mold remediation" #4, "fire damage restoration" #2 -- to just #2,
    silently erasing two genuine, evidence-backed opportunities (#6 and #4
    both fall inside score_gap()'s 4-15 "opportunity" band) purely because
    a third, unrelated query happened to rank strongly.

    Rule (avoids both the min() bias above AND the opposite max()/worst
    bias, which could manufacture a fake opportunity from an irrelevant
    long-tail query ranking poorly): a genuine opportunity exists if ANY
    real, independently verified per-query position falls inside the SAME
    band score_gap() already checks. When true, the returned value is a
    REAL observed position for one specific real query -- never an average,
    median, or other estimate; when multiple queries qualify, the smallest
    (strongest) in-band value is used, deterministically, purely so two
    runs over the same data always agree -- score_gap()'s point contribution
    is identical no matter which qualifying value is chosen, since it only
    checks band membership, never the exact number. If no position falls in
    the band (the business ranks well, or poorly, across every tracked
    query alike), the single best (min) real position is returned --
    identical to best_position()'s existing behavior, so a business with
    only one tracked query (the historical, still-common case) scores
    exactly as before.

    Returns a dict: {"value", "query", "opportunity_band_hit",
    "all_observations"} -- "value"/"query" are None only when there is no
    real evidence at all for this field (stays UNKNOWN, never guessed).
    "all_observations" lists every real (position, query) pair considered,
    preserved for the provenance record even though only one value is ever
    written to the prospect's scalar field.
    """
    positions = real_positions(matches, field)
    all_observations = [{"position": v, "query": q} for v, q in positions]
    if not positions:
        return {"value": None, "query": None, "opportunity_band_hit": False, "all_observations": []}

    band = OPPORTUNITY_BANDS.get(field)
    in_band = [(v, q) for v, q in positions if band and band[0] <= v <= band[1]]
    if in_band:
        value, query = min(in_band, key=lambda vq: vq[0])
        return {"value": value, "query": query, "opportunity_band_hit": True, "all_observations": all_observations}

    value, query = min(positions, key=lambda vq: vq[0])
    return {"value": value, "query": query, "opportunity_band_hit": False, "all_observations": all_observations}


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
    null field. Returns (added, selections):
      added -- {field: new_value} for fields actually added (a subset of
        RANKING_FIELDS, possibly empty) -- what actually gets written to
        the prospect record, backward-compatible with the pre-V3.7.1 shape.
      selections -- {field: select_representative_position()'s full dict}
        for the same fields, for provenance -- which query the value came
        from, whether it was an in-band opportunity, and every real
        per-query observation considered, not just the one chosen.
    """
    added, selections = {}, {}
    for field in RANKING_FIELDS:
        if p.get(field) is not None:
            continue  # never overwrite an existing, already-established value
        selection = select_representative_position(matches, field)
        if selection["value"] is not None:
            added[field] = selection["value"]
            selections[field] = selection
    return added, selections


def record_provenance(prospect_id, fields_added, matches, market_id, selections=None):
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
        # V3.7.1 -- full per-field traceability: which specific query each
        # written value came from, whether it reflected a genuine in-band
        # opportunity, and every real per-query observation considered
        # (not just the one written to the scalar field) -- so "the maps
        # position is #6" can always be traced back to exactly which query
        # that #6 was for, never blended with or confused for a different
        # query's number.
        "field_selection": selections or {},
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

    fields_added, selections = apply_new_ranking_fields(p, matches)
    if not fields_added:
        log_enrichment_attempt(pid, market_id, "matched_but_no_usable_position", matched_rows=len(matches))
        logfn(f"{pid}: matched {len(matches)} ranking row(s) but no new, usable maps_position/organic_position "
              "-- left at NEEDS_ENRICHMENT (never overwrites an existing value, never guesses).")
        return "matched_but_no_usable_position"

    # Persist the new field(s) wherever this lead currently lives, exactly
    # like every other stage in this pipeline -- reuses set_status_everywhere
    # so discovered.jsonl and needs_enrichment.jsonl never desync.
    set_status_everywhere(pid, p["status"], extra_fields=fields_added)
    record_provenance(pid, fields_added, matches, market_id, selections=selections)
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
