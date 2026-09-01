#!/usr/bin/env python3
"""
Manual/external buying-signal import -- V3.1.1. Lets real evidence from a
SERP export, Semrush, manual Maps/Ads observation, job-board research, or
any future external tool enter the same SignalEvidence model
(schemas/signal_evidence.schema.json) as the research-prompt path
(assess_buying_signals.py), with NO special-case downstream logic --
scripts/signal_evidence.py resolves both the same way.

Validates strictly: a malformed row is rejected and reported, never
silently coerced or defaulted into passing (a missing entity_match_confidence
or confidence is a hard reject, not a guess).

Usage:
  python3 scripts/import_buying_signals.py --file data/enrichment/buying-signals.csv
"""
import argparse
import csv
import json
from pathlib import Path

from _lib import DATA, lead_dir, append_jsonl, now_iso

REQUIRED_COLUMNS = (
    "business_id", "signal_type", "value", "source", "source_type",
    "observed_at", "confidence", "evidence", "entity_match_confidence",
)
VALID_SOURCE_TYPES = {
    "official_website", "official_careers", "official_press_release", "official_social",
    "google_serp", "google_maps", "google_business_profile", "semrush", "job_posting",
    "government_registry", "licensing_source", "credible_news", "credible_business_directory",
    "historical_snapshot", "manual_import", "other",
}


def _parse_bool_or_str(raw, signal_type):
    if raw is None or raw == "":
        return None, "value is required"
    low = raw.strip().lower()
    if low in ("true", "false"):
        return low == "true", None
    if signal_type == "review_velocity_signal":
        if raw.strip().upper() in ("STRONG", "MODERATE", "LOW", "UNKNOWN"):
            return raw.strip().upper(), None
        return None, f"invalid review_velocity_signal value: {raw!r}"
    return None, f"value {raw!r} is not a recognized boolean for signal_type {signal_type!r}"


def _parse_float(raw, field, lo=0.0, hi=1.0):
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None, f"{field} must be a number, got {raw!r}"
    if not (lo <= v <= hi):
        return None, f"{field} must be between {lo} and {hi}, got {v}"
    return v, None


def validate_row(row, line_no):
    errors = []
    for col in REQUIRED_COLUMNS:
        if not row.get(col):
            errors.append(f"line {line_no}: missing required column '{col}'")
    if errors:
        return None, errors

    value, err = _parse_bool_or_str(row["value"], row["signal_type"])
    if err:
        errors.append(f"line {line_no}: {err}")

    confidence, err = _parse_float(row["confidence"], "confidence")
    if err:
        errors.append(f"line {line_no}: {err}")

    entity_match_confidence, err = _parse_float(row["entity_match_confidence"], "entity_match_confidence")
    if err:
        errors.append(f"line {line_no}: {err}")

    if row["source_type"] not in VALID_SOURCE_TYPES:
        errors.append(f"line {line_no}: unknown source_type {row['source_type']!r}")

    try:
        from datetime import datetime
        observed_at = datetime.fromisoformat(row["observed_at"])
    except ValueError:
        errors.append(f"line {line_no}: observed_at {row['observed_at']!r} is not a valid ISO timestamp/date")
        observed_at = None

    if errors:
        return None, errors

    evidence_item = {
        "signal_type": row["signal_type"],
        "value": value,
        "confidence": confidence,
        "source": row["source"],
        "source_type": row["source_type"],
        "observed_at": row["observed_at"],
        "published_at": row.get("published_at") or None,
        "evidence": row["evidence"],
        "entity_match_confidence": entity_match_confidence,
        "notes": row.get("notes") or None,
    }
    return {"business_id": row["business_id"], "item": evidence_item}, []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    args = ap.parse_args()

    path = Path(args.file)
    if not path.exists():
        raise SystemExit(f"File not found: {path}")

    imported, skipped = 0, []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader, start=2):  # header is line 1
            result, errors = validate_row(row, i)
            if errors:
                skipped.append({"line": i, "errors": errors})
                continue
            evidence_path = lead_dir(result["business_id"]) / "buying_signal_evidence.jsonl"
            append_jsonl(evidence_path, result["item"])
            imported += 1

    print(f"Imported {imported} evidence row(s) from {path.name}.")
    if skipped:
        print(f"Rejected {len(skipped)} malformed row(s) (imported nothing from these, no defaults guessed):")
        for s in skipped:
            for e in s["errors"]:
                print(f"  {e}")


if __name__ == "__main__":
    main()
