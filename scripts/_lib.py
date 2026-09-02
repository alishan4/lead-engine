"""Shared helpers for lead-engine scripts. No external deps beyond PyYAML."""
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config"

# V3.5: LEAD_ENGINE_DATA_DIR lets a caller (the controlled-validation sandbox
# in acquisition_worker.py) point every data path at a throwaway copy instead
# of the real production data/ tree, so a validation run cannot corrupt real
# prospect state. Unset in every normal/production invocation, in which case
# behavior is byte-for-byte what it always was.
DATA = Path(os.environ["LEAD_ENGINE_DATA_DIR"]) if os.environ.get("LEAD_ENGINE_DATA_DIR") else ROOT / "data"
PROSPECTS = DATA / "prospects"
MARKETS = DATA / "markets"
LEADS = DATA / "leads"
RANKINGS = DATA / "rankings"
OUTREACH = DATA / "outreach"


def load_yaml(name):
    with open(CONFIG / name) as f:
        return yaml.safe_load(f)


def read_jsonl(path):
    path = Path(path)
    if not path.exists():
        return []
    records = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def write_jsonl(path, records):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def append_jsonl(path, record):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def slugify(*parts):
    s = "-".join(str(p) for p in parts if p)
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def content_hash(*fields):
    h = hashlib.sha256()
    for f in fields:
        h.update(str(f).encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:16]


def market_slug(niche, city, state):
    return slugify(niche, city, state)


def load_market(niche, city, state):
    slug = market_slug(niche, city, state)
    path = MARKETS / slug / "market.json"
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return None


def lead_dir(prospect_id):
    d = LEADS / prospect_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_json(path):
    path = Path(path)
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)


def write_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.write("\n")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def days_since(iso_ts):
    """Return age in days of an ISO timestamp, or None if iso_ts is falsy/unparseable."""
    if not iso_ts:
        return None
    try:
        ts = datetime.fromisoformat(iso_ts)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds() / 86400.0


def rankings_path(market_id):
    return RANKINGS / f"{market_id}.csv"


# Every shared file a status update might need to reach. Deliberately
# excludes rejected.jsonl -- moving a record into/out of "rejected" is a
# file-moving operation (see qualify_leads.py, build_dossier.py.move_status),
# not a plain in-place field update, and mixing the two here would risk
# silently duplicating or losing records instead of just updating a field.
STATUS_SYNC_FILES = ("discovered.jsonl", "qualified.jsonl", "manual_review.jsonl", "needs_enrichment.jsonl")


def set_status_everywhere(prospect_id, new_status, extra_fields=None):
    """
    Update `status` (and optionally other fields) for prospect_id in every
    shared prospect file it currently appears in.

    This exists because three separate scripts (verify_contact.py,
    qa_email.py, check_freshness.py) each independently reimplemented
    "update status" but only wrote to discovered.jsonl -- silently
    desyncing it from qualified.jsonl the moment a lead progressed past
    initial qualification. Route every future status change through this
    one function instead of hand-rolling the read/mutate/write loop again.
    """
    for fname in STATUS_SYNC_FILES:
        path = PROSPECTS / fname
        records = read_jsonl(path)
        changed = False
        for r in records:
            if r.get("id") == prospect_id:
                r["status"] = new_status
                if extra_fields:
                    r.update(extra_fields)
                changed = True
        if changed:
            write_jsonl(path, records)


def load_franchise_blocklist():
    """Flat list of (category, pattern) from config/franchise_blocklist.yaml."""
    raw = load_yaml("franchise_blocklist.yaml")
    flat = []
    for category, patterns in (raw or {}).items():
        for p in patterns or []:
            flat.append((category, p.lower()))
    return flat


def match_franchise_blocklist(business_name, website, blocklist=None):
    """
    Cheap, free, deterministic substring match -- a hit means "worth a
    research pass to determine corporate_marketing_controlled," never an
    automatic reject. Returns (category, matched_pattern) or (None, None).
    """
    blocklist = blocklist if blocklist is not None else load_franchise_blocklist()
    haystack = f"{business_name or ''} {website or ''}".lower()
    for category, pattern in blocklist:
        if pattern in haystack:
            return category, pattern
    return None, None


def check_freshness(dossier, limits):
    """
    Phase 9 stale-finding protection: is this dossier's evidence still
    current enough to draft outreach from? Returns (is_fresh, reasons).
    A finding older than finding_freshness_days, or ranking data older than
    ranking_freshness_days (when a ranking claim is actually made), fails.
    """
    reasons = []
    finding_age = days_since(dossier.get("observed_at") or dossier.get("created_at"))
    if finding_age is not None and finding_age > limits["finding_freshness_days"]:
        reasons.append(
            f"strongest_finding observed {finding_age:.1f} days ago, "
            f"over the {limits['finding_freshness_days']}-day freshness limit"
        )

    makes_ranking_claim = dossier.get("maps_position") is not None or dossier.get("organic_position") is not None
    if makes_ranking_claim:
        ranking_age = days_since(dossier.get("ranking_observed_at"))
        if ranking_age is None:
            reasons.append(
                "dossier cites a maps_position/organic_position but has no "
                "ranking_observed_at -- ranking claims must have a dated source"
            )
        elif ranking_age > limits["ranking_freshness_days"]:
            reasons.append(
                f"ranking data is {ranking_age:.1f} days old, over the "
                f"{limits['ranking_freshness_days']}-day ranking freshness limit"
            )

    return (len(reasons) == 0, reasons)
