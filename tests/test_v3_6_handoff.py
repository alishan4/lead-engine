"""
V3.6 shared handoff bridge tests. Pure-function/mock-based -- no real
Google Sheets credentials, no network call, no Gmail API client anywhere.
"""
import json
import shutil
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))

import handoff_lib as hl  # noqa: E402
import handoff_backend as hb  # noqa: E402
import sync_handoff  # noqa: E402
import import_outreach_results as ior  # noqa: E402
import export_tracker_csv  # noqa: E402
from _lib import load_yaml, now_iso, write_json, append_jsonl  # noqa: E402


LIMITS = load_yaml("limits.yaml")


def selective_load_yaml(handoff_cfg):
    """sync_handoff.build_rows() calls load_yaml() for BOTH
    'handoff.yaml' and 'limits.yaml' -- a bare `return_value=` mock would
    (and did, during development) silently hand the handoff config back for
    the limits.yaml call too, breaking check_freshness() in a way that has
    nothing to do with the code under test. Route real filenames to the
    real _lib.load_yaml, only 'handoff.yaml' to the fixture config."""
    def _load(name):
        if name == "handoff.yaml":
            return handoff_cfg
        return load_yaml(name)
    return _load


def make_prospect(pid, status="SEND_WINDOW_PLANNED", **extra):
    p = {
        "id": pid, "business_name": "Fixture Biz", "website": "https://fixture-biz.test/",
        "city": "Testville", "state": "TS", "country": "US", "niche": "hvac",
        "status": status, "why_this_company": "c", "why_this_problem": "p",
        "why_now": None, "why_likely_buyer": "b",
    }
    p.update(extra)
    return p


def make_contact(channel_type="COMPANY_EMAIL", identity_status="COMPANY_INBOX_ONLY", email="office@fixture-biz.test"):
    return {
        "prospect_id": "x", "business_name": "Fixture Biz",
        "identity": {"status": identity_status, "confidence": 0.7, "person_name": None, "role": None,
                     "email": email if channel_type != "CONTACT_FORM" else None, "sources": []},
        "mailbox": {"status": "UNKNOWN"},
        "channel": {"type": channel_type, "address_or_url": email},
        "overall_status": "CONTACT_VERIFIED" if channel_type != "CONTACT_FORM" else "CONTACT_FORM_READY",
        "generated_at": now_iso(),
    }


def make_ready_row(pid):
    return {
        "prospect_id": pid, "business_name": "Fixture Biz", "website": "https://fixture-biz.test/",
        "niche": "hvac", "city": "Testville", "state": "TS",
        "email": {"subject": "s", "body": "b", "word_count": 10},
        "send_window": {"timezone": "America/Chicago", "local_datetime": "2026-01-20T07:00:00-06:00", "window": "07:00-08:30",
                         "pkt_datetime": "2026-01-20T18:00:00+05:00"},
        "qa_status": "QA_PASS",
        "fit_gap_snapshot": {"fit": {"confirmed": 62, "potential": 90}, "gap": {"confirmed": 48, "potential": 70}},
        "content_hash": "abc123",
        "lead_engine_status_at_export": "SEND_WINDOW_PLANNED",
        "exported_at": now_iso(),
    }


def make_window():
    return {"timezone": "America/Chicago", "local_datetime": "2026-01-20T07:00:00-06:00", "window": "07:00-08:30",
            "pkt_datetime": "2026-01-20T18:00:00+05:00"}


def make_draft():
    return {"subject": "s", "body": "b", "word_count": 10, "content_hash": "d1"}


def make_dossier(fresh=True):
    return {"observed_at": now_iso() if fresh else "2000-01-01T00:00:00+00:00"}


class TestHandoffLibPure(unittest.TestCase):
    """Items 1, 2, 3, 5, 6, 8, 15 at the pure-function level -- no I/O."""

    def test_eligible_named_email_lead(self):
        p = make_prospect("p1")
        c = make_contact("NAMED_EMAIL", "VERIFIED")
        ok, reason = hl.is_eligible_for_export(p, c, make_window(), make_draft(), make_dossier(), LIMITS)
        self.assertTrue(ok, reason)

    def test_ineligible_wrong_status(self):
        p = make_prospect("p1", status="ASSET_STAGED")
        c = make_contact()
        ok, reason = hl.is_eligible_for_export(p, c, make_window(), make_draft(), make_dossier(), LIMITS)
        self.assertFalse(ok)

    def test_ineligible_unverified_identity_never_enters_email_queue(self):
        p = make_prospect("p1")
        c = make_contact("NAMED_EMAIL", "UNVERIFIED")
        c["overall_status"] = "CONTACT_UNVERIFIED"
        ok, reason = hl.is_eligible_for_export(p, c, make_window(), make_draft(), make_dossier(), LIMITS)
        self.assertFalse(ok)

    def test_contact_form_channel_routes_to_form_queue(self):
        fields = {"preferred_channel": "CONTACT_FORM"}
        self.assertEqual(hl.queue_for_channel(fields["preferred_channel"]), "CONTACT_FORM_READY")

    def test_email_channel_routes_to_email_queue(self):
        for ch in ("NAMED_EMAIL", "COMPANY_EMAIL"):
            self.assertEqual(hl.queue_for_channel(ch), "EMAIL_READY")

    def test_merge_row_first_export_has_null_external_fields(self):
        p, c = make_prospect("p1"), make_contact("NAMED_EMAIL", "VERIFIED")
        fields = hl.build_lead_engine_fields(p, make_ready_row("p1"), c, None, None, make_dossier(), LIMITS)
        row = hl.merge_row(None, fields)
        for f in hl.EXTERNAL_OWNED_FIELDS:
            self.assertIsNone(row[f])
        self.assertEqual(row["last_action"], "EXPORTED")

    def test_merge_row_never_clobbers_gmail_fields(self):
        """Items 5 & 6: a Lead Engine re-sync must never overwrite
        gmail_state/gmail_message_id/etc. even though the fresh
        lead_engine_fields dict has no knowledge of them at all."""
        existing = {"gmail_state": "GMAIL_SENT", "gmail_message_id": "MID1", "gmail_thread_id": "TID1",
                    "delivery_state": "NO_BOUNCE_DETECTED", "reply_state": None, "follow_up_state": None,
                    "suppression_reason": None, "created_at": "2026-01-01T00:00:00+00:00",
                    "last_action": "GMAIL_SENT", "last_action_at": "2026-01-02T00:00:00+00:00",
                    "human_review": False}
        p, c = make_prospect("p1"), make_contact("NAMED_EMAIL", "VERIFIED")
        fresh_fields = hl.build_lead_engine_fields(p, make_ready_row("p1"), c, None, None, make_dossier(), LIMITS)
        merged = hl.merge_row(existing, fresh_fields)
        self.assertEqual(merged["gmail_state"], "GMAIL_SENT")
        self.assertEqual(merged["gmail_message_id"], "MID1")
        self.assertEqual(merged["delivery_state"], "NO_BOUNCE_DETECTED")
        self.assertEqual(merged["created_at"], "2026-01-01T00:00:00+00:00")
        # Lead-engine-owned fields DID refresh
        self.assertEqual(merged["lead_engine_state"], "SEND_WINDOW_PLANNED")

    def test_lead_engine_state_distinct_from_gmail_state(self):
        """Item 15: READY_TO_SEND/SEND_WINDOW_PLANNED (lead_engine_state)
        must never be conflated with GMAIL_SENT (gmail_state), nor
        NO_BOUNCE_DETECTED (delivery_state) with DELIVERED."""
        row = hl.merge_row(None, hl.build_lead_engine_fields(
            make_prospect("p1"), make_ready_row("p1"), make_contact("NAMED_EMAIL", "VERIFIED"), None, None,
            make_dossier(), LIMITS))
        row, applied, _ = hl.apply_event(row, {"lead_id": "p1", "event_type": "GMAIL_SENT", "event_at": now_iso(),
                                                 "gmail_message_id": "MID1", "gmail_thread_id": "TID1"})
        self.assertTrue(applied)
        self.assertEqual(row["lead_engine_state"], "SEND_WINDOW_PLANNED")
        self.assertEqual(row["gmail_state"], "GMAIL_SENT")
        self.assertNotEqual(row["lead_engine_state"], row["gmail_state"])
        row, applied, _ = hl.apply_event(row, {"lead_id": "p1", "event_type": "NO_BOUNCE_DETECTED", "event_at": now_iso()})
        self.assertEqual(row["delivery_state"], "NO_BOUNCE_DETECTED")
        self.assertNotIn("DELIVERED", [row["delivery_state"]])  # never claims positive delivery proof

    def test_apply_event_rejects_stale_event(self):
        row = hl.merge_row(None, hl.build_lead_engine_fields(
            make_prospect("p1"), make_ready_row("p1"), make_contact("NAMED_EMAIL", "VERIFIED"), None, None,
            make_dossier(), LIMITS))
        row, applied1, _ = hl.apply_event(row, {"lead_id": "p1", "event_type": "GMAIL_SENT", "event_at": "2026-02-01T00:00:00+00:00"})
        self.assertTrue(applied1)
        row2, applied2, reason = hl.apply_event(row, {"lead_id": "p1", "event_type": "SEND_FAILED", "event_at": "2026-01-01T00:00:00+00:00"})
        self.assertFalse(applied2)
        self.assertEqual(row2["gmail_state"], "GMAIL_SENT")  # stale event never regressed it

    def test_duplicate_gmail_event_dedup_key_ignores_event_at(self):
        """Item 8: same message/thread id + event_type dedups regardless of
        exact event_at formatting."""
        e1 = {"lead_id": "p1", "event_type": "GMAIL_SENT", "event_at": "2026-01-20T13:00:00+00:00",
              "gmail_message_id": "MID1", "gmail_thread_id": "TID1"}
        e2 = {"lead_id": "p1", "event_type": "GMAIL_SENT", "event_at": "2026-01-20T13:00:00.000+00:00",
              "gmail_message_id": "MID1", "gmail_thread_id": "TID1"}
        self.assertEqual(hl.event_dedup_key(e1), hl.event_dedup_key(e2))


class IsolatedHandoffMixin:
    """Redirects every handoff/prospect/lead/outreach path to a throwaway
    temp dir, exactly like V3.3's IsolatedDataMixin / V3.5's
    IsolatedWorkerMixin -- never touches real data."""

    def setUp(self):
        self.tmp = Path("/tmp") / f"v3_6_test_{id(self)}"
        for sub in ("prospects", "leads", "outreach", "handoff"):
            (self.tmp / sub).mkdir(parents=True, exist_ok=True)
        self.cfg = load_yaml("handoff.yaml")
        self.cfg = json.loads(json.dumps(self.cfg))  # deep copy
        self.cfg["local_backend"]["dir"] = "handoff"  # LocalFileBackend/export_tracker_csv resolve this against
                                                        # DATA (patched below), matching real production behavior
        # V3.6.1: force local-only regardless of this machine's real,
        # on-disk config/handoff.yaml -- if that file has ever been pointed
        # at a real Google Sheet (as it legitimately is on a machine where
        # V3.6's live verification ran), tests must NEVER inherit that or
        # they silently make real network calls. Every test that wants to
        # exercise the google_sheets path builds its OWN explicit,
        # unconfigured copy (see TestIdempotentSyncAndLocalFallback) --
        # never the ambient real credentials.
        self.cfg["backend"] = "local"
        self.cfg["google_sheets"]["service_account_file"] = None
        self.cfg["google_sheets"]["spreadsheet_id"] = None

        self._orig_sync = {k: getattr(sync_handoff, k) for k in ("PROSPECTS", "LEADS", "OUTREACH")}
        self._orig_ior = {k: getattr(ior, k) for k in ("RESULTS_PATH",)}
        self._orig_etc = {"PROSPECTS": export_tracker_csv.PROSPECTS, "DATA": export_tracker_csv.DATA}
        self._orig_hb_data = hb.DATA

        sync_handoff.PROSPECTS = self.tmp / "prospects"
        sync_handoff.LEADS = self.tmp / "leads"
        sync_handoff.OUTREACH = self.tmp / "outreach"
        ior.RESULTS_PATH = self.tmp / "outreach" / "outreach_results.jsonl"
        export_tracker_csv.PROSPECTS = self.tmp / "prospects"
        export_tracker_csv.DATA = self.tmp
        hb.DATA = self.tmp  # LocalFileBackend resolves its dir as DATA / cfg["dir"].name

    def tearDown(self):
        for k, v in self._orig_sync.items():
            setattr(sync_handoff, k, v)
        for k, v in self._orig_ior.items():
            setattr(ior, k, v)
        export_tracker_csv.PROSPECTS = self._orig_etc["PROSPECTS"]
        export_tracker_csv.DATA = self._orig_etc["DATA"]
        hb.DATA = self._orig_hb_data
        shutil.rmtree(self.tmp, ignore_errors=True)

    def seed_discovered(self, records):
        with open(self.tmp / "prospects" / "discovered.jsonl", "w") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

    def seed_ready_to_send(self, rows):
        with open(self.tmp / "outreach" / "ready_to_send.jsonl", "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

    def seed_lead_files(self, pid, contact=None, asset=None, wedge=None, dossier=None, window=None, draft=None):
        d = self.tmp / "leads" / pid
        d.mkdir(parents=True, exist_ok=True)
        if contact is not None:
            write_json(d / "contact_record.json", contact)
        if asset is not None:
            write_json(d / "staged_asset.json", asset)
        if wedge is not None:
            write_json(d / "primary_wedge.json", wedge)
        if dossier is not None:
            write_json(d / "intelligence_dossier.json", dossier)
        if window is not None:
            write_json(d / "send_window.json", window)
        if draft is not None:
            write_json(d / "email_draft.json", draft)

    @patch("handoff_backend.load_yaml")
    def build_rows_with_cfg(self, mock_load_yaml, logfn=lambda m: None):
        mock_load_yaml.return_value = self.cfg
        with patch("sync_handoff.load_yaml", side_effect=selective_load_yaml(self.cfg)):
            return sync_handoff.build_rows(logfn)


class TestBuildRowsIntegration(IsolatedHandoffMixin, unittest.TestCase):
    """Items 1, 2, 3, 11 end to end through sync_handoff.build_rows()."""

    def test_only_eligible_leads_produce_rows(self):
        self.seed_discovered([
            make_prospect("email-lead", status="SEND_WINDOW_PLANNED"),
            make_prospect("not-ready-lead", status="ASSET_STAGED"),
        ])
        self.seed_ready_to_send([make_ready_row("email-lead"), make_ready_row("not-ready-lead")])
        self.seed_lead_files("email-lead", contact=make_contact("NAMED_EMAIL", "VERIFIED"),
                              dossier=make_dossier(), window=make_window(), draft=make_draft())
        # not-ready-lead has no lead files at all -- must be skipped, not crash
        email_rows, form_rows, failures, skipped = self.build_rows_with_cfg()
        self.assertEqual(len(email_rows), 1)
        self.assertEqual(email_rows[0]["lead_id"], "email-lead")
        self.assertEqual(form_rows, [])
        self.assertEqual(failures, [])
        self.assertGreaterEqual(skipped, 1)

    def test_contact_form_lead_goes_to_form_rows_only(self):
        self.seed_discovered([make_prospect("form-lead")])
        self.seed_ready_to_send([make_ready_row("form-lead")])
        self.seed_lead_files("form-lead", contact=make_contact("CONTACT_FORM", "FORM_ONLY"),
                              dossier=make_dossier(), window=make_window(), draft=make_draft())
        email_rows, form_rows, failures, skipped = self.build_rows_with_cfg()
        self.assertEqual(email_rows, [])
        self.assertEqual(len(form_rows), 1)
        self.assertEqual(form_rows[0]["preferred_channel"], "CONTACT_FORM")

    def test_one_bad_lead_does_not_block_others(self):
        self.seed_discovered([make_prospect("good-lead"), make_prospect("bad-lead")])
        self.seed_ready_to_send([make_ready_row("good-lead"), make_ready_row("bad-lead")])
        self.seed_lead_files("good-lead", contact=make_contact("NAMED_EMAIL", "VERIFIED"),
                              dossier=make_dossier(), window=make_window(), draft=make_draft())
        # bad-lead: corrupt contact_record.json to force a real exception during load_json
        bad_dir = self.tmp / "leads" / "bad-lead"
        bad_dir.mkdir(parents=True, exist_ok=True)
        (bad_dir / "contact_record.json").write_text("{not valid json")
        (bad_dir / "intelligence_dossier.json").write_text(json.dumps(make_dossier()))
        (bad_dir / "send_window.json").write_text(json.dumps(make_window()))
        (bad_dir / "email_draft.json").write_text(json.dumps(make_draft()))

        email_rows, form_rows, failures, skipped = self.build_rows_with_cfg()
        self.assertEqual(len(email_rows), 1)
        self.assertEqual(email_rows[0]["lead_id"], "good-lead")
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["lead_id"], "bad-lead")


class TestIdempotentSyncAndLocalFallback(IsolatedHandoffMixin, unittest.TestCase):
    """Items 4, 9, 10."""

    def test_duplicate_sync_updates_same_row_no_duplicates(self):
        self.seed_discovered([make_prospect("p1")])
        self.seed_ready_to_send([make_ready_row("p1")])
        self.seed_lead_files("p1", contact=make_contact("NAMED_EMAIL", "VERIFIED"),
                              dossier=make_dossier(), window=make_window(), draft=make_draft())
        with patch("sync_handoff.load_yaml", side_effect=selective_load_yaml(self.cfg)):
            result1 = sync_handoff.sync(logfn=lambda m: None)
            result2 = sync_handoff.sync(logfn=lambda m: None)
        self.assertEqual(result1["handoff_sync_status"], "SYNCED")
        self.assertEqual(result2["handoff_sync_status"], "SYNCED")
        backend = hb.LocalFileBackend(self.cfg)
        self.assertEqual(len(backend.all_rows()), 1)

    def test_local_backend_works_with_zero_google_credentials(self):
        cfg = json.loads(json.dumps(self.cfg))
        cfg["backend"] = "local"
        backend = hb.build_backend(cfg)
        self.assertIsInstance(backend, hb.LocalFileBackend)
        backend.export_ready([dict(hl.merge_row(None, hl.build_lead_engine_fields(
            make_prospect("p1"), make_ready_row("p1"), make_contact("NAMED_EMAIL", "VERIFIED"), None, None,
            make_dossier(), LIMITS)))], [])
        self.assertEqual(len(backend.all_rows()), 1)

    def test_remote_sync_failure_preserves_local_queue(self):
        cfg = json.loads(json.dumps(self.cfg))
        cfg["backend"] = "google_sheets"
        cfg["google_sheets"]["service_account_file"] = None  # never configured
        self.seed_discovered([make_prospect("p1")])
        self.seed_ready_to_send([make_ready_row("p1")])
        self.seed_lead_files("p1", contact=make_contact("NAMED_EMAIL", "VERIFIED"),
                              dossier=make_dossier(), window=make_window(), draft=make_draft())
        with patch("sync_handoff.load_yaml", side_effect=selective_load_yaml(cfg)):
            result = sync_handoff.sync(logfn=lambda m: None)
        self.assertEqual(result["handoff_sync_status"], "SHARED_HANDOFF_AUTH_REQUIRED")
        backend = hb.LocalFileBackend(self.cfg)
        self.assertEqual(len(backend.all_rows()), 1)  # local queue intact despite remote failure


class TestResultImportIdempotency(IsolatedHandoffMixin, unittest.TestCase):
    """Items 5, 6, 7, 8 through the full import_outreach_results.py path."""

    def _seed_row(self):
        backend = hb.LocalFileBackend(self.cfg)
        row = hl.merge_row(None, hl.build_lead_engine_fields(
            make_prospect("p1"), make_ready_row("p1"), make_contact("NAMED_EMAIL", "VERIFIED"), None, None,
            make_dossier(), LIMITS))
        backend.export_ready([row], [])
        return backend

    def test_import_applies_event_and_is_idempotent_on_rerun(self):
        self._seed_row()
        events_path = self.tmp / "outreach" / "outreach_results.jsonl"
        append_jsonl(events_path, {"lead_id": "p1", "event_type": "GMAIL_SENT", "event_at": now_iso(),
                                     "gmail_message_id": "MID1", "gmail_thread_id": "TID1", "source": "test"})
        with patch("import_outreach_results.load_yaml", return_value=self.cfg):
            r1 = ior.import_results(logfn=lambda m: None)
            r2 = ior.import_results(logfn=lambda m: None)
        self.assertEqual(r1["events_applied"], 1)
        self.assertEqual(r2["events_applied"], 0)
        self.assertEqual(r2["events_skipped_duplicate"], 1)
        backend = hb.LocalFileBackend(self.cfg)
        self.assertEqual(backend.all_rows()["p1"]["gmail_state"], "GMAIL_SENT")

    def test_lead_engine_resync_after_import_never_clobbers_gmail_state(self):
        """Items 5 & 6 end to end: import a GMAIL_SENT event, THEN re-run a
        normal Lead Engine sync -- gmail_state must survive."""
        self.seed_discovered([make_prospect("p1")])
        self.seed_ready_to_send([make_ready_row("p1")])
        self.seed_lead_files("p1", contact=make_contact("NAMED_EMAIL", "VERIFIED"),
                              dossier=make_dossier(), window=make_window(), draft=make_draft())
        with patch("sync_handoff.load_yaml", side_effect=selective_load_yaml(self.cfg)):
            sync_handoff.sync(logfn=lambda m: None)

        events_path = self.tmp / "outreach" / "outreach_results.jsonl"
        append_jsonl(events_path, {"lead_id": "p1", "event_type": "GMAIL_SENT", "event_at": now_iso(),
                                     "gmail_message_id": "MID1", "gmail_thread_id": "TID1", "source": "test"})
        with patch("import_outreach_results.load_yaml", return_value=self.cfg):
            ior.import_results(logfn=lambda m: None)

        with patch("sync_handoff.load_yaml", side_effect=selective_load_yaml(self.cfg)):
            sync_handoff.sync(logfn=lambda m: None)  # Lead Engine re-syncs again -- must NOT clobber

        backend = hb.LocalFileBackend(self.cfg)
        row = backend.all_rows()["p1"]
        self.assertEqual(row["gmail_state"], "GMAIL_SENT")
        self.assertEqual(row["gmail_message_id"], "MID1")

    def test_duplicate_gmail_event_across_two_import_calls_not_duplicated(self):
        self._seed_row()
        events_path = self.tmp / "outreach" / "outreach_results.jsonl"
        append_jsonl(events_path, {"lead_id": "p1", "event_type": "GMAIL_SENT", "event_at": "2026-01-20T13:00:00+00:00",
                                     "gmail_message_id": "MID1", "gmail_thread_id": "TID1", "source": "test"})
        append_jsonl(events_path, {"lead_id": "p1", "event_type": "GMAIL_SENT", "event_at": "2026-01-20T13:00:00.500+00:00",
                                     "gmail_message_id": "MID1", "gmail_thread_id": "TID1", "source": "test"})
        with patch("import_outreach_results.load_yaml", return_value=self.cfg):
            result = ior.import_results(logfn=lambda m: None)
        self.assertEqual(result["events_applied"], 1)
        self.assertEqual(result["events_skipped_duplicate"], 1)


class TestTrackerCsvExports(IsolatedHandoffMixin, unittest.TestCase):
    """Item 16."""

    def test_csvs_generated_with_expected_headers(self):
        self.seed_discovered([make_prospect("p1")])
        backend = hb.LocalFileBackend(self.cfg)
        row = hl.merge_row(None, hl.build_lead_engine_fields(
            make_prospect("p1"), make_ready_row("p1"), make_contact("NAMED_EMAIL", "VERIFIED"), None, None,
            make_dossier(), LIMITS))
        backend.export_ready([row], [])

        with patch("export_tracker_csv.load_yaml", return_value=self.cfg):
            counts = export_tracker_csv.export_all(logfn=lambda m: None)

        out_dir = self.tmp / "handoff"
        for fname in ("leads_master.csv", "outreach_log.csv", "follow_up_queue.csv", "daily_pipeline.csv"):
            self.assertTrue((out_dir / fname).exists(), f"{fname} not generated")
        self.assertEqual(counts["leads_master_rows"], 1)
        self.assertEqual(counts["outreach_log_rows"], 1)


class TestNoGmailSurfaceInHandoff(unittest.TestCase):
    """Items 12, 13, 14."""

    FILES = ("handoff_lib.py", "handoff_backend.py", "sync_handoff.py",
              "import_outreach_results.py", "export_tracker_csv.py")

    def test_no_gmail_credential_imports(self):
        # Checks actual import statements, not prose -- these modules'
        # docstrings deliberately *document* the smtplib/imaplib boundary in
        # plain English (expected, good), which a bare substring check would
        # misflag.
        for fname in self.FILES:
            text = (SCRIPTS / fname).read_text()
            for term in ("smtplib", "imaplib", "oauth2client", "google_auth_oauthlib"):
                self.assertNotIn(f"import {term}", text, f"{fname} must never import {term}")
            self.assertNotIn("from google.oauth2.credentials", text)

    def test_no_gmail_api_service_built(self):
        text = (SCRIPTS / "handoff_backend.py").read_text()
        self.assertNotIn('build("gmail"', text)
        self.assertIn('build("sheets"', text)

    def test_gitignore_covers_handoff_dir(self):
        gitignore = (ROOT / ".gitignore").read_text()
        self.assertIn("data/handoff/*", gitignore)

    def test_handoff_dir_not_tracked_by_git(self):
        import subprocess
        (ROOT / "data" / "handoff").mkdir(parents=True, exist_ok=True)
        probe = ROOT / "data" / "handoff" / "_v3_6_test_probe.json"
        probe.write_text("{}")
        try:
            result = subprocess.run(["git", "check-ignore", str(probe)], cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, "data/handoff/ files must be git-ignored")
        finally:
            probe.unlink(missing_ok=True)


class TestScopeAndCsvColumns(unittest.TestCase):
    def test_handoff_row_columns_match_spec_count(self):
        self.assertEqual(len(hl.COLUMNS), 50)

    def test_no_v3_scoring_files_modified_by_v3_6(self):
        # Static guard: none of the frozen V3.1-V3.4 scoring/routing modules
        # import anything from the new V3.6 modules (one-way dependency only).
        for fname in ("score_leads.py", "qualify_leads.py", "assess_commercial_fit.py",
                      "assess_google_gap.py", "wedge_selection.py"):
            text = (SCRIPTS / fname).read_text()
            for v36_mod in ("handoff_lib", "handoff_backend", "sync_handoff", "import_outreach_results"):
                self.assertNotIn(v36_mod, text)


# ---------------------------------------------------------------------------
# V3.6.1 -- GoogleSheetsBackend RESULTS-tab handling (fake Sheets API, no
# network call, no real credentials).
# ---------------------------------------------------------------------------
class _FakeExec:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _FakeSheetsValues:
    """Minimal stand-in for `service.spreadsheets().values()`. `tabs` is
    {tab_name: [[row...], ...]} (row 0 is the header, if any)."""

    def __init__(self, tabs):
        self.tabs = dict(tabs)
        self.update_calls = []

    def get(self, spreadsheetId, range):
        tab = range.split("!")[0]
        data = self.tabs.get(tab, [])
        return _FakeExec({"values": data} if data else {})

    def update(self, spreadsheetId, range, valueInputOption, body):
        tab = range.split("!")[0]
        self.update_calls.append((tab, range, body["values"]))
        self.tabs[tab] = body["values"]
        return _FakeExec({})


class _FakeSheetsService:
    def __init__(self, tabs):
        self._values = _FakeSheetsValues(tabs)

    def spreadsheets(self):
        return self

    def values(self):
        return self._values


def _sheets_backend(results_tab="__unset__"):
    cfg = {
        "service_account_file": "/fake/path.json", "spreadsheet_id": "sid",
        "scopes": ["https://www.googleapis.com/auth/spreadsheets"],
        "email_ready_tab": "EMAIL_READY", "contact_form_ready_tab": "CONTACT_FORM_READY",
    }
    if results_tab != "__unset__":
        cfg["results_tab"] = results_tab
    return hb.GoogleSheetsBackend({"google_sheets": cfg})


class TestGoogleSheetsResultsTabHandling(unittest.TestCase):
    """Items: empty RESULTS tab, header creation/validation, configurable
    results tab, backward-compatible default RESULTS tab."""

    def test_empty_results_tab_returns_no_events(self):
        backend = _sheets_backend()
        fake = _FakeSheetsService({})  # RESULTS tab has nothing at all
        with patch.object(hb.GoogleSheetsBackend, "_client", return_value=(fake, "sid")):
            events = backend.import_results()
        self.assertEqual(events, [])

    def test_empty_results_tab_self_heals_canonical_header(self):
        backend = _sheets_backend()
        fake = _FakeSheetsService({})
        with patch.object(hb.GoogleSheetsBackend, "_client", return_value=(fake, "sid")):
            backend.import_results()
        self.assertEqual(len(fake._values.update_calls), 1)
        tab, rng, values = fake._values.update_calls[0]
        self.assertEqual(tab, "RESULTS")
        self.assertEqual(values, [list(hl.RESULT_EVENT_COLUMNS)])

    def test_canonical_columns_come_from_the_existing_result_event_schema(self):
        # Never a second, invented schema -- exact field order from
        # schemas/outreach_result_event.schema.json's own properties.
        self.assertEqual(
            hl.RESULT_EVENT_COLUMNS,
            ("lead_id", "event_type", "event_at", "gmail_message_id", "gmail_thread_id", "reason", "note", "source"),
        )

    def test_configurable_results_tab_is_used(self):
        backend = _sheets_backend(results_tab="MY_CUSTOM_RESULTS")
        fake = _FakeSheetsService({})
        with patch.object(hb.GoogleSheetsBackend, "_client", return_value=(fake, "sid")):
            backend.import_results()
        tab, rng, values = fake._values.update_calls[0]
        self.assertEqual(tab, "MY_CUSTOM_RESULTS")

    def test_backward_compatible_default_when_results_tab_key_absent(self):
        backend = _sheets_backend(results_tab="__unset__")
        self.assertNotIn("results_tab", backend.cfg)  # simulates a config saved before this key existed
        fake = _FakeSheetsService({})
        with patch.object(hb.GoogleSheetsBackend, "_client", return_value=(fake, "sid")):
            backend.import_results()
        tab, rng, values = fake._values.update_calls[0]
        self.assertEqual(tab, "RESULTS")

    def test_existing_populated_results_tab_is_never_overwritten(self):
        existing_rows = [
            list(hl.RESULT_EVENT_COLUMNS),
            ["real-lead-1", "GMAIL_SENT", "2026-01-01T00:00:00+00:00", "MID", "TID", "", "", "chatgpt"],
        ]
        backend = _sheets_backend()
        fake = _FakeSheetsService({"RESULTS": [list(r) for r in existing_rows]})
        with patch.object(hb.GoogleSheetsBackend, "_client", return_value=(fake, "sid")):
            events = backend.import_results()
        self.assertEqual(fake._values.update_calls, [])  # never touched -- nothing overwritten
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["lead_id"], "real-lead-1")

    def test_malformed_truncated_sheet_row_produces_a_partial_dict_not_a_crash(self):
        """A real Google Sheets API response omits trailing empty cells --
        this must not raise here; downstream validate_event() is what
        rejects it."""
        rows = [list(hl.RESULT_EVENT_COLUMNS), ["only-lead-id"]]  # truncated: only 1 of 8 columns present
        backend = _sheets_backend()
        fake = _FakeSheetsService({"RESULTS": rows})
        with patch.object(hb.GoogleSheetsBackend, "_client", return_value=(fake, "sid")):
            events = backend.import_results()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].get("lead_id"), "only-lead-id")
        self.assertNotIn("event_type", events[0])  # dropped by truncation -- exactly the case validate_event must catch


# ---------------------------------------------------------------------------
# V3.6.1 -- handoff_lib.validate_event (pure)
# ---------------------------------------------------------------------------
class TestValidateEventPure(unittest.TestCase):
    def test_valid_event_passes(self):
        ok, reason = hl.validate_event({"lead_id": "p1", "event_type": "GMAIL_SENT", "event_at": now_iso()})
        self.assertTrue(ok, reason)

    def test_missing_lead_id_rejected(self):
        ok, reason = hl.validate_event({"event_type": "GMAIL_SENT", "event_at": now_iso()})
        self.assertFalse(ok)
        self.assertIn("lead_id", reason)

    def test_missing_event_type_rejected(self):
        ok, reason = hl.validate_event({"lead_id": "p1", "event_at": now_iso()})
        self.assertFalse(ok)
        self.assertIn("event_type", reason)

    def test_unrecognized_event_type_rejected(self):
        ok, reason = hl.validate_event({"lead_id": "p1", "event_type": "NOT_A_REAL_EVENT_TYPE"})
        self.assertFalse(ok)

    def test_non_dict_row_rejected(self):
        ok, reason = hl.validate_event(["not", "a", "dict"])
        self.assertFalse(ok)

    def test_extra_unknown_field_does_not_fail_validation(self):
        ok, reason = hl.validate_event({"lead_id": "p1", "event_type": "GMAIL_SENT", "event_at": now_iso(),
                                          "some_future_field_not_yet_known": "value"})
        self.assertTrue(ok, reason)

    def test_event_dedup_key_never_raises_on_malformed_event(self):
        self.assertEqual(hl.event_dedup_key({}), (None, None, None))
        self.assertEqual(hl.event_dedup_key({"lead_id": "p1"}), ("p1", None, None))


# ---------------------------------------------------------------------------
# V3.6.1 -- per-event failure isolation through the real
# import_outreach_results.import_results() orchestration.
# ---------------------------------------------------------------------------
class TestMalformedEventIsolation(IsolatedHandoffMixin, unittest.TestCase):
    def _seed_row(self, pid):
        backend = hb.LocalFileBackend(self.cfg)
        row = hl.merge_row(None, hl.build_lead_engine_fields(
            make_prospect(pid), make_ready_row(pid), make_contact("NAMED_EMAIL", "VERIFIED"), None, None,
            make_dossier(), LIMITS))
        backend.export_ready([row], [])

    def _run_import(self):
        with patch("import_outreach_results.load_yaml", return_value=self.cfg):
            return ior.import_results(logfn=lambda m: None)

    def test_missing_lead_id_isolated_as_failure(self):
        self._seed_row("p1")
        append_jsonl(self.tmp / "outreach" / "outreach_results.jsonl",
                     {"event_type": "GMAIL_SENT", "event_at": now_iso(), "source": "test"})
        result = self._run_import()
        self.assertEqual(result["events_applied"], 0)
        self.assertEqual(result["events_rejected_invalid"], 1)
        self.assertEqual(len(result["import_failures"]), 1)
        self.assertIn("lead_id", result["import_failures"][0]["error"])

    def test_missing_event_type_isolated_as_failure(self):
        self._seed_row("p1")
        append_jsonl(self.tmp / "outreach" / "outreach_results.jsonl",
                     {"lead_id": "p1", "event_at": now_iso(), "source": "test"})
        result = self._run_import()
        self.assertEqual(result["events_applied"], 0)
        self.assertEqual(result["events_rejected_invalid"], 1)
        self.assertIn("event_type", result["import_failures"][0]["error"])

    def test_malformed_truncated_row_isolated_as_failure(self):
        self._seed_row("p1")
        append_jsonl(self.tmp / "outreach" / "outreach_results.jsonl", {})  # nothing usable at all
        result = self._run_import()
        self.assertEqual(result["events_applied"], 0)
        self.assertEqual(result["events_rejected_invalid"], 1)

    def test_malformed_event_never_blocks_a_following_valid_event(self):
        self._seed_row("p1")
        events_path = self.tmp / "outreach" / "outreach_results.jsonl"
        append_jsonl(events_path, {"event_type": "GMAIL_SENT"})  # malformed: no lead_id
        append_jsonl(events_path, {"lead_id": "p1", "event_type": "GMAIL_SENT", "event_at": now_iso(),
                                     "gmail_message_id": "MID1", "gmail_thread_id": "TID1", "source": "test"})
        result = self._run_import()
        self.assertEqual(result["events_rejected_invalid"], 1)
        self.assertEqual(result["events_applied"], 1)
        row = hb.LocalFileBackend(self.cfg).all_rows()["p1"]
        self.assertEqual(row["gmail_state"], "GMAIL_SENT")

    def test_multiple_malformed_mixed_with_multiple_valid_events(self):
        self._seed_row("p1")
        self._seed_row("p2")
        events_path = self.tmp / "outreach" / "outreach_results.jsonl"
        append_jsonl(events_path, {"event_type": "GMAIL_SENT"})                                    # malformed: no lead_id
        append_jsonl(events_path, {"lead_id": "p1"})                                                # malformed: no event_type
        append_jsonl(events_path, {"lead_id": "p1", "event_type": "GMAIL_SENT", "event_at": now_iso(),
                                     "gmail_message_id": "M1", "gmail_thread_id": "T1", "source": "test"})  # valid
        append_jsonl(events_path, {"lead_id": "ghost-lead", "event_type": "TOTALLY_BOGUS", "event_at": now_iso()})  # malformed: unrecognized type
        append_jsonl(events_path, {"lead_id": "p2", "event_type": "NO_BOUNCE_DETECTED", "event_at": now_iso(),
                                     "source": "test"})                                               # valid
        result = self._run_import()
        self.assertEqual(result["events_rejected_invalid"], 3)
        self.assertEqual(result["events_applied"], 2)
        rows = hb.LocalFileBackend(self.cfg).all_rows()
        self.assertEqual(rows["p1"]["gmail_state"], "GMAIL_SENT")
        self.assertEqual(rows["p2"]["delivery_state"], "NO_BOUNCE_DETECTED")

    def test_valid_event_applies_through_full_orchestration(self):
        self._seed_row("p1")
        append_jsonl(self.tmp / "outreach" / "outreach_results.jsonl",
                     {"lead_id": "p1", "event_type": "GMAIL_SENT", "event_at": now_iso(),
                      "gmail_message_id": "M1", "gmail_thread_id": "T1", "source": "test"})
        result = self._run_import()
        self.assertEqual(result["events_applied"], 1)
        self.assertEqual(result["events_rejected_invalid"], 0)
        self.assertEqual(len(result["import_failures"]), 0)

    def test_duplicate_valid_event_not_reapplied(self):
        """The exact same event (same Gmail message/thread id) sitting in
        the results file is re-read on every import call (the file is never
        truncated/consumed) -- a second import run must apply it 0 times."""
        self._seed_row("p1")
        events_path = self.tmp / "outreach" / "outreach_results.jsonl"
        event = {"lead_id": "p1", "event_type": "GMAIL_SENT", "event_at": now_iso(),
                 "gmail_message_id": "M1", "gmail_thread_id": "T1", "source": "test"}
        append_jsonl(events_path, event)
        first = self._run_import()
        self.assertEqual(first["events_applied"], 1)
        second = self._run_import()
        self.assertEqual(second["events_applied"], 0)
        self.assertEqual(second["events_skipped_duplicate"], 1)

    def test_stale_event_skipped_not_treated_as_failure(self):
        self._seed_row("p1")
        events_path = self.tmp / "outreach" / "outreach_results.jsonl"
        append_jsonl(events_path, {"lead_id": "p1", "event_type": "GMAIL_SENT", "event_at": "2026-02-01T00:00:00+00:00",
                                     "gmail_message_id": "M1", "gmail_thread_id": "T1", "source": "test"})
        self._run_import()
        append_jsonl(events_path, {"lead_id": "p1", "event_type": "SEND_FAILED", "event_at": "2026-01-01T00:00:00+00:00",
                                     "source": "test"})  # older than the already-applied GMAIL_SENT
        result = self._run_import()
        self.assertEqual(result["events_applied"], 0)
        self.assertEqual(result["events_skipped_stale"], 1)
        self.assertEqual(len(result["import_failures"]), 0)  # stale is not a failure
        row = hb.LocalFileBackend(self.cfg).all_rows()["p1"]
        self.assertEqual(row["gmail_state"], "GMAIL_SENT")  # never regressed by the stale SEND_FAILED


if __name__ == "__main__":
    unittest.main()
