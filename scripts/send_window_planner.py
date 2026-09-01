#!/usr/bin/env python3
"""
V3.3 send-window planner. Computes the next Tue-Fri local-business-hours
slot for a QA_PASS'd lead. Pure calculation, no network, no LLM. Reuses the
same best-effort state->timezone map as V2's export_gmail_drafts.py rather
than inventing a second one; a state missing from the map is a real,
reportable WINDOW_UNSCHEDULABLE outcome, never a guessed timezone.

Usage:
  python3 scripts/send_window_planner.py --id <slug>
"""
import argparse
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

from _lib import PROSPECTS, read_jsonl, lead_dir, load_json, write_json, load_yaml, set_status_everywhere, now_iso
from outreach_lib import record_event
from export_gmail_drafts import STATE_TIMEZONE

ENTRY_STATUS = "READY_TO_SEND"
ALLOWED_WEEKDAYS = {1, 2, 3, 4}  # Mon=0 ... Sun=6 -> Tue=1..Fri=4


def pick_window_bounds(niche, cfg):
    sw = cfg["send_window"]
    if niche in sw["professional_niches"]:
        start, end = sw["professional_window"]
    else:
        start, end = sw.get("home_service_window", sw["default_window"])
    return start, end


def _parse_hm(s):
    h, m = s.split(":")
    return dtime(int(h), int(m))


def next_send_datetime(now_local, start_str, end_str):
    """
    Pure: given an already-tz-aware `now_local`, return the next moment that
    is both an allowed weekday (Tue-Fri) and inside [start, end). If `now`
    itself qualifies, returns `now` (today, immediately schedulable).
    """
    start_t, end_t = _parse_hm(start_str), _parse_hm(end_str)
    candidate = now_local
    for _ in range(14):  # at most two weeks out; always terminates well before that
        if candidate.weekday() in ALLOWED_WEEKDAYS:
            day_start = candidate.replace(hour=start_t.hour, minute=start_t.minute, second=0, microsecond=0)
            day_end = candidate.replace(hour=end_t.hour, minute=end_t.minute, second=0, microsecond=0)
            if candidate <= day_start:
                return day_start
            if day_start <= candidate < day_end:
                return candidate
        candidate = (candidate + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return None


def plan_window(state, niche, cfg, now_utc=None):
    """Pure decision function: returns (plan_dict_or_None, reason_if_none)."""
    tz_name = STATE_TIMEZONE.get((state or "").strip().upper())
    if not tz_name:
        return None, f"no timezone mapping for state={state!r} -- cannot compute a real local send window"

    now_utc = now_utc or datetime.now(ZoneInfo("UTC"))
    now_local = now_utc.astimezone(ZoneInfo(tz_name))
    start_str, end_str = pick_window_bounds(niche, cfg)
    planned_local = next_send_datetime(now_local, start_str, end_str)
    if planned_local is None:
        return None, "no qualifying Tue-Fri window found in the search horizon"

    planned_utc = planned_local.astimezone(ZoneInfo("UTC"))
    planned_pkt = planned_local.astimezone(ZoneInfo(cfg["send_window"]["pkt_timezone"]))
    return {
        "timezone": tz_name,
        "local_datetime": planned_local.isoformat(),
        "local_weekday": planned_local.strftime("%A"),
        "utc_datetime": planned_utc.isoformat(),
        "pkt_datetime": planned_pkt.isoformat(),
        "window": f"{start_str}-{end_str}",
    }, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    args = ap.parse_args()

    p = next((r for r in read_jsonl(PROSPECTS / "discovered.jsonl") if r["id"] == args.id), None)
    if not p:
        raise SystemExit(f"Prospect {args.id} not found in discovered.jsonl")
    if p.get("status") != ENTRY_STATUS:
        raise SystemExit(f"{args.id}: status is {p.get('status')!r}, not {ENTRY_STATUS}.")

    cfg = load_yaml("outreach.yaml")
    plan, reason = plan_window(p.get("state"), p.get("niche"), cfg)
    if plan is None:
        record_event(args.id, "SEND_WINDOW_PLANNING_FAILED", p.get("status"), p.get("status"), reason)
        print(f"{args.id}: could not plan a send window -- {reason}")
        return

    plan["generated_at"] = now_iso()
    write_json(lead_dir(args.id) / "send_window.json", plan)
    set_status_everywhere(args.id, "SEND_WINDOW_PLANNED")
    record_event(args.id, "SEND_WINDOW_PLANNED", p.get("status"), "SEND_WINDOW_PLANNED",
                 f"next window: {plan['local_datetime']} {plan['timezone']}")
    print(f"{args.id}: SEND_WINDOW_PLANNED -- {plan['local_weekday']} {plan['local_datetime']} ({plan['timezone']})")


if __name__ == "__main__":
    main()
