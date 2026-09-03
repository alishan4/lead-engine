"""
V3.6 -- shared READY_TO_SEND handoff bridge. Pure functions for building,
merging, and updating one handoff row (schemas/handoff_row.schema.json).
No file I/O and no backend calls live here -- scripts/sync_handoff.py and
scripts/import_outreach_results.py own that; this module is the seam every
test in tests/test_v3_6_handoff.py exercises directly.

Ownership boundary encoded here, not just documented: COLUMNS lists every
field; EXTERNAL_OWNED_FIELDS are the subset Lead Engine's own sync
(merge_row) NEVER overwrites once set -- only apply_event (driven by a real
imported Gmail-side event) may change them. This is what makes
READY_TO_SEND != GMAIL_SENT and NO_BOUNCE_DETECTED != DELIVERED structural
in the shared queue, not just a documentation promise.
"""
import json

from _lib import check_freshness, now_iso
from outreach_lib import normalize_domain, is_suppressed, account_lock_check

COLUMNS = (
    "lead_id", "business_id", "business_name", "domain", "website", "city", "state", "country",
    "niche", "sub_niche",
    "fit_confirmed", "fit_potential", "gap_confirmed", "gap_potential", "qualification_status",
    "why_this_company", "why_this_problem", "why_now", "why_likely_buyer",
    "primary_wedge", "wedge_confidence", "asset_type", "asset_summary",
    "contact_name", "contact_role", "contact_email", "identity_status", "identity_confidence", "preferred_channel",
    "subject", "body", "qa_status", "evidence_freshness",
    "prospect_timezone", "planned_local_date", "planned_local_window", "planned_pkt_time",
    "lead_engine_state", "gmail_state", "delivery_state", "reply_state", "follow_up_state",
    "gmail_message_id", "gmail_thread_id",
    "last_action", "last_action_at", "human_review", "suppression_reason",
    "created_at", "updated_at",
)

# External-owned: only scripts/import_outreach_results.py's apply_event()
# may change these. merge_row() (Lead Engine's own re-sync path) always
# preserves whatever is already there.
EXTERNAL_OWNED_FIELDS = frozenset({
    "gmail_state", "delivery_state", "reply_state", "follow_up_state",
    "gmail_message_id", "gmail_thread_id", "suppression_reason",
})

EMAIL_CHANNEL_TYPES = ("NAMED_EMAIL", "COMPANY_EMAIL")
FORM_CHANNEL_TYPE = "CONTACT_FORM"

# V3.6.1 -- the canonical RESULTS-tab column order, taken directly from
# schemas/outreach_result_event.schema.json's own property order (never a
# second, conflicting schema invented here). This is what
# GoogleSheetsBackend.import_results() writes when it finds a completely
# empty RESULTS tab, and the reference order for anyone setting the tab up
# by hand.
RESULT_EVENT_COLUMNS = (
    "lead_id", "event_type", "event_at", "gmail_message_id", "gmail_thread_id",
    "reason", "note", "source",
)

# event_type -> which field it updates. SUPPRESSED and HUMAN_HANDOFF target
# suppression_reason/human_review instead of one of the four lifecycle
# state columns -- everything else updates exactly one of
# gmail_state/delivery_state/reply_state/follow_up_state. Deliberately
# explicit (no wildcard/inference) so adding a new event type is a one-line,
# reviewable change in two places (here and config/handoff.yaml).
EVENT_STATE_FIELD = {
    "GMAIL_RECONCILED": "gmail_state",
    "PRIOR_SENT_FOUND": "gmail_state",
    "DUPLICATE_FOUND": "gmail_state",
    "SEND_WINDOW_CONFIRMED": "gmail_state",
    "SEND_ATTEMPTED": "gmail_state",
    "GMAIL_SENT": "gmail_state",
    "SEND_FAILED": "gmail_state",
    "CLOSED": "gmail_state",
    "PRIOR_BOUNCE_FOUND": "delivery_state",
    "NO_BOUNCE_DETECTED": "delivery_state",
    "DELIVERY_FAILED": "delivery_state",
    "PRIOR_REPLY_FOUND": "reply_state",
    "REPLIED": "reply_state",
    "POSITIVE_REPLY": "reply_state",
    "NEGATIVE_REPLY": "reply_state",
    "FOLLOW_UP_DUE": "follow_up_state",
    "SUPPRESSED": "suppression_reason",
    "HUMAN_HANDOFF": "human_review",
}

# Which of the four lifecycle *timestamp groups* an event's newer-wins
# ordering is checked against. suppression_reason/human_review are treated
# as their own group (set-once-ish, but still ordered so a stale re-import
# can't un-suppress a lead).
STATE_GROUPS = ("gmail_state", "delivery_state", "reply_state", "follow_up_state", "suppression_reason", "human_review")


def is_eligible_for_export(prospect, contact, window, draft, dossier, limits_cfg):
    """
    Pure: does this lead meet every V3.4 READY_TO_SEND requirement PLUS
    V3.6's own re-checks (freshness re-verified at export time, not just at
    QA time; not currently suppressed/account-locked -- a lead can be
    suppressed by an imported event AFTER it first reached READY_TO_SEND).
    Returns (eligible: bool, reason).
    """
    if prospect.get("status") not in ("SEND_WINDOW_PLANNED", "READY_TO_SEND"):
        return False, f"status is {prospect.get('status')!r}, not SEND_WINDOW_PLANNED/READY_TO_SEND"
    if not (contact and window and draft):
        return False, "missing contact_record.json / send_window.json / email_draft.json"
    if contact.get("overall_status") not in ("CONTACT_VERIFIED", "CONTACT_FORM_READY"):
        return False, f"contact overall_status is {contact.get('overall_status')!r}, not verified/form-ready"
    channel_type = (contact.get("channel") or {}).get("type")
    if channel_type not in EMAIL_CHANNEL_TYPES + (FORM_CHANNEL_TYPE,):
        return False, f"no usable contact channel (channel.type={channel_type!r})"
    if dossier:
        is_fresh, reasons = check_freshness(dossier, limits_cfg)
        if not is_fresh:
            return False, f"evidence no longer fresh at export time: {'; '.join(reasons)}"
    suppressed, sup = is_suppressed(email=contact.get("identity", {}).get("email"),
                                     domain=normalize_domain(prospect.get("website")),
                                     business_id=prospect.get("id"))
    if suppressed:
        return False, f"suppressed ({sup.get('reason')}) -- never export a suppressed lead into an active queue"
    allowed, lock_reason = account_lock_check(prospect.get("id"), domain=prospect.get("website"))
    if not allowed:
        return False, f"account-locked: {lock_reason}"
    return True, "eligible"


def queue_for_channel(channel_type):
    return "CONTACT_FORM_READY" if channel_type == FORM_CHANNEL_TYPE else "EMAIL_READY"


def _asset_summary(asset):
    if not asset:
        return None
    title = asset.get("title") or ""
    noticed = (asset.get("sections") or {}).get("what_i_noticed") or ""
    summary = f"{title}: {noticed}" if title else noticed
    return summary[:400] or None


def build_lead_engine_fields(prospect, ready_row, contact, asset, wedge, dossier, limits_cfg):
    """
    Pure: builds every Lead-Engine-owned field (i.e. everything in COLUMNS
    except EXTERNAL_OWNED_FIELDS, last_action*, created_at/updated_at,
    human_review). Safe to recompute fresh on every sync -- Lead Engine is
    the authoritative source for all of it.
    """
    identity = contact.get("identity", {})
    channel = contact.get("channel", {})
    window = ready_row.get("send_window", {})
    fit_gap = ready_row.get("fit_gap_snapshot", {}) or {}
    fit, gap = fit_gap.get("fit") or {}, fit_gap.get("gap") or {}

    is_fresh, _ = check_freshness(dossier, limits_cfg) if dossier else (True, [])
    domain = normalize_domain(prospect.get("website"))

    why_this_company = (wedge or {}).get("why_this_company") or prospect.get("why_this_company")
    why_this_problem = (wedge or {}).get("why_this_problem") or prospect.get("why_this_problem")
    why_now = (wedge or {}).get("why_now") if wedge and wedge.get("why_now") is not None else prospect.get("why_now")
    why_likely_buyer = (wedge or {}).get("why_likely_buyer") or prospect.get("why_likely_buyer")

    return {
        "lead_id": prospect["id"],
        "business_id": domain or prospect["id"],
        "business_name": prospect.get("business_name"),
        "domain": domain,
        "website": prospect.get("website"),
        "city": prospect.get("city"), "state": prospect.get("state"), "country": prospect.get("country"),
        "niche": prospect.get("niche"),
        "sub_niche": None,  # no sub-niche taxonomy exists yet -- never guessed
        "fit_confirmed": fit.get("confirmed"), "fit_potential": fit.get("potential"),
        "gap_confirmed": gap.get("confirmed"), "gap_potential": gap.get("potential"),
        "qualification_status": "HIGH_PRIORITY" if prospect.get("status") == "HIGH_PRIORITY"
                                 else prospect.get("qualification_tier") or "QUALIFIED",
        "why_this_company": why_this_company, "why_this_problem": why_this_problem,
        "why_now": why_now, "why_likely_buyer": why_likely_buyer,
        "primary_wedge": (wedge or {}).get("opportunity_type"),
        "wedge_confidence": (wedge or {}).get("confidence"),
        "asset_type": (asset or {}).get("asset_type"),
        "asset_summary": _asset_summary(asset),
        "contact_name": identity.get("person_name"),
        "contact_role": identity.get("role"),
        "contact_email": identity.get("email") if channel.get("type") in EMAIL_CHANNEL_TYPES else None,
        "identity_status": identity.get("status"),
        "identity_confidence": identity.get("confidence"),
        "preferred_channel": channel.get("type"),
        "subject": ready_row.get("email", {}).get("subject"),
        "body": ready_row.get("email", {}).get("body"),
        "qa_status": ready_row.get("qa_status"),
        "evidence_freshness": "FRESH" if is_fresh else "STALE",
        "prospect_timezone": window.get("timezone"),
        "planned_local_date": (window.get("local_datetime") or "").split("T")[0] or None,
        "planned_local_window": window.get("window"),
        "planned_pkt_time": window.get("pkt_datetime"),
        "lead_engine_state": prospect.get("status"),
    }


def merge_row(existing_row, lead_engine_fields, now=None):
    """
    Pure: the idempotent-export merge. `existing_row` is the previously
    persisted row for this lead_id (or None on first export).
    Lead-Engine-owned fields are always replaced with the fresh values;
    EXTERNAL_OWNED_FIELDS, created_at, last_action, last_action_at, and
    human_review are always preserved from `existing_row` when present --
    this is the single mechanism that guarantees a Lead Engine re-sync can
    never clobber a Gmail-side result written by import_outreach_results.py.
    """
    now = now or now_iso()
    if existing_row is None:
        row = dict(lead_engine_fields)
        for f in EXTERNAL_OWNED_FIELDS:
            row.setdefault(f, None)
        row["human_review"] = False
        row["last_action"] = "EXPORTED"
        row["last_action_at"] = now
        row["created_at"] = now
        row["updated_at"] = now
        return row

    row = dict(existing_row)
    row.update(lead_engine_fields)
    for f in EXTERNAL_OWNED_FIELDS:
        row[f] = existing_row.get(f)
    row["human_review"] = existing_row.get("human_review", False)
    row["last_action"] = existing_row.get("last_action") or "EXPORTED"
    row["last_action_at"] = existing_row.get("last_action_at") or now
    row["created_at"] = existing_row.get("created_at") or now
    row["updated_at"] = now
    return row


def validate_event(event):
    """
    Pure: minimum-shape validation for one external result event, run
    BEFORE dedup/apply so scripts/import_outreach_results.py can isolate a
    malformed/invalid row as its own failure without touching any other
    event in the batch (V3.6.1 -- "one malformed result event must never
    abort or block other valid events").

    Deliberately permissive about EXTRA/unknown fields on the event dict --
    schema evolution (a future field ChatGPT starts sending) must not turn
    into a rejection. Only checks what apply_event()/event_dedup_key()
    actually require: a real lead_id, and a real, recognized event_type.
    Returns (True, None) or (False, reason).
    """
    if not isinstance(event, dict):
        return False, "row is not a valid event object (malformed/truncated Google Sheets row)"
    lead_id = event.get("lead_id")
    if not lead_id or not isinstance(lead_id, str):
        return False, "missing lead_id"
    event_type = event.get("event_type")
    if not event_type or not isinstance(event_type, str):
        return False, "missing event_type"
    if event_type not in EVENT_STATE_FIELD:
        return False, f"unrecognized event_type {event_type!r}"
    return True, None


def event_dedup_key(event):
    """
    Stable identity for one external event. When a Gmail message/thread id
    is present, dedup on (lead_id, event_type, message_id, thread_id) alone
    -- ignoring event_at -- so the exact same Gmail-side event re-imported
    with slightly different formatting is still recognized as identical
    ("never duplicate the same Gmail message/thread event"). Otherwise falls
    back to including event_at.

    Uses .get() throughout (never bracket access) so this can never itself
    raise on a malformed event -- callers should still run validate_event()
    first; this is defense in depth, not a substitute for it. Only called
    on events that already passed validate_event() in
    scripts/import_outreach_results.py, so lead_id/event_type are always
    real strings in practice.
    """
    mid, tid = event.get("gmail_message_id"), event.get("gmail_thread_id")
    lead_id, event_type = event.get("lead_id"), event.get("event_type")
    if mid or tid:
        return (lead_id, event_type, mid, tid)
    return (lead_id, event_type, event.get("event_at"))


# ---------------------------------------------------------------------------
# V3.8.1 -- CANDIDATES tab. An ADDITIONAL handoff surface, alongside (never
# replacing) EMAIL_READY/CONTACT_FORM_READY/RESULTS above. Every field here
# is Lead-Engine-owned and deterministic (schemas/candidate_record.schema.json)
# -- there is currently no ChatGPT-side write-back concept for a candidate
# row (unlike EXTERNAL_OWNED_FIELDS above), so a re-sync safely overwrites
# every field except created_at, which is always preserved from the first
# sync -- this is what keeps a rediscovered/re-synced candidate from ever
# duplicating its row (idempotent upsert-by-lead_id, same mechanism
# GoogleSheetsBackend._upsert_tab / LocalFileBackend already use for the
# two queues above).
# ---------------------------------------------------------------------------
CANDIDATE_COLUMNS = (
    "lead_id", "business_name", "domain", "website", "city", "state", "country",
    "niche", "phone", "profile_url", "discovery_source", "discovered_at",
    "verification_status", "basic_business_facts",
    "created_at", "updated_at",
)


def candidate_row_from_record(candidate_record, now=None):
    """Pure: schemas/candidate_record.schema.json's dict -> one flat
    CANDIDATE_COLUMNS row ready for a Sheet/local-file upsert.
    `basic_business_facts` (a nested dict) is JSON-stringified since every
    handoff row (Sheets or CSV) is a flat key/value structure -- the
    original structured dict is never lost, just serialized for transport,
    exactly like `env`/other nested blobs elsewhere in this pipeline."""
    now = now or now_iso()
    row = {c: candidate_record.get(c) for c in CANDIDATE_COLUMNS if c not in ("created_at", "updated_at", "basic_business_facts")}
    row["basic_business_facts"] = json.dumps(candidate_record.get("basic_business_facts") or {}, ensure_ascii=False)
    row["created_at"] = now
    row["updated_at"] = now
    return row


def merge_candidate_row(existing_row, fresh_row, now=None):
    """
    Pure: the idempotent-export merge for one CANDIDATES row. Every field
    is Lead-Engine-owned and safe to recompute fresh on every sync (no
    EXTERNAL_OWNED_FIELDS concept exists for a candidate today) -- the only
    thing preserved from `existing_row` is `created_at`, so re-syncing the
    same lead_id (a rediscovery, or simply a later run re-reading the same
    CANDIDATE_VERIFIED prospect) always updates the SAME row in place,
    never appends a duplicate.
    """
    now = now or now_iso()
    if existing_row is None:
        row = dict(fresh_row)
        row["created_at"] = now
        row["updated_at"] = now
        return row
    row = dict(fresh_row)
    row["created_at"] = existing_row.get("created_at") or now
    row["updated_at"] = now
    return row


def apply_event(row, event, now=None):
    """
    Pure: applies one already-deduped external event to `row`. Enforces
    newer-wins ordering per STATE_GROUPS entry via `_state_timestamps`
    (stored on the row, not part of COLUMNS/the public schema -- an
    internal bookkeeping field) so a stale/out-of-order re-import can never
    regress a field a newer event already set. Returns (new_row, applied,
    reason).
    """
    now = now or now_iso()
    event_type = event.get("event_type")
    field = EVENT_STATE_FIELD.get(event_type)
    if field is None:
        return row, False, f"unknown event_type {event_type!r} -- not in EVENT_STATE_FIELD, ignored"

    row = dict(row)
    timestamps = dict(row.get("_state_timestamps") or {})
    incoming_at = event.get("event_at") or now
    current_at = timestamps.get(field)
    if current_at and incoming_at < current_at:
        return row, False, f"stale event ({incoming_at} older than already-applied {current_at} for {field}) -- ignored"

    if field == "suppression_reason":
        row["suppression_reason"] = event.get("reason") or event_type
    elif field == "human_review":
        row["human_review"] = True
    else:
        row[field] = event_type
        if event.get("gmail_message_id"):
            row["gmail_message_id"] = event["gmail_message_id"]
        if event.get("gmail_thread_id"):
            row["gmail_thread_id"] = event["gmail_thread_id"]

    timestamps[field] = incoming_at
    row["_state_timestamps"] = timestamps
    row["last_action"] = event_type
    row["last_action_at"] = incoming_at
    row["updated_at"] = now
    return row, True, "applied"
