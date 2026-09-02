"""
V3.5 same-day catch-up eligibility tests -- pure functions, fake clock only,
no real time/filesystem dependency (see scripts/catchup.py's own docstring
for why). Covers spec checklist items: catch-up eligibility, catch-up
outside the time window, an old deterministic-only run not counting as a
completed V3.5 cycle, and duplicate-run prevention (ALREADY_COMPLETED_TODAY/
RUN_ALREADY_ACTIVE).
"""
import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import catchup  # noqa: E402

WINDOW = {"start": "12:00", "end": "14:00", "tz": "Asia/Karachi", "days": ["Tue", "Wed", "Thu", "Fri"]}


def wed(hour, minute):
    # 2026-09-02 is a real Wednesday (matches the actual production date
    # this feature shipped on) -- used throughout as a stable "eligible day".
    return datetime(2026, 9, 2, hour, minute)


class TestWindowHelpers(unittest.TestCase):
    def test_in_window_start_inclusive(self):
        self.assertTrue(catchup.in_catchup_window(wed(12, 0), WINDOW))

    def test_in_window_end_exclusive(self):
        self.assertFalse(catchup.in_catchup_window(wed(14, 0), WINDOW))

    def test_in_window_middle(self):
        self.assertTrue(catchup.in_catchup_window(wed(12, 47), WINDOW))

    def test_outside_window_before_noon(self):
        self.assertFalse(catchup.in_catchup_window(wed(11, 59), WINDOW))

    def test_ineligible_day(self):
        monday = datetime(2026, 9, 7, 12, 30)  # a Monday
        self.assertFalse(catchup.in_catchup_window(monday, WINDOW))

    def test_scheduled_firing_time_exact(self):
        self.assertTrue(catchup.is_scheduled_firing_time(wed(12, 0), WINDOW))

    def test_scheduled_firing_time_tolerance(self):
        self.assertTrue(catchup.is_scheduled_firing_time(wed(12, 4), WINDOW))

    def test_scheduled_firing_time_outside_tolerance(self):
        self.assertFalse(catchup.is_scheduled_firing_time(wed(12, 10), WINDOW))


class TestDetermineRun(unittest.TestCase):
    def test_normal_scheduled_firing(self):
        decision = catchup.determine_run(wed(12, 0), None, False, WINDOW)
        self.assertEqual(decision, catchup.NORMAL_SCHEDULE)

    def test_catchup_eligible_inside_window_no_completed_run(self):
        decision = catchup.determine_run(wed(12, 47), None, False, WINDOW)
        self.assertEqual(decision, catchup.SAME_DAY_CATCH_UP)

    def test_deterministic_only_run_does_not_block_catchup(self):
        """The exact real shape of today's pre-V3.5 run: a summary exists,
        but has no acquisition_run_completed key at all (V3.4 schema).
        Must NOT be read as a completed V3.5 cycle."""
        deterministic_only_summary = {
            "run_id": "run-20260902-070000-6e50d107", "dry_run": False,
            "infrastructure_failure": False, "qualified": 0, "high_priority": 0,
        }
        decision = catchup.determine_run(wed(12, 47), deterministic_only_summary, False, WINDOW)
        self.assertEqual(decision, catchup.SAME_DAY_CATCH_UP)

    def test_validation_run_summary_must_not_be_passed_as_todays_summary(self):
        """A DRY-RUN-prefixed validation summary is a different file by
        convention and must never be loaded as today's dated summary by the
        caller -- this test documents/asserts that even if one slipped
        through with acquisition_run_completed=True, catchup.py has no way
        to distinguish it from a real one, so the caller-side file-naming
        discipline (never load DRY-RUN-*.json as <date>.json) is load-bearing."""
        validation_summary_shape = {"acquisition_run_completed": True, "dry_run": True}
        decision = catchup.determine_run(wed(12, 47), validation_summary_shape, False, WINDOW)
        self.assertEqual(decision, catchup.ALREADY_COMPLETED_TODAY)  # caller must never pass this in for a real dry-run file

    def test_already_completed_today_blocks_duplicate_run(self):
        completed_summary = {"acquisition_run_completed": True}
        decision = catchup.determine_run(wed(13, 30), completed_summary, False, WINDOW)
        self.assertEqual(decision, catchup.ALREADY_COMPLETED_TODAY)

    def test_run_already_active_takes_priority_over_everything(self):
        completed_summary = {"acquisition_run_completed": True}
        decision = catchup.determine_run(wed(12, 47), completed_summary, True, WINDOW)
        self.assertEqual(decision, catchup.RUN_ALREADY_ACTIVE)

    def test_missed_window_after_2pm(self):
        decision = catchup.determine_run(wed(14, 30), None, False, WINDOW)
        self.assertEqual(decision, catchup.MISSED_ACQUISITION_WINDOW)

    def test_missed_window_does_not_fire_after_2pm_even_if_incomplete(self):
        # No accidental late-day auto-run -- section 31's explicit rule.
        decision = catchup.determine_run(wed(23, 59), None, False, WINDOW)
        self.assertEqual(decision, catchup.MISSED_ACQUISITION_WINDOW)

    def test_no_action_before_window_on_eligible_day(self):
        decision = catchup.determine_run(wed(6, 0), None, False, WINDOW)
        self.assertEqual(decision, catchup.NORMAL_SCHEDULE)

    def test_no_action_on_ineligible_day(self):
        saturday = datetime(2026, 9, 5, 13, 0)
        decision = catchup.determine_run(saturday, None, False, WINDOW)
        self.assertEqual(decision, catchup.NORMAL_SCHEDULE)


if __name__ == "__main__":
    unittest.main()
