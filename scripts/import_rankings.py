#!/usr/bin/env python3
"""
Normalize a ranking data export into schemas/ranking_snapshot.schema.json and
append it to data/rankings/<market_id>.csv. No paid API required -- this is
purely a file importer for data you already have.

Supported inputs (auto-detected by extension/columns):
  1. Semrush "Organic Research > Positions" CSV export
  2. A manually-prepared CSV using the canonical column names directly
  3. A JSON snapshot: a list of objects, each close to the canonical schema
  4. XLSX, only if `openpyxl` happens to be installed (not a required dep) --
     otherwise this script tells you to re-export as CSV instead.

None of these require config/routing.yaml's never_auto_run specialists
(seo-dataforseo, seo-google) to run -- this is how V2 gets real ranking data
into the scorer without ever calling a paid API automatically.

Usage:
  python3 scripts/import_rankings.py --market hvac-nashville-tn --file export.csv
  python3 scripts/import_rankings.py --market hvac-nashville-tn --file snapshot.json
"""
import argparse
import csv
import json
import sys
from pathlib import Path

from _lib import rankings_path, now_iso

CANONICAL_FIELDS = [
    "market_id", "business_name", "domain", "keyword", "search_volume",
    "cpc", "kd", "organic_position", "ranking_url", "maps_position",
    "serp_features", "competitor_names", "source", "observed_at",
    "exact_rank_verified", "notes",
]

# source is provenance ("where did this fact ultimately come from"), not just
# import mechanism -- a human typing up numbers they read off a real Semrush
# report is still "semrush" data, not a fabricated "manual_csv" guess. Widen
# beyond the three original mechanism-only values via --source-override.
KNOWN_SOURCES = {
    "semrush_csv", "manual_csv", "json_snapshot", "semrush", "google_local_capture",
    # V3.7 -- scripts/import_ranking_observation.py's clean single-observation
    # interface (schemas/ranking_evidence_observation.schema.json) uses these.
    "manual_maps_check", "manual_serp_check",
}

# Semrush "Organic Research > Positions" export column names (case-insensitive,
# with a few historical variants) -> canonical field.
SEMRUSH_COLUMN_MAP = {
    "keyword": "keyword",
    "position": "organic_position",
    "search volume": "search_volume",
    "volume": "search_volume",
    "cpc": "cpc",
    "keyword difficulty": "kd",
    "keyword difficulty index": "kd",
    "kd": "kd",
    "kd%": "kd",
    "url": "ranking_url",
    "domain": "domain",
}


def _to_int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_bool(v):
    if isinstance(v, bool):
        return v
    if v in (None, ""):
        return None
    return str(v).strip().lower() in ("true", "1", "yes")


def normalize_row(raw, market_id, source, observed_at, business_name_hint, domain_hint):
    row = {f: None for f in CANONICAL_FIELDS}
    row["market_id"] = market_id
    row["source"] = source
    row["observed_at"] = observed_at
    row["business_name"] = raw.get("business_name") or business_name_hint
    row["domain"] = raw.get("domain") or domain_hint
    # Unless a row explicitly says otherwise, an actual organic_position/
    # maps_position value is assumed to be an exact tracked rank; rows that
    # instead record an absence observation ("not present in the captured
    # result set") must set exact_rank_verified: false explicitly, never
    # imply precision that isn't there.
    row["exact_rank_verified"] = True

    if source == "semrush_csv":
        lower = {k.strip().lower(): v for k, v in raw.items()}
        for src_col, canon in SEMRUSH_COLUMN_MAP.items():
            if src_col in lower and lower[src_col] not in (None, ""):
                row[canon] = lower[src_col]
        row["organic_position"] = _to_int(row["organic_position"])
        row["search_volume"] = _to_int(row["search_volume"])
        row["cpc"] = _to_float(row["cpc"])
        row["kd"] = _to_float(row["kd"])
        row["maps_position"] = _to_int(raw.get("Maps Position") or raw.get("maps_position"))
    else:
        # manual_csv / json_snapshot / semrush (hand-transcribed) / google_local_capture:
        # assume canonical field names already, falling back gracefully if a
        # field is simply absent.
        for f in CANONICAL_FIELDS:
            if f in ("market_id", "source", "observed_at", "business_name", "domain", "exact_rank_verified"):
                continue
            if raw.get(f) not in (None, ""):
                row[f] = raw[f]
        row["organic_position"] = _to_int(row["organic_position"])
        row["maps_position"] = _to_int(row["maps_position"])
        row["search_volume"] = _to_int(row["search_volume"])
        row["cpc"] = _to_float(row["cpc"])
        row["kd"] = _to_float(row["kd"])
        if raw.get("exact_rank_verified") not in (None, ""):
            row["exact_rank_verified"] = _to_bool(raw["exact_rank_verified"])

    return row


def detect_source(path, first_row_keys):
    if path.suffix.lower() == ".json":
        return "json_snapshot"
    lower_keys = {k.strip().lower() for k in first_row_keys}
    if {"keyword", "position"}.issubset(lower_keys):
        return "semrush_csv"
    return "manual_csv"


def load_rows(path):
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text())
        if isinstance(data, dict):
            data = [data]
        return data, "json_snapshot"
    if path.suffix.lower() in (".xlsx", ".xls"):
        try:
            import openpyxl
        except ImportError:
            raise SystemExit(
                "XLSX import requires the optional `openpyxl` package, which isn't "
                "installed in this environment. Re-export from Semrush/Excel as CSV "
                "instead (File > Download > CSV) -- no other change needed."
            )
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb.active
        rows_iter = ws.iter_rows(values_only=True)
        headers = [str(h).strip() if h is not None else "" for h in next(rows_iter)]
        rows = [dict(zip(headers, r)) for r in rows_iter]
        return rows, "semrush_csv" if {"keyword", "position"}.issubset({h.lower() for h in headers}) else "manual_csv"
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        source = detect_source(path, rows[0].keys() if rows else [])
        return rows, source


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", required=True, help="market_id, e.g. hvac-nashville-tn")
    ap.add_argument("--file", required=True)
    ap.add_argument("--business-name", help="fallback business_name for rows that don't specify one")
    ap.add_argument("--domain", help="fallback domain for rows that don't specify one")
    ap.add_argument(
        "--source-override",
        help="Record a specific provenance label (e.g. 'semrush', 'google_local_capture') instead "
        "of the auto-detected import mechanism -- use this whenever you're hand-transcribing real "
        "numbers from an external report/search rather than importing a literal export file.",
    )
    ap.add_argument(
        "--observed-at",
        help="ISO date/timestamp for when this data was actually observed/generated (e.g. "
        "2026-09-01), if different from right now. Freshness checks key off this, not import time.",
    )
    args = ap.parse_args()

    path = Path(args.file)
    if not path.exists():
        raise SystemExit(f"File not found: {path}")

    raw_rows, detected_source = load_rows(path)
    if not raw_rows:
        print(f"No rows found in {path}", file=sys.stderr)
        return

    source = args.source_override or detected_source
    if source not in KNOWN_SOURCES:
        raise SystemExit(f"Unknown source '{source}'. Known values: {sorted(KNOWN_SOURCES)}")
    observed_at = args.observed_at or now_iso()
    normalized = [
        normalize_row(r, args.market, source, observed_at, args.business_name, args.domain)
        for r in raw_rows
    ]

    out_path = rankings_path(args.market)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    file_exists = out_path.exists() and out_path.stat().st_size > 0
    with open(out_path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CANONICAL_FIELDS)
        if not file_exists:
            writer.writeheader()
        writer.writerows(normalized)

    print(f"Imported {len(normalized)} row(s) from {path.name} (source: {source}, "
          f"observed_at: {observed_at}) into {out_path}")


if __name__ == "__main__":
    main()
