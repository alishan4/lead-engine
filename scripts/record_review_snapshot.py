#!/usr/bin/env python3
"""
Review snapshot storage -- V3.1.1. Append-only, never overwrites a prior
observation, at data/leads/<slug>/review_snapshots.jsonl. This is what lets
review_velocity_signal become a real, deterministic calculation over time
instead of an inference from a single point-in-time review count (which
V3.1 correctly never allowed).

Usage:
  python3 scripts/record_review_snapshot.py --id <slug> --review-count 45 \\
      --rating 4.7 --source "https://www.bbb.org/... (fetched 2026-09-02)"
"""
import argparse

from _lib import lead_dir, append_jsonl, now_iso


def snapshot_path(prospect_id):
    return lead_dir(prospect_id) / "review_snapshots.jsonl"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    ap.add_argument("--review-count", type=int, required=True)
    ap.add_argument("--rating", type=float, default=None)
    ap.add_argument("--source", required=True)
    ap.add_argument("--observed-at", default=None, help="ISO timestamp; defaults to now")
    args = ap.parse_args()

    snapshot = {
        "business_id": args.id,
        "observed_at": args.observed_at or now_iso(),
        "review_count": args.review_count,
        "rating": args.rating,
        "source": args.source,
    }
    append_jsonl(snapshot_path(args.id), snapshot)
    print(f"Recorded snapshot for {args.id}: {args.review_count} reviews "
          f"(rating {args.rating}) at {snapshot['observed_at']}. "
          f"Never overwrites -- {snapshot_path(args.id)} is append-only.")


if __name__ == "__main__":
    main()
