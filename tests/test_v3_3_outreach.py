import json
import shutil
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import _lib
import outreach_lib
import contact_identity
import generate_outreach_email
import qa_outreach_email
import send_window_planner
import send_executor
import delivery_reconciliation
import follow_up
import reply_handling
from wedge_selection import commercial_mechanism_is_defensible


OUTREACH_CFG = _lib.load_yaml("outreach.yaml")
LIMITS = _lib.load_yaml("limits.yaml")


class IsolatedDataMixin:
    """Redirects every outreach data path to a throwaway temp dir so tests
    never touch the real suppression/account/audit registries or real leads."""

    def setUp(self):
        self.tmp = Path("/tmp") / f"v3_3_test_{id(self)}"
        self.tmp.mkdir(parents=True, exist_ok=True)
        self._orig = {
            "SUPPRESSION_PATH": outreach_lib.SUPPRESSION_PATH,
            "ACCOUNT_PATH": outreach_lib.ACCOUNT_PATH,
            "AUDIT_LOG_PATH": outreach_lib.AUDIT_LOG_PATH,
            "SEND_LOG_PATH": outreach_lib.SEND_LOG_PATH,
            "DELIVERABILITY_PATH": outreach_lib.DELIVERABILITY_PATH,
        }
        outreach_lib.SUPPRESSION_PATH = self.tmp / "suppression_registry.jsonl"
        outreach_lib.ACCOUNT_PATH = self.tmp / "account_registry.jsonl"
        outreach_lib.AUDIT_LOG_PATH = self.tmp / "audit_log.jsonl"
        outreach_lib.SEND_LOG_PATH = self.tmp / "send_log.jsonl"
        outreach_lib.DELIVERABILITY_PATH = self.tmp / "deliverability_events.jsonl"
        # send_executor and delivery_reconciliation imported the names directly
        send_executor.SEND_LOG_PATH = outreach_lib.SEND_LOG_PATH

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(outreach_lib, k, v)
        send_executor.SEND_LOG_PATH = outreach_lib.SEND_LOG_PATH
        shutil.rmtree(self.tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Suppression registry
# ---------------------------------------------------------------------------
class TestSuppression(IsolatedDataMixin, unittest.TestCase):
    def test_01_not_suppressed_by_default(self):
        self.assertEqual(outreach_lib.is_suppressed(email="a@b.com")[0], False)

    def test_02_add_and_check_by_email(self):
        outreach_lib.add_suppression("BOUNCE", email="a@b.com")
        self.assertTrue(outreach_lib.is_suppressed(email="a@b.com")[0])

    def test_03_add_and_check_by_domain(self):
        outreach_lib.add_suppression("UNSUBSCRIBE", domain="b.com")
        self.assertTrue(outreach_lib.is_suppressed(email="someone@b.com")[0])

    def test_04_add_and_check_by_business_id(self):
        outreach_lib.add_suppression("DO_NOT_CONTACT", business_id="biz-1")
        self.assertTrue(outreach_lib.is_suppressed(business_id="biz-1")[0])

    def test_05_idempotent_double_suppress(self):
        outreach_lib.add_suppression("BOUNCE", email="dup@b.com")
        outreach_lib.add_suppression("BOUNCE", email="dup@b.com")
        self.assertEqual(len(outreach_lib.load_suppression()), 1)

    def test_06_invalid_reason_rejected(self):
        with self.assertRaises(ValueError):
            outreach_lib.add_suppression("NOT_A_REAL_REASON", email="x@y.com")

    def test_07_unrelated_email_not_suppressed(self):
        outreach_lib.add_suppression("BOUNCE", email="a@b.com")
        self.assertFalse(outreach_lib.is_suppressed(email="c@d.com")[0])


# ---------------------------------------------------------------------------
# Account lock
# ---------------------------------------------------------------------------
class TestAccountLock(IsolatedDataMixin, unittest.TestCase):
    def test_08_new_business_allowed(self):
        allowed, _ = outreach_lib.account_lock_check("biz-new")
        self.assertTrue(allowed)

    def test_09_active_outreach_blocks_duplicate(self):
        outreach_lib.register_touch("biz-2", "biz2.com", "FIRST_TOUCH_SENT")
        allowed, reason = outreach_lib.account_lock_check("biz-2")
        self.assertFalse(allowed)
        self.assertIn("ACTIVE_OUTREACH", reason)

    def test_10_dry_run_touch_does_not_lock(self):
        outreach_lib.register_touch("biz-3", "biz3.com", "FIRST_TOUCH_DRY_RUN")
        allowed, _ = outreach_lib.account_lock_check("biz-3")
        self.assertTrue(allowed, "a dry-run touch must never open a real duplicate-blocking thread")

    def test_11_replied_blocks_new_touch(self):
        outreach_lib.register_touch("biz-4", "biz4.com", "FIRST_TOUCH_SENT")
        outreach_lib.register_touch("biz-4", "biz4.com", "REPLY_RECEIVED")
        allowed, _ = outreach_lib.account_lock_check("biz-4")
        self.assertFalse(allowed)

    def test_12_recycle_eligible_closed_account_allowed(self):
        outreach_lib.register_touch("biz-5", "biz5.com", "FIRST_TOUCH_SENT")
        outreach_lib.register_touch("biz-5", "biz5.com", "CLOSED", extra={"closed_reason": "RECYCLE_ELIGIBLE"})
        allowed, _ = outreach_lib.account_lock_check("biz-5")
        self.assertTrue(allowed)

    def test_13_closed_no_reply_blocks(self):
        outreach_lib.register_touch("biz-6", "biz6.com", "FIRST_TOUCH_SENT")
        outreach_lib.register_touch("biz-6", "biz6.com", "CLOSED", extra={"closed_reason": "SEQUENCE_EXHAUSTED_NO_REPLY"})
        allowed, _ = outreach_lib.account_lock_check("biz-6")
        self.assertFalse(allowed)

    def test_14_domain_normalization_matches_www(self):
        self.assertEqual(outreach_lib.normalize_domain("https://www.Example.com/contact"), "example.com")

    def test_15_domain_match_catches_same_company_different_id(self):
        outreach_lib.register_touch("biz-7", "https://www.sameco.com", "FIRST_TOUCH_SENT")
        allowed, _ = outreach_lib.account_lock_check("biz-7-duplicate-record", domain="sameco.com")
        self.assertFalse(allowed, "same real company under a different prospect id must still be caught")


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------
class TestAuditLog(IsolatedDataMixin, unittest.TestCase):
    def test_16_record_and_read(self):
        outreach_lib.record_event("biz-8", "TEST_EVENT", "A", "B", "because")
        entries = outreach_lib.read_audit_log("biz-8")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["to_status"], "B")

    def test_17_dry_run_flag_recorded(self):
        outreach_lib.record_event("biz-9", "X", "A", "B", "r", dry_run=True)
        self.assertTrue(outreach_lib.read_audit_log("biz-9")[0]["dry_run"])

    def test_18_append_only_never_overwrites(self):
        outreach_lib.record_event("biz-10", "E1", "A", "B", "r1")
        outreach_lib.record_event("biz-10", "E2", "B", "C", "r2")
        self.assertEqual(len(outreach_lib.read_audit_log("biz-10")), 2)


# ---------------------------------------------------------------------------
# Contact identity
# ---------------------------------------------------------------------------
class TestContactIdentity(unittest.TestCase):
    def test_19_entry_gate_requires_asset_staged(self):
        self.assertTrue(contact_identity.entry_allowed("ASSET_STAGED"))
        self.assertFalse(contact_identity.entry_allowed("QUALIFIED"))

    def test_20_never_infer_info_at_domain(self):
        research = {"person_name": None, "email": "info@example.com",
                    "sources": [{"source_type": "public_directory", "observed_at": "2026-09-01T00:00:00+00:00"}]}
        identity = contact_identity.resolve_identity(research, OUTREACH_CFG)
        self.assertEqual(identity["status"], "UNVERIFIED",
                          "public_directory alone is not an acceptable identity source type")

    def test_21_named_person_on_official_site_verified(self):
        research = {"person_name": "Jane Q. Example", "role": "managing_attorney",
                    "email": "jane@firm.com",
                    "sources": [{"source_type": "official_website_team_page", "url": "https://firm.com/about",
                                 "observed_at": "2026-09-01T00:00:00+00:00"}]}
        identity = contact_identity.resolve_identity(research, OUTREACH_CFG)
        self.assertEqual(identity["status"], "VERIFIED")
        self.assertGreaterEqual(identity["confidence"], OUTREACH_CFG["identity"]["min_identity_confidence"])

    def test_22_company_inbox_without_name(self):
        research = {"person_name": None, "email": "office@example-roofing.com",
                    "sources": [{"source_type": "official_website_team_page", "observed_at": "2026-09-01T00:00:00+00:00"}]}
        identity = contact_identity.resolve_identity(research, OUTREACH_CFG)
        self.assertEqual(identity["status"], "COMPANY_INBOX_ONLY")

    def test_23_form_only_fallback(self):
        research = {"person_name": None, "email": None, "sources": [], "has_contact_form": True}
        identity = contact_identity.resolve_identity(research, OUTREACH_CFG)
        self.assertEqual(identity["status"], "FORM_ONLY")

    def test_24_nothing_found_is_unverified_not_error(self):
        research = {"person_name": None, "email": None, "sources": [], "has_contact_form": False}
        identity = contact_identity.resolve_identity(research, OUTREACH_CFG)
        self.assertEqual(identity["status"], "UNVERIFIED")
        self.assertEqual(identity["confidence"], 0.0)

    def test_25_rejected_evidence_preserved_not_discarded(self):
        research = {"person_name": None, "email": None, "sources": [], "has_contact_form": False,
                    "rejected_evidence": [{"source_type": "aggregator", "note": "no primary source"}]}
        identity = contact_identity.resolve_identity(research, OUTREACH_CFG)
        self.assertEqual(len(identity["rejected_evidence"]), 1)

    def test_26_mailbox_defaults_unknown(self):
        mailbox = contact_identity.resolve_mailbox({})
        self.assertEqual(mailbox["status"], "UNKNOWN")

    def test_27_mailbox_never_probed_actively(self):
        mailbox = contact_identity.resolve_mailbox({"mailbox_hint": None})
        self.assertEqual(mailbox["status"], "UNKNOWN")

    def test_28_overall_status_verified_with_email(self):
        identity = {"status": "VERIFIED", "email": "a@b.com"}
        channel = {"type": "NAMED_EMAIL", "address_or_url": "a@b.com"}
        mailbox = {"status": "UNKNOWN"}
        self.assertEqual(contact_identity.overall_status(identity, channel, mailbox), "CONTACT_VERIFIED")

    def test_29_overall_status_form_only(self):
        identity = {"status": "FORM_ONLY"}
        channel = {"type": "CONTACT_FORM", "address_or_url": "https://x.com/contact"}
        mailbox = {"status": "UNKNOWN"}
        self.assertEqual(contact_identity.overall_status(identity, channel, mailbox), "CONTACT_FORM_READY")

    def test_30_invalid_mailbox_forces_reverify_even_if_identity_verified(self):
        identity = {"status": "VERIFIED", "email": "a@b.com"}
        channel = {"type": "NAMED_EMAIL", "address_or_url": "a@b.com"}
        mailbox = {"status": "INVALID"}
        self.assertEqual(contact_identity.overall_status(identity, channel, mailbox), "CONTACT_REVERIFY_REQUIRED")

    def test_31_unverified_identity_with_no_channel(self):
        identity = {"status": "UNVERIFIED"}
        channel = {"type": "NONE", "address_or_url": None}
        mailbox = {"status": "UNKNOWN"}
        self.assertEqual(contact_identity.overall_status(identity, channel, mailbox), "CONTACT_UNVERIFIED")


# ---------------------------------------------------------------------------
# Email generation
# ---------------------------------------------------------------------------
class TestEmailGeneration(unittest.TestCase):
    def _fixtures(self):
        prospect = {"business_name": "Acme Roofing"}
        contact = {"identity": {"person_name": "Jane Doe"}, "channel": {"type": "NAMED_EMAIL", "address_or_url": "jane@acme.com"}}
        wedge = {"opportunity_type": "COMPETITOR_GAP", "observation": "Rival Roofing ranks #1 for 'emergency roof repair charlotte' and Acme does not appear in the top 10."}
        asset = {"asset_type": "THREE_POINT_COMPARISON"}
        return prospect, contact, wedge, asset

    def test_32_entry_gate(self):
        self.assertTrue(generate_outreach_email.entry_allowed("CONTACT_VERIFIED"))
        self.assertTrue(generate_outreach_email.entry_allowed("CONTACT_FORM_READY"))
        self.assertFalse(generate_outreach_email.entry_allowed("CONTACT_UNVERIFIED"))

    def test_33_body_contains_real_observation(self):
        prospect, contact, wedge, asset = self._fixtures()
        body = generate_outreach_email.build_body(prospect, contact, wedge, asset, "Ali")
        self.assertIn("Rival Roofing", body)

    def test_34_no_fabricated_metrics_in_mechanism(self):
        prospect, contact, wedge, asset = self._fixtures()
        body = generate_outreach_email.build_body(prospect, contact, wedge, asset, "Ali")
        ok, _ = commercial_mechanism_is_defensible(body)
        self.assertTrue(ok)

    def test_35_greets_named_person_when_available(self):
        prospect, contact, wedge, asset = self._fixtures()
        body = generate_outreach_email.build_body(prospect, contact, wedge, asset, "Ali")
        self.assertTrue(body.startswith("Hi Jane Doe,"))

    def test_36_generic_greeting_without_named_person(self):
        prospect, contact, wedge, asset = self._fixtures()
        contact["identity"]["person_name"] = None
        body = generate_outreach_email.build_body(prospect, contact, wedge, asset, "Ali")
        self.assertTrue(body.startswith("Hi,"))

    def test_37_word_count_within_target_band(self):
        prospect, contact, wedge, asset = self._fixtures()
        body = generate_outreach_email.build_body(prospect, contact, wedge, asset, "Ali")
        wc = generate_outreach_email.word_count(body)
        self.assertLessEqual(wc, OUTREACH_CFG["email"]["word_count_hard_ceiling"])

    def test_38_subject_mentions_business_name(self):
        subject = generate_outreach_email.build_subject("Acme Roofing", {"opportunity_type": "COMPETITOR_GAP"})
        self.assertIn("Acme Roofing", subject)


# ---------------------------------------------------------------------------
# QA gate
# ---------------------------------------------------------------------------
class TestQAGate(IsolatedDataMixin, unittest.TestCase):
    def _good_inputs(self):
        contact = {"overall_status": "CONTACT_VERIFIED", "channel": {"address_or_url": "jane@acme.com"},
                   "identity": {"status": "VERIFIED"}}
        wedge = {"confidence": 0.8, "observation": "Rival Roofing ranks #1 for a keyword Acme does not appear for.",
                 "evidence": []}
        asset = {"asset_type": "THREE_POINT_COMPARISON"}
        draft = {"body": "Hi Jane Doe,\n\nRival Roofing ranks #1.\n\nThat's real signal.\n\nWant the comparison? -- Ali",
                 "subject": "Quick note", "word_count": 20}
        dossier = {"business": {"name": "Acme Roofing"}, "observed_at": _lib.now_iso()}
        return contact, wedge, asset, draft, dossier

    def test_39_passes_clean_draft(self):
        contact, wedge, asset, draft, dossier = self._good_inputs()
        verdict, reasons = qa_outreach_email.apply_qa("biz-qa-1", contact, wedge, asset, draft, dossier, OUTREACH_CFG, LIMITS)
        self.assertEqual(verdict, "QA_PASS", reasons)

    def test_40_fails_unverified_recipient(self):
        contact, wedge, asset, draft, dossier = self._good_inputs()
        contact["overall_status"] = "CONTACT_UNVERIFIED"
        verdict, reasons = qa_outreach_email.apply_qa("biz-qa-2", contact, wedge, asset, draft, dossier, OUTREACH_CFG, LIMITS)
        self.assertEqual(verdict, "QA_FAILED")

    def test_41_fails_suppressed_recipient(self):
        outreach_lib.add_suppression("BOUNCE", email="jane@acme.com")
        contact, wedge, asset, draft, dossier = self._good_inputs()
        verdict, reasons = qa_outreach_email.apply_qa("biz-qa-3", contact, wedge, asset, draft, dossier, OUTREACH_CFG, LIMITS)
        self.assertEqual(verdict, "QA_FAILED")
        self.assertTrue(any("suppress" in r for r in reasons))

    def test_42_fails_account_locked(self):
        outreach_lib.register_touch("biz-qa-4", None, "FIRST_TOUCH_SENT")
        contact, wedge, asset, draft, dossier = self._good_inputs()
        verdict, reasons = qa_outreach_email.apply_qa("biz-qa-4", contact, wedge, asset, draft, dossier, OUTREACH_CFG, LIMITS)
        self.assertEqual(verdict, "QA_FAILED")

    def test_43_fails_low_confidence_wedge(self):
        contact, wedge, asset, draft, dossier = self._good_inputs()
        wedge["confidence"] = 0.3
        verdict, reasons = qa_outreach_email.apply_qa("biz-qa-5", contact, wedge, asset, draft, dossier, OUTREACH_CFG, LIMITS)
        self.assertEqual(verdict, "QA_FAILED")

    def test_44_fails_missing_asset(self):
        contact, wedge, asset, draft, dossier = self._good_inputs()
        verdict, reasons = qa_outreach_email.apply_qa("biz-qa-6", contact, wedge, None, draft, dossier, OUTREACH_CFG, LIMITS)
        self.assertEqual(verdict, "QA_FAILED")

    def test_45_fails_generic_observation_company_swap(self):
        contact, wedge, asset, draft, dossier = self._good_inputs()
        wedge["observation"] = "Your service pages could be better."
        verdict, reasons = qa_outreach_email.apply_qa("biz-qa-7", contact, wedge, asset, draft, dossier, OUTREACH_CFG, LIMITS)
        self.assertEqual(verdict, "QA_FAILED")

    def test_46_fails_forbidden_generic_agency_phrase(self):
        contact, wedge, asset, draft, dossier = self._good_inputs()
        draft["body"] += " We offer SEO services to boost rankings."
        verdict, reasons = qa_outreach_email.apply_qa("biz-qa-8", contact, wedge, asset, draft, dossier, OUTREACH_CFG, LIMITS)
        self.assertEqual(verdict, "QA_FAILED")

    def test_47_fails_word_count_ceiling(self):
        contact, wedge, asset, draft, dossier = self._good_inputs()
        draft["word_count"] = 999
        verdict, reasons = qa_outreach_email.apply_qa("biz-qa-9", contact, wedge, asset, draft, dossier, OUTREACH_CFG, LIMITS)
        self.assertEqual(verdict, "QA_FAILED")

    def test_48_fails_stale_dossier(self):
        contact, wedge, asset, draft, dossier = self._good_inputs()
        dossier["observed_at"] = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        verdict, reasons = qa_outreach_email.apply_qa("biz-qa-10", contact, wedge, asset, draft, dossier, OUTREACH_CFG, LIMITS)
        self.assertEqual(verdict, "QA_FAILED")

    def test_49_fails_named_greeting_without_verified_identity(self):
        contact, wedge, asset, draft, dossier = self._good_inputs()
        contact["identity"]["status"] = "COMPANY_INBOX_ONLY"
        verdict, reasons = qa_outreach_email.apply_qa("biz-qa-11", contact, wedge, asset, draft, dossier, OUTREACH_CFG, LIMITS)
        self.assertEqual(verdict, "QA_FAILED")

    def test_50_fabricated_revenue_claim_fails(self):
        contact, wedge, asset, draft, dossier = self._good_inputs()
        draft["body"] += " You're losing $5,000 per month because of this."
        verdict, reasons = qa_outreach_email.apply_qa("biz-qa-12", contact, wedge, asset, draft, dossier, OUTREACH_CFG, LIMITS)
        self.assertEqual(verdict, "QA_FAILED")


# ---------------------------------------------------------------------------
# Send window planner
# ---------------------------------------------------------------------------
class TestSendWindow(unittest.TestCase):
    def test_51_no_timezone_mapping_is_honest_failure(self):
        plan, reason = send_window_planner.plan_window("ZZ", "roofing", OUTREACH_CFG)
        self.assertIsNone(plan)
        self.assertIn("timezone", reason)

    def test_52_professional_niche_uses_professional_window(self):
        start, end = send_window_planner.pick_window_bounds("family_law", OUTREACH_CFG)
        self.assertEqual((start, end), tuple(OUTREACH_CFG["send_window"]["professional_window"]))

    def test_53_home_service_niche_uses_home_service_window(self):
        start, end = send_window_planner.pick_window_bounds("roofing", OUTREACH_CFG)
        self.assertEqual((start, end), tuple(OUTREACH_CFG["send_window"]["home_service_window"]))

    def test_54_planned_datetime_always_tue_fri(self):
        plan, reason = send_window_planner.plan_window("NC", "roofing", OUTREACH_CFG)
        self.assertIsNotNone(plan, reason)
        self.assertIn(plan["local_weekday"], ["Tuesday", "Wednesday", "Thursday", "Friday"])

    def test_55_weekend_now_rolls_forward_to_tuesday(self):
        # A Saturday: 2026-09-05 is a Saturday.
        saturday_utc = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
        plan, _ = send_window_planner.plan_window("NC", "roofing", OUTREACH_CFG, now_utc=saturday_utc)
        self.assertEqual(plan["local_weekday"], "Tuesday")

    def test_56_within_window_now_schedules_today(self):
        from zoneinfo import ZoneInfo
        # 2026-09-01 is a Tuesday; 07:30 local falls inside roofing's 07:00-08:30 window.
        tuesday_in_window = datetime(2026, 9, 1, 7, 30, tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)
        plan, _ = send_window_planner.plan_window("NC", "roofing", OUTREACH_CFG, now_utc=tuesday_in_window)
        self.assertEqual(plan["local_weekday"], "Tuesday")


# ---------------------------------------------------------------------------
# Send executor -- dry-run only, safety-critical
# ---------------------------------------------------------------------------
class TestSendExecutor(IsolatedDataMixin, unittest.TestCase):
    def _inputs(self):
        p = {"status": "SEND_WINDOW_PLANNED", "website": "acme.com"}
        contact = {"overall_status": "CONTACT_VERIFIED", "channel": {"address_or_url": "jane@acme.com"}}
        draft = {"body": "hi", "content_hash": "abc123", "subject": "hi"}
        return p, contact, draft

    def test_57_production_send_always_raises(self):
        with self.assertRaises(NotImplementedError):
            send_executor.production_send_DESIGNED_NOT_IMPLEMENTED()

    def test_58_dry_run_send_never_calls_real_transport(self):
        p, contact, draft = self._inputs()
        record, reason = send_executor.dry_run_send("biz-send-1", p, contact, draft, OUTREACH_CFG, daily_count_fn=lambda: 0)
        self.assertIsNotNone(record)
        self.assertTrue(record["message_id"].startswith("DRYRUN-"))
        self.assertTrue(record["dry_run"])

    def test_59_blocked_by_suppression(self):
        outreach_lib.add_suppression("BOUNCE", email="jane@acme.com")
        p, contact, draft = self._inputs()
        record, reason = send_executor.dry_run_send("biz-send-2", p, contact, draft, OUTREACH_CFG, daily_count_fn=lambda: 0)
        self.assertIsNone(record)

    def test_60_blocked_by_account_lock(self):
        outreach_lib.register_touch("biz-send-3", "acme.com", "FIRST_TOUCH_SENT")
        p, contact, draft = self._inputs()
        record, reason = send_executor.dry_run_send("biz-send-3", p, contact, draft, OUTREACH_CFG, daily_count_fn=lambda: 0)
        self.assertIsNone(record)

    def test_61_blocked_by_wrong_status(self):
        p, contact, draft = self._inputs()
        p["status"] = "EMAIL_DRAFT_READY"
        record, reason = send_executor.dry_run_send("biz-send-4", p, contact, draft, OUTREACH_CFG, daily_count_fn=lambda: 0)
        self.assertIsNone(record)

    def test_62_blocked_by_daily_ceiling(self):
        p, contact, draft = self._inputs()
        ceiling = OUTREACH_CFG["volume"]["daily_ceiling_initial"]
        record, reason = send_executor.dry_run_send("biz-send-5", p, contact, draft, OUTREACH_CFG, daily_count_fn=lambda: ceiling)
        self.assertIsNone(record)
        self.assertIn("ceiling", reason)

    def test_63_blocked_by_regressed_contact_status(self):
        p, contact, draft = self._inputs()
        contact["overall_status"] = "CONTACT_UNVERIFIED"
        record, reason = send_executor.dry_run_send("biz-send-6", p, contact, draft, OUTREACH_CFG, daily_count_fn=lambda: 0)
        self.assertIsNone(record)

    def test_64_zero_sends_is_a_valid_outcome(self):
        # No assertion of a send happening at all -- this test exists to
        # document that an empty send_log is a fine, expected state.
        self.assertEqual(outreach_lib.load_suppression(), [])


# ---------------------------------------------------------------------------
# Delivery reconciliation + deliverability health
# ---------------------------------------------------------------------------
class TestDeliveryReconciliation(IsolatedDataMixin, unittest.TestCase):
    def test_65_no_evidence_stays_pending(self):
        status, reason = delivery_reconciliation.resolve_delivery(None)
        self.assertIsNone(status)

    def test_66_explicit_no_bounce(self):
        status, _ = delivery_reconciliation.resolve_delivery({"bounce": False})
        self.assertEqual(status, "NO_BOUNCE_DETECTED")

    def test_67_bounce_true(self):
        status, _ = delivery_reconciliation.resolve_delivery({"bounce": True, "bounce_type": "hard"})
        self.assertEqual(status, "DELIVERY_FAILED")

    def test_68_hard_bounce_suppresses(self):
        next_status, suppressed = delivery_reconciliation.handle_bounce("biz-d1", "a@b.com", "b.com", "hard")
        self.assertTrue(suppressed)
        self.assertTrue(outreach_lib.is_suppressed(email="a@b.com")[0])

    def test_69_soft_bounce_does_not_suppress(self):
        next_status, suppressed = delivery_reconciliation.handle_bounce("biz-d2", "a2@b.com", "b.com", "soft")
        self.assertFalse(suppressed)
        self.assertFalse(outreach_lib.is_suppressed(email="a2@b.com")[0])

    def test_70_health_healthy_below_min_sample(self):
        events = [{"event": "ATTEMPT"}] * 5 + [{"event": "BOUNCE", "bounce_type": "hard"}] * 2
        health = delivery_reconciliation.compute_deliverability_health(events, OUTREACH_CFG)
        self.assertEqual(health["status"], "HEALTHY")

    def test_71_health_pauses_above_threshold_with_enough_sample(self):
        events = [{"event": "ATTEMPT"}] * 20 + [{"event": "BOUNCE", "bounce_type": "hard"}] * 2
        health = delivery_reconciliation.compute_deliverability_health(events, OUTREACH_CFG)
        self.assertEqual(health["status"], "PAUSED")

    def test_72_abuse_complaint_immediate_pause(self):
        events = [{"event": "ATTEMPT"}] * 3 + [{"event": "ABUSE_COMPLAINT"}]
        health = delivery_reconciliation.compute_deliverability_health(events, OUTREACH_CFG)
        self.assertEqual(health["status"], "PAUSED")


# ---------------------------------------------------------------------------
# Follow-up sequence
# ---------------------------------------------------------------------------
class TestFollowUp(IsolatedDataMixin, unittest.TestCase):
    def test_73_no_touch_due_immediately_after_day_zero(self):
        now = datetime.now(timezone.utc).isoformat()
        idx = follow_up.next_touch_index(now, 1, OUTREACH_CFG["follow_up"]["day_offsets"])
        self.assertIsNone(idx)

    def test_74_touch_due_after_offset_elapses(self):
        past = (datetime.now(timezone.utc) - timedelta(days=4)).isoformat()
        idx = follow_up.next_touch_index(past, 1, OUTREACH_CFG["follow_up"]["day_offsets"])
        self.assertEqual(idx, 1)

    def test_75_sequence_exhausted_returns_none(self):
        past = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
        idx = follow_up.next_touch_index(past, len(OUTREACH_CFG["follow_up"]["day_offsets"]), OUTREACH_CFG["follow_up"]["day_offsets"])
        self.assertIsNone(idx)

    def test_76_gate_blocks_suppressed(self):
        outreach_lib.add_suppression("BOUNCE", email="f@g.com")
        allowed, _ = follow_up.follow_up_gate("biz-f1", "f@g.com", None)
        self.assertFalse(allowed)

    def test_77_gate_blocks_replied_account(self):
        outreach_lib.register_touch("biz-f2", None, "FIRST_TOUCH_SENT")
        outreach_lib.register_touch("biz-f2", None, "REPLY_RECEIVED")
        allowed, _ = follow_up.follow_up_gate("biz-f2", None, None)
        self.assertFalse(allowed)

    def test_78_no_generic_checkin_without_new_evidence(self):
        wedge = {"observation": "original observation"}
        content, reason = follow_up.build_follow_up_content(1, wedge, None)
        self.assertIsNone(content)

    def test_79_rejects_evidence_identical_to_original(self):
        wedge = {"observation": "same text"}
        content, reason = follow_up.build_follow_up_content(1, wedge, {"observation": "same text"})
        self.assertIsNone(content)

    def test_80_accepts_genuinely_new_evidence(self):
        wedge = {"observation": "original observation"}
        content, reason = follow_up.build_follow_up_content(1, wedge, {"observation": "a new, distinct finding", "source": "x"})
        self.assertIsNotNone(content)


# ---------------------------------------------------------------------------
# Reply classification + human handoff + recycle
# ---------------------------------------------------------------------------
class TestReplyHandling(IsolatedDataMixin, unittest.TestCase):
    def test_81_unsubscribe_detected(self):
        self.assertEqual(reply_handling.classify_reply("Please unsubscribe me from this list."), "UNSUBSCRIBE")

    def test_82_negative_detected(self):
        self.assertEqual(reply_handling.classify_reply("Not interested, thanks."), "NEGATIVE")

    def test_83_positive_requires_explicit_phrase(self):
        self.assertEqual(reply_handling.classify_reply("Sounds good, let's talk next week."), "POSITIVE")

    def test_84_ambiguous_never_becomes_positive(self):
        result = reply_handling.classify_reply("Interesting, I'll have to think about whether this makes sense for us given everything else going on.")
        self.assertNotEqual(result, "POSITIVE")
        self.assertEqual(result, "UNKNOWN")

    def test_85_short_neutral_ack(self):
        self.assertEqual(reply_handling.classify_reply("Thanks."), "NEUTRAL")

    def test_86_empty_reply_unknown(self):
        self.assertEqual(reply_handling.classify_reply(""), "UNKNOWN")

    def test_87_negative_beats_positive_when_both_present(self):
        result = reply_handling.classify_reply("Not interested, please remove me, sounds good otherwise though")
        self.assertIn(result, ("UNSUBSCRIBE", "NEGATIVE"))

    def test_88_recycle_requires_both_time_and_trigger(self):
        old = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
        eligible, _ = reply_handling.recycle_eligible(old, 150, None, OUTREACH_CFG["recycle"]["valid_trigger_signal_types"])
        self.assertFalse(eligible, "time alone must never be sufficient")

    def test_89_recycle_fails_if_too_soon_even_with_trigger(self):
        recent = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        eligible, _ = reply_handling.recycle_eligible(recent, 150, "runs_google_ads", OUTREACH_CFG["recycle"]["valid_trigger_signal_types"])
        self.assertFalse(eligible)

    def test_90_recycle_succeeds_with_both(self):
        old = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
        eligible, _ = reply_handling.recycle_eligible(old, 150, "runs_google_ads", OUTREACH_CFG["recycle"]["valid_trigger_signal_types"])
        self.assertTrue(eligible)

    def test_91_human_handoff_has_recommended_step(self):
        handoff = reply_handling.build_human_handoff("biz-h1", {"business_name": "Acme"}, None, None, None, "text", "POSITIVE")
        self.assertIn("recommended_next_step", handoff)


class TestIdempotency(unittest.TestCase):
    def test_92_artifact_is_current_matches_hash(self):
        self.assertTrue(outreach_lib.artifact_is_current({"content_hash": "x"}, "x"))

    def test_93_artifact_is_current_mismatch(self):
        self.assertFalse(outreach_lib.artifact_is_current({"content_hash": "x"}, "y"))

    def test_94_artifact_is_current_none(self):
        self.assertFalse(outreach_lib.artifact_is_current(None, "x"))


if __name__ == "__main__":
    unittest.main()
