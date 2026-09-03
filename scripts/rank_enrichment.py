#!/usr/bin/env python3
"""
V3.8 -- Automated Ranking Enrichment. Turns NEEDS_ENRICHMENT from a parking
lot into a temporary queue: it does NOT re-discover, re-verify, or re-score
a business's FIT -- every one of those facts is already correct and
untouched on the prospect record. The single thing missing is ranking
evidence (maps_position/organic_position), and that is the ONLY thing this
module tries to fill in, using the exact same deterministic, no-Claude
machinery V3.7/V3.7.1 already built (scripts/import_ranking_observation.py,
scripts/reevaluate_needs_enrichment.py) -- nothing here duplicates or
loosens either.

Flow (see OPERATING-RULES.md's V3.8 update for the full policy context):

  NEEDS_ENRICHMENT queue (real records, MANUAL_REVIEW never included)
    -> prioritize (FIT confirmed, GAP potential, niche tier, contactability,
       evidence completeness)
    -> select a small bounded set of money queries per lead (reuses
       config/niches.yaml money_keywords -- never invents a keyword)
    -> ask the configured provider chain (scripts/ranking_providers.py) for
       each query, in priority order, bounded by
       config/ranking_enrichment.yaml's per-run/per-lead/per-query caps
    -> import any genuinely new, validated observation via the existing
       scripts/import_ranking_observation.py choke point
    -> run the existing, unchanged scripts/reevaluate_needs_enrichment.py
       deterministic re-evaluation for every lead attempted this cycle
    -> QUALIFIED/HIGH_PRIORITY/MANUAL_REVIEW/REJECTED, or still
       NEEDS_ENRICHMENT if no trustworthy evidence was available
       (RANKING_SOURCE_REQUIRED) -- never guessed past.

No Claude call, no live network call, no credential, anywhere in this
module. This is deliberately cheap enough to run every day before any
fresh-discovery Claude spend -- see scripts/acquisition_worker.py's `run()`
for where this is invoked in the daily order.

Usage:
  python3 scripts/rank_enrichment.py               # run one enrichment cycle, print stats JSON
  python3 scripts/rank_enrichment.py --dry-run-queue  # print the prioritized queue only, touch nothing
"""
import argparse
import json
import subprocess
import sys

from _lib import ROOT, PROSPECTS, LEADS, load_yaml, read_jsonl, market_slug, now_iso
from rescore_leads import domain_of
from import_ranking_observation import import_observations
from ranking_providers import (
    build_providers, attempt_query, STATUS_OBSERVATION, STATUS_ALREADY_SATISFIED,
    STATUS_SOURCE_REQUIRED, STATUS_FAILURE,
)
import reevaluate_needs_enrichment as ren

SCRIPTS = ROOT / "scripts"

RANKING_FIELDS = ("maps_position", "organic_position")


# ---------------------------------------------------------------------------
# 1. Priority queue -- pure
# ---------------------------------------------------------------------------

def _niche_tier(niche, niches_cfg):
    return ((niches_cfg.get("niches") or {}).get(niche) or {}).get("tier", 3)


def _evidence_completeness(p):
    """Average of whatever completeness signals the record already carries
    (V3.1 fit_completeness/gap_completeness, falling back to V2's
    data_completeness) -- never fabricated, 0 when genuinely unknown."""
    vals = [v for v in (p.get("fit_completeness"), p.get("gap_completeness"), p.get("data_completeness")) if v is not None]
    return (sum(vals) / len(vals)) if vals else 0


def enrichment_priority_key(p, niches_cfg):
    """Pure. Lower sorts first. Order, per the V3.8 spec:
    1. FIT confirmed (higher first)
    2. GAP potential (higher first)
    3. niche commercial value (tier 1 = highest value, sorts first)
    4. contactability (higher first)
    5. evidence completeness (higher first)
    then a deterministic id tiebreak so two runs over the same data always
    agree on ordering."""
    return (
        -(p.get("fit_confirmed_score") or 0),
        -(p.get("gap_potential_score") or 0),
        _niche_tier(p.get("niche"), niches_cfg),
        -(p.get("contactability_score") or 0),
        -_evidence_completeness(p),
        p.get("id") or "",
    )


def build_enrichment_queue(records, niches_cfg):
    """Pure: defensively filters to status == NEEDS_ENRICHMENT only, even if
    handed a mixed list -- MANUAL_REVIEW (or any other status) must never be
    treated as rank-only auto-qualifiable; those leads need a human FIT call,
    not ranking evidence (see OPERATING-RULES.md's V3.8 update)."""
    eligible = [r for r in records if r.get("status") == "NEEDS_ENRICHMENT"]
    return sorted(eligible, key=lambda p: enrichment_priority_key(p, niches_cfg))


# ---------------------------------------------------------------------------
# 2. Query selection -- pure
# ---------------------------------------------------------------------------

def select_queries(prospect, niches_cfg, cfg):
    """Pure: 2-4 (config-bounded) money-query records for one lead, reusing
    config/niches.yaml's existing money_keywords -- never generates a novel
    keyword, never uses Claude. Returns [] when there is genuinely nothing
    useful to ask (no keywords configured for this niche, no city/state, or
    both maps_position/organic_position are already known -- nothing
    missing to enrich)."""
    niche = prospect.get("niche")
    money_keywords = ((niches_cfg.get("niches") or {}).get(niche) or {}).get("money_keywords") or []
    city, state = prospect.get("city"), prospect.get("state")
    if not money_keywords or not city or not state:
        return []

    needed_types = [t for t, field in (("MAPS", "maps_position"), ("ORGANIC", "organic_position"))
                    if prospect.get(field) is None]
    if not needed_types:
        return []

    max_q = max(1, cfg.get("max_queries_per_lead", 4))
    # Interleave keyword-major (kw1/MAPS, kw1/ORGANIC, kw2/MAPS, ...) rather
    # than type-major -- otherwise a niche with >= max_q money_keywords and
    # both fields missing would exhaust the whole query budget on MAPS
    # alone and never ask an ORGANIC query at all.
    combos = [(kw, etype) for kw in money_keywords for etype in needed_types][:max_q]

    domain = domain_of(prospect.get("website"))
    out = []
    for kw, etype in combos:
        query_text = f"{kw} {city} {state}".lower()
        out.append({
            "query": query_text,
            "location": f"{city}, {state}",
            "business_name": prospect.get("business_name"),
            "domain": domain,
            "niche": niche,
            "intended_evidence_type": etype,
            "why": (f"money keyword '{kw}' x {city}, {state} -- "
                    f"{'Maps/local-pack' if etype == 'MAPS' else 'organic'} position is the missing "
                    f"evidence currently blocking this lead's GAP score"),
        })
    return out


# ---------------------------------------------------------------------------
# 3. Orchestration
# ---------------------------------------------------------------------------

def empty_stats():
    return {
        "ranking_backlog_before": 0,
        "ranking_leads_attempted": 0,
        "ranking_queries_attempted": 0,
        "ranking_observations_imported": 0,
        "ranking_provider_failures": 0,
        "ranking_backlog_after": 0,
        "qualified_after_ranking": 0,
        "still_needs_enrichment": 0,
        "ranking_cost_estimate": 0.0,  # every configured provider is file-based/zero-cost -- honestly 0.0, never invented
        "ranking_failures": [],
    }


def run_cycle(cfg=None, niches_cfg=None, log=print, deadline=None):
    """Runs one bounded ranking-enrichment cycle. Never raises for a single
    lead/query's problem (see attempt_query's own isolation) -- only a
    config/IO error at setup time propagates, exactly like every other
    stage in this pipeline. Returns the stats dict described in the V3.8
    spec's Sec.15 (reporting)."""
    cfg = cfg or load_yaml("ranking_enrichment.yaml")
    niches_cfg = niches_cfg or load_yaml("niches.yaml")
    stats = empty_stats()

    all_needs_enrichment = read_jsonl(PROSPECTS / "needs_enrichment.jsonl")
    queue = build_enrichment_queue(all_needs_enrichment, niches_cfg)
    stats["ranking_backlog_before"] = len(queue)
    if not queue:
        stats["ranking_backlog_after"] = 0
        stats["still_needs_enrichment"] = len(all_needs_enrichment)
        return stats

    providers = build_providers(cfg)
    freshness_days = cfg.get("freshness_days", 14)
    max_leads = cfg.get("max_enrichment_leads_per_run", 8)
    max_requests = cfg.get("max_provider_requests_per_run", 24)

    requests_used = 0
    attempted_ids = []
    for p in queue[:max_leads]:
        if deadline is not None and deadline.exceeded():
            log("  ranking_enrichment: worker deadline reached -- remaining backlog carried over, untouched")
            break
        if requests_used >= max_requests:
            log(f"  ranking_enrichment: provider request budget ({max_requests}) exhausted this cycle -- "
                f"remaining leads carried over, untouched")
            break

        pid = p["id"]
        market_id = market_slug(p.get("niche"), p.get("city"), p.get("state"))
        queries = select_queries(p, niches_cfg, cfg)
        if not queries:
            continue  # nothing this module can usefully ask for this lead this cycle

        attempted_ids.append(pid)
        stats["ranking_leads_attempted"] += 1
        new_observations = []
        for qr in queries:
            if requests_used >= max_requests:
                break
            qr["market_id"] = market_id
            stats["ranking_queries_attempted"] += 1
            requests_used += 1
            result = attempt_query(providers, qr, freshness_days, log=log)
            if result.status == STATUS_OBSERVATION:
                new_observations.append(result.observation)
            elif result.status == STATUS_FAILURE:
                stats["ranking_provider_failures"] += 1
                stats["ranking_failures"].append({
                    "prospect_id": pid, "query": qr["query"], "provider": result.provider,
                    "reason": result.reason, "at": now_iso(),
                })
            elif result.status == STATUS_ALREADY_SATISFIED:
                log(f"  {pid}: {qr['query']!r} already satisfied by on-file evidence -- {result.reason}")
            else:  # RANKING_SOURCE_REQUIRED
                log(f"  {pid}: {qr['query']!r} -- RANKING_SOURCE_REQUIRED ({result.reason})")

        if new_observations:
            imported, import_failures = import_observations(new_observations, logfn=log)
            stats["ranking_observations_imported"] += imported
            for f in import_failures:
                stats["ranking_provider_failures"] += 1
                stats["ranking_failures"].append({"prospect_id": pid, "reason": f["error"], "at": now_iso()})

    # Deterministic re-evaluation (V3.7/V3.7.1, unchanged) for every lead
    # actually attempted this cycle -- cheap, idempotent, no Claude call.
    # This is what actually applies whatever evidence (already-on-file or
    # freshly-imported above) into the prospect record and re-routes it.
    any_fields_added = False
    for pid in attempted_ids:
        current = next((r for r in read_jsonl(PROSPECTS / "needs_enrichment.jsonl") if r["id"] == pid), None)
        if not current:
            continue  # already routed out of NEEDS_ENRICHMENT earlier this cycle -- nothing left to do
        outcome = ren.reevaluate_one(current, logfn=log)
        if outcome == "fields_added":
            any_fields_added = True

    if any_fields_added:
        proc = subprocess.run([sys.executable, "qualify_leads.py", "--v3"], cwd=SCRIPTS,
                               capture_output=True, text=True, timeout=60)
        log(f"  $ qualify_leads.py --v3 -> exit {proc.returncode}")

    final_needs_enrichment = read_jsonl(PROSPECTS / "needs_enrichment.jsonl")
    stats["still_needs_enrichment"] = len(final_needs_enrichment)
    stats["ranking_backlog_after"] = len(build_enrichment_queue(final_needs_enrichment, niches_cfg))
    qualified_ids_after = {r["id"] for r in read_jsonl(PROSPECTS / "qualified.jsonl")}
    stats["qualified_after_ranking"] = sum(1 for pid in attempted_ids if pid in qualified_ids_after)
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run-queue", action="store_true",
                     help="Print the prioritized enrichment queue only -- runs no provider, imports nothing, "
                          "mutates nothing. Safe to run against real production data at any time.")
    args = ap.parse_args()

    niches_cfg = load_yaml("niches.yaml")
    if args.dry_run_queue:
        records = read_jsonl(PROSPECTS / "needs_enrichment.jsonl")
        queue = build_enrichment_queue(records, niches_cfg)
        for p in queue:
            print(f"{p['id']}: fit_confirmed={p.get('fit_confirmed_score')} gap_potential={p.get('gap_potential_score')} "
                  f"niche_tier={_niche_tier(p.get('niche'), niches_cfg)} contactability={p.get('contactability_score')} "
                  f"completeness={_evidence_completeness(p):.0f}")
        print(f"ranking_backlog_size={len(queue)}")
        return 0

    # Progress goes to stderr, not stdout -- stdout carries ONLY the final
    # JSON stats block, so a caller (a human, or run_daily.py/
    # acquisition_worker.py in the future) can always parse it cleanly.
    stats = run_cycle(log=lambda m: print(m, file=sys.stderr))
    print(json.dumps(stats, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
