#!/usr/bin/env python3
"""
Buying-signal / why-now evidence assessment -- V3.1.1. The research is done
by Claude following prompts/buying-signals.md, returning structured
SignalEvidence objects (never bare booleans); this script persists that
evidence (append-only, alongside anything already imported via
import_buying_signals.py or a prior run of this script), resolves it
deterministically via scripts/signal_evidence.py, and writes the resolved
flat fields (+ confidence tier per signal, for HIGH_PRIORITY's VERIFIED-or-
better gate) onto the prospect record. Never fabricates -- unresolved
signals stay null, exactly as V3.1 required, now with a full evidence trail
behind every non-null value.

Usage:
  python3 scripts/assess_buying_signals.py --id <slug> --print-prompt
  python3 scripts/assess_buying_signals.py --id <slug> --save evidence.json
"""
import argparse
import json
import sys

from _lib import (
    ROOT, PROSPECTS, read_jsonl, write_jsonl, lead_dir, load_json, write_json,
    load_yaml, now_iso,
)
from signal_evidence import resolve_signals, derive_paid_search_organic_gap, compute_review_velocity

PROMPT_PATH = ROOT / "prompts" / "buying-signals.md"

SIGNAL_TYPES = (
    "runs_google_ads", "runs_lsa", "recent_expansion", "new_location",
    "marketing_hiring_signal", "recent_site_investment", "new_high_value_service",
    "multiple_locations",
)
# review_velocity_signal is computed separately from snapshots, not resolved
# from researched evidence items. paid_search_organic_gap is always derived.


def evidence_path(prospect_id):
    return lead_dir(prospect_id) / "buying_signal_evidence.jsonl"


def load_all_evidence(prospect_id):
    return read_jsonl(evidence_path(prospect_id))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    ap.add_argument("--print-prompt", action="store_true")
    ap.add_argument("--save")
    args = ap.parse_args()

    discovered = read_jsonl(PROSPECTS / "discovered.jsonl")
    p = next((r for r in discovered if r["id"] == args.id), None)
    if not p:
        raise SystemExit(f"Prospect {args.id} not found in discovered.jsonl")

    if args.save:
        raw = sys.stdin.read() if args.save == "-" else open(args.save).read()
        result = json.loads(raw)
        new_items = result.get("evidence", [])

        for item in new_items:
            item.setdefault("published_at", None)
            item.setdefault("notes", None)
            evidence_path(args.id).parent.mkdir(parents=True, exist_ok=True)
            from _lib import append_jsonl
            append_jsonl(evidence_path(args.id), item)

        all_items = load_all_evidence(args.id)
        source_cfg = load_yaml("signal_sources.yaml")
        resolved = resolve_signals(all_items, source_cfg)

        review_snapshots = read_jsonl(lead_dir(args.id) / "review_snapshots.jsonl")
        review_velocity = compute_review_velocity(review_snapshots, source_cfg)

        resolved_values = {}
        tiers = {}
        statuses = {}
        for sig in SIGNAL_TYPES:
            r = resolved.get(sig, {"value": None, "tier": None, "status": "NO_EVIDENCE"})
            resolved_values[sig] = r["value"]
            tiers[sig] = r["tier"]
            statuses[sig] = r["status"]
        resolved_values["review_velocity_signal"] = review_velocity if review_velocity != "UNKNOWN" else None
        statuses["review_velocity_signal"] = "RESOLVED" if review_velocity != "UNKNOWN" else "NO_EVIDENCE"

        organic_position = p.get("organic_position")
        resolved_values["paid_search_organic_gap"] = derive_paid_search_organic_gap(
            resolved_values.get("runs_google_ads"), resolved_values.get("runs_lsa"), organic_position,
        )
        tiers["paid_search_organic_gap"] = None  # derived, not independently evidenced -- no tier of its own

        conflicts = [sig for sig, r in resolved.items() if r["status"] == "CONFLICTED"]

        for fname in ("discovered.jsonl", "qualified.jsonl", "manual_review.jsonl", "needs_enrichment.jsonl"):
            recs = read_jsonl(PROSPECTS / fname)
            changed = False
            for r in recs:
                if r["id"] == args.id:
                    r.update(resolved_values)
                    r["buying_signal_tiers"] = tiers
                    r["signal_conflicts"] = conflicts
                    r["status"] = "BUYING_SIGNALS_ASSESSED"
                    changed = True
            if changed:
                write_jsonl(PROSPECTS / fname, recs)

        qual = load_json(lead_dir(args.id) / "qualification_v3.json") or {"prospect_id": args.id}
        qual["buying_signals"] = {
            **resolved_values,
            "tiers": tiers,
            "resolution_status": statuses,
            "conflicts": conflicts,
            "evidence_count": len(all_items),
            "resolved_at": now_iso(),
        }
        write_json(lead_dir(args.id) / "qualification_v3.json", qual)

        confirmed = sum(1 for v in resolved_values.values() if v is not None)
        verified_plus = sum(1 for t in tiers.values() if t in ("VERIFIED", "STRONG_VERIFIED"))
        print(f"{args.id}: BUYING_SIGNALS_ASSESSED -- {confirmed}/{len(resolved_values)} signals resolved "
              f"({verified_plus} at VERIFIED+), {len(conflicts)} conflict(s): {conflicts}")
        for sig, status in statuses.items():
            if status not in ("RESOLVED",):
                print(f"  {sig}: {status}")
        return

    prompt = PROMPT_PATH.read_text()
    print(prompt)
    print("\n---\n## prospect record\n")
    print(json.dumps(p, indent=2))
    existing = load_all_evidence(args.id)
    if existing:
        print("\n## existing evidence already on file for this business (do not re-research these) \n")
        print(json.dumps(existing, indent=2))
    snapshots = read_jsonl(lead_dir(args.id) / "review_snapshots.jsonl")
    if snapshots:
        print("\n## existing review snapshots\n")
        print(json.dumps(snapshots, indent=2))


if __name__ == "__main__":
    main()
