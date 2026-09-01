"""
V3.3 shared outreach-execution infrastructure: suppression registry,
account-level outreach lock, append-only audit log, idempotency helpers.

Nothing here ever sends anything. This module has zero knowledge of Gmail
or any transport -- it only tracks state that governs whether a send is
ALLOWED, and records that a state transition happened.
"""
from _lib import OUTREACH, DATA, read_jsonl, write_jsonl, append_jsonl, now_iso, days_since

SUPPRESSION_PATH = OUTREACH / "suppression_registry.jsonl"
ACCOUNT_PATH = OUTREACH / "account_registry.jsonl"
AUDIT_LOG_PATH = OUTREACH / "audit_log.jsonl"
SEND_LOG_PATH = OUTREACH / "send_log.jsonl"          # dry-run records only in V3.3
DELIVERABILITY_PATH = OUTREACH / "deliverability_events.jsonl"

VALID_SUPPRESSION_REASONS = {
    "BOUNCE", "INVALID_EMAIL", "UNSUBSCRIBE", "DO_NOT_CONTACT",
    "NEGATIVE_REPLY", "ABUSE_COMPLAINT", "DUPLICATE_ACCOUNT", "MANUAL_SUPPRESSION",
}


def _norm(s):
    return (s or "").strip().lower()


def normalize_domain(email_or_domain_or_url):
    """Best-effort domain normalization: strips protocol/path/mailto/local-part."""
    s = _norm(email_or_domain_or_url)
    if not s:
        return None
    s = s.replace("mailto:", "")
    if "@" in s:
        s = s.split("@", 1)[1]
    if "://" in s:
        s = s.split("://", 1)[1]
    s = s.split("/", 1)[0]
    s = s[4:] if s.startswith("www.") else s
    return s or None


# ----------------------------------------------------------------------------
# Suppression registry -- global, persistent, append-only. Checked before
# EVERY draft, EVERY send, EVERY follow-up. A suppression is permanent unless
# a human explicitly removes it (no automated un-suppression in V3.3).
# ----------------------------------------------------------------------------

def load_suppression():
    return read_jsonl(SUPPRESSION_PATH)


def is_suppressed(email=None, domain=None, business_id=None, registry=None):
    """Returns (True, matching_record) if any identifier matches an active
    suppression entry, else (False, None). Matching is exact on whichever
    identifiers are provided -- no fuzzy matching (a false negative here is
    safer to catch downstream than a false positive silently blocking a
    legitimate new lead)."""
    registry = registry if registry is not None else load_suppression()
    email_n, domain_n = _norm(email), _norm(domain) or normalize_domain(email)
    for r in registry:
        if email_n and _norm(r.get("email")) == email_n:
            return True, r
        if domain_n and normalize_domain(r.get("domain") or r.get("email")) == domain_n:
            return True, r
        if business_id and r.get("business_id") == business_id:
            return True, r
    return False, None


def add_suppression(reason, email=None, domain=None, business_id=None, source="", note=""):
    if reason not in VALID_SUPPRESSION_REASONS:
        raise ValueError(f"Unknown suppression reason: {reason!r}")
    already, existing = is_suppressed(email=email, domain=domain, business_id=business_id)
    if already:
        return existing  # idempotent -- never double-suppress the same identity
    record = {
        "email": email, "domain": domain or normalize_domain(email),
        "business_id": business_id, "reason": reason, "source": source,
        "note": note, "suppressed_at": now_iso(),
    }
    append_jsonl(SUPPRESSION_PATH, record)
    return record


# ----------------------------------------------------------------------------
# Account-level outreach lock -- one active outreach thread per business at
# a time, keyed by business_id (the prospect id is stable and 1:1 with one
# real business here, which is a safe key; domain is tracked as a secondary
# check for the "different prospect record, same real company" case).
# ----------------------------------------------------------------------------
ACCOUNT_STATES = ("NEW", "ACTIVE_OUTREACH", "AWAITING_REPLY", "REPLIED", "CLOSED", "SUPPRESSED")


def load_account_registry():
    return read_jsonl(ACCOUNT_PATH)


def get_account(business_id, domain=None):
    for r in load_account_registry():
        if r.get("business_id") == business_id:
            return r
        if domain and r.get("domain") and normalize_domain(r["domain"]) == normalize_domain(domain):
            return r
    return None


def _write_account(record):
    records = load_account_registry()
    for i, r in enumerate(records):
        if r.get("business_id") == record["business_id"]:
            records[i] = record
            write_jsonl(ACCOUNT_PATH, records)
            return
    append_jsonl(ACCOUNT_PATH, record)


def account_lock_check(business_id, domain=None):
    """
    Returns (allowed: bool, reason: str). A NEW business, or one with no
    registry entry at all, is always allowed a first touch. An account
    already ACTIVE_OUTREACH / AWAITING_REPLY / REPLIED / CLOSED / SUPPRESSED
    blocks a second first-touch draft -- this is the explicit guard against
    "never send a duplicate simply to test V3.3," generalized to all
    outreach, not just test runs.
    """
    acct = get_account(business_id, domain)
    if acct is None:
        return True, "no prior outreach on record for this business"
    state = acct.get("state")
    if state in ("NEW",):
        return True, "account registered but no touch sent yet"
    if state == "CLOSED" and acct.get("closed_reason") == "RECYCLE_ELIGIBLE":
        return True, "prior sequence closed and explicitly marked recycle-eligible"
    return False, f"account already has state={state} (first touch at {acct.get('first_touch_at')}) -- outreach lock blocks a duplicate thread"


def register_touch(business_id, domain, event, message_id=None, extra=None):
    """
    event in {"FIRST_TOUCH_DRY_RUN", "FIRST_TOUCH_SENT", "FOLLOW_UP", "REPLY_RECEIVED",
    "CLOSED", "SUPPRESSED"}. This is the single place account state advances.
    """
    acct = get_account(business_id, domain) or {
        "business_id": business_id, "domain": domain, "state": "NEW",
        "first_touch_at": None, "last_touch_at": None, "touch_count": 0, "message_ids": [],
    }
    acct["last_touch_at"] = now_iso()
    if event in ("FIRST_TOUCH_DRY_RUN", "FIRST_TOUCH_SENT"):
        acct["first_touch_at"] = acct["first_touch_at"] or acct["last_touch_at"]
        acct["state"] = "ACTIVE_OUTREACH" if event == "FIRST_TOUCH_SENT" else acct["state"]
        if event == "FIRST_TOUCH_DRY_RUN":
            acct["state"] = "NEW"  # a dry run never actually opens a real thread
        acct["touch_count"] = acct.get("touch_count", 0) + 1
    elif event == "FOLLOW_UP":
        acct["state"] = "AWAITING_REPLY"
        acct["touch_count"] = acct.get("touch_count", 0) + 1
    elif event == "REPLY_RECEIVED":
        acct["state"] = "REPLIED"
    elif event == "CLOSED":
        acct["state"] = "CLOSED"
        acct["closed_reason"] = (extra or {}).get("closed_reason")
    elif event == "SUPPRESSED":
        acct["state"] = "SUPPRESSED"
    if message_id:
        acct.setdefault("message_ids", []).append(message_id)
    if extra:
        acct.update({k: v for k, v in extra.items() if k != "closed_reason"} if event == "CLOSED" else extra)
    _write_account(acct)
    return acct


# ----------------------------------------------------------------------------
# Audit log -- append-only, one line per state transition. Never mutated,
# never truncated; this is the record a human reconstructs "what happened
# and why" from.
# ----------------------------------------------------------------------------

def record_event(prospect_id, event, from_status, to_status, reason, evidence_refs=None, dry_run=True, extra=None):
    entry = {
        "prospect_id": prospect_id, "event": event, "from_status": from_status,
        "to_status": to_status, "reason": reason, "evidence_refs": evidence_refs or [],
        "dry_run": dry_run, "recorded_at": now_iso(),
    }
    if extra:
        entry.update(extra)
    append_jsonl(AUDIT_LOG_PATH, entry)
    return entry


def read_audit_log(prospect_id=None):
    records = read_jsonl(AUDIT_LOG_PATH)
    if prospect_id:
        return [r for r in records if r.get("prospect_id") == prospect_id]
    return records


# ----------------------------------------------------------------------------
# Idempotency helper -- "have I already produced this exact artifact?"
# ----------------------------------------------------------------------------

def artifact_is_current(existing_obj, expected_hash, hash_field="content_hash"):
    return bool(existing_obj) and existing_obj.get(hash_field) == expected_hash
