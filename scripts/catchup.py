#!/usr/bin/env python3
"""
V3.5 -- same-day catch-up eligibility. Pure functions, fully unit-testable
with a fake clock: nothing here reads the real clock or filesystem itself
except the thin CLI at the bottom, which exists only for manual/human
sanity-checking from a shell.

The permanent production schedule (systemd/lead-engine-daily.timer) is
unchanged by this module -- it does not add a second timer or any polling
loop. This module only answers "if something invokes the acquisition
worker right now, what should happen?" -- the invocation itself still has
to come from somewhere (the timer's normal 12:00 firing, or a manual/
follow-up run inside the window). See docs/AUTOMATION.md for the explicit
limitation this implies.
"""
from datetime import datetime

NORMAL_SCHEDULE = "NORMAL_SCHEDULE"
SAME_DAY_CATCH_UP = "SAME_DAY_CATCH_UP"
MISSED_ACQUISITION_WINDOW = "MISSED_ACQUISITION_WINDOW"
ALREADY_COMPLETED_TODAY = "ALREADY_COMPLETED_TODAY"
RUN_ALREADY_ACTIVE = "RUN_ALREADY_ACTIVE"

DAY_ABBR = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _hhmm(dt):
    return dt.hour * 60 + dt.minute


def _parse_hhmm(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def in_catchup_window(now_local, window_cfg):
    """Pure: is now_local (a naive datetime already in the target tz) inside
    the configured catch-up window on an eligible day?"""
    day = DAY_ABBR[now_local.weekday()]
    if day not in window_cfg["days"]:
        return False
    start, end = _parse_hhmm(window_cfg["start"]), _parse_hhmm(window_cfg["end"])
    return start <= _hhmm(now_local) < end


def is_scheduled_firing_time(now_local, window_cfg, tolerance_minutes=5):
    """True only right at the timer's own 12:00 firing (within a small
    tolerance for systemd's AccuracySec) -- used to distinguish 'this is the
    normal scheduled run' from 'this is a same-day recovery invocation'."""
    day = DAY_ABBR[now_local.weekday()]
    if day not in window_cfg["days"]:
        return False
    start = _parse_hhmm(window_cfg["start"])
    return abs(_hhmm(now_local) - start) <= tolerance_minutes


def determine_run(now_local, todays_summary, acquisition_lock_active, window_cfg):
    """
    The single decision function. Returns one of the module-level constants.

    now_local: naive datetime already converted to the schedule's timezone
        (Asia/Karachi) -- callers own the tz conversion so this function
        stays trivially testable with any fake time.
    todays_summary: the parsed dict from data/runtime/daily_runs/<today>.json
        for *today's Karachi-local date*, or None if it doesn't exist yet.
        A deterministic-only run (no `acquisition_run_completed` key at all,
        e.g. today's real pre-V3.5 2026-09-02 run) is correctly NOT treated
        as a completed V3.5 cycle. A DRY-RUN-prefixed validation summary must
        never be passed in here by the caller -- it is a different file, by
        the same convention run_daily.py already uses for --dry-run.
    acquisition_lock_active: whether the acquisition-worker lock is
        currently held by another process.
    """
    if acquisition_lock_active:
        return RUN_ALREADY_ACTIVE

    if todays_summary and todays_summary.get("acquisition_run_completed") is True:
        return ALREADY_COMPLETED_TODAY

    if is_scheduled_firing_time(now_local, window_cfg):
        return NORMAL_SCHEDULE

    if in_catchup_window(now_local, window_cfg):
        return SAME_DAY_CATCH_UP

    day = DAY_ABBR[now_local.weekday()]
    if day in window_cfg["days"] and _hhmm(now_local) >= _parse_hhmm(window_cfg["end"]):
        return MISSED_ACQUISITION_WINDOW

    return NORMAL_SCHEDULE  # outside any eligible day, or before the window -- nothing to do


def main():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _lib import load_yaml, load_json, DATA
    from zoneinfo import ZoneInfo

    cfg = load_yaml("acquisition.yaml")["catchup_window"]
    now_local = datetime.now(ZoneInfo(cfg["tz"])).replace(tzinfo=None)
    today_key = now_local.date().isoformat()
    summary_path = DATA / "runtime" / "daily_runs" / f"{today_key}.json"
    summary = load_json(summary_path)
    lock_path = DATA / "runtime" / "acquisition.lock"
    lock_active = False
    if lock_path.exists():
        try:
            import fcntl
            with open(lock_path) as f:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(f, fcntl.LOCK_UN)
        except (OSError, BlockingIOError):
            lock_active = True

    decision = determine_run(now_local, summary, lock_active, cfg)
    print(f"{decision} (now={now_local.isoformat()} {cfg['tz']}, today_summary_exists={bool(summary)})")


if __name__ == "__main__":
    main()
