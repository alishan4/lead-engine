"""
V3.5 unattended Claude acquisition worker tests. Every Claude invocation is
mocked (claude_invoke.run_claude / acquisition_worker.run_claude) -- no live
`claude -p` call is ever made by this suite, per the operating spec's
explicit "unit tests must use mocks/fixtures" requirement. subprocess.run is
mocked wherever a real script would otherwise be shelled out to.
"""
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))

import claude_invoke  # noqa: E402
import claude_preflight  # noqa: E402
import acquisition_worker as aw  # noqa: E402
import discover_prospects  # noqa: E402
import catchup  # noqa: E402


def fake_completed_process(returncode=0, stdout="", stderr=""):
    p = MagicMock()
    p.returncode = returncode
    p.stdout = stdout
    p.stderr = stderr
    return p


# ---------------------------------------------------------------------------
# claude_invoke.py -- the invocation seam itself
# ---------------------------------------------------------------------------
class TestClaudeInvoke(unittest.TestCase):
    def _envelope(self, result, is_error=False, structured=None):
        env = {"result": result, "is_error": is_error, "subtype": "success" if not is_error else "error_during_execution"}
        if structured is not None:
            env["structured_output"] = structured
        return json.dumps(env)

    def test_invocation_uses_restricted_and_tool_allowlist(self):
        """The structural safety model: every call must carry --restricted
        and an explicit Read/WebSearch/WebFetch-only allowlist -- never
        Bash, Write, Edit, or any Gmail/browser-automation tool."""
        with patch("claude_invoke.subprocess.run") as m:
            m.return_value = fake_completed_process(stdout=self._envelope("AUTH_OK"))
            claude_invoke.run_claude("hello", json_schema=None, timeout_s=10, max_budget_usd=0.1)
            cmd = m.call_args.args[0]
            self.assertIn("--restricted", cmd)
            idx = cmd.index("--allowedTools")
            tools = cmd[idx + 1]
            for banned in ("Bash", "Write", "Edit", "NotebookEdit", "PowerShell"):
                self.assertNotIn(banned, tools)
            self.assertIn("Read", tools)

    def test_structured_output_returned_directly(self):
        with patch("claude_invoke.subprocess.run") as m:
            m.return_value = fake_completed_process(
                stdout=self._envelope('{"a": 1}', structured={"a": 1})
            )
            result = claude_invoke.run_claude("q", json_schema={"type": "object"}, timeout_s=10, max_budget_usd=0.1)
            self.assertEqual(result, {"a": 1})

    def test_falls_back_to_parsing_result_when_no_structured_output(self):
        with patch("claude_invoke.subprocess.run") as m:
            m.return_value = fake_completed_process(stdout=self._envelope('{"a": 2}'))
            result = claude_invoke.run_claude("q", json_schema={"type": "object"}, timeout_s=10, max_budget_usd=0.1)
            self.assertEqual(result, {"a": 2})

    def test_timeout_raises_claude_timeout(self):
        with patch("claude_invoke.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=5)):
            with self.assertRaises(claude_invoke.ClaudeTimeout):
                claude_invoke.run_claude("q", timeout_s=5, max_budget_usd=0.1)

    def test_nonzero_exit_raises_invocation_error(self):
        with patch("claude_invoke.subprocess.run") as m:
            m.return_value = fake_completed_process(returncode=1, stderr="boom")
            with self.assertRaises(claude_invoke.ClaudeInvocationError):
                claude_invoke.run_claude("q", timeout_s=5, max_budget_usd=0.1)

    def test_auth_error_raises_claude_auth_required(self):
        with patch("claude_invoke.subprocess.run") as m:
            m.return_value = fake_completed_process(returncode=1, stderr="Error: not authenticated, please login")
            with self.assertRaises(claude_invoke.ClaudeAuthRequired):
                claude_invoke.run_claude("q", timeout_s=5, max_budget_usd=0.1)

    def test_malformed_json_raises_invocation_error_never_fabricates(self):
        with patch("claude_invoke.subprocess.run") as m:
            m.return_value = fake_completed_process(stdout="not json at all")
            with self.assertRaises(claude_invoke.ClaudeInvocationError):
                claude_invoke.run_claude("q", timeout_s=5, max_budget_usd=0.1)

    def test_preflight_requires_auth_ok_substring(self):
        with patch("claude_invoke.run_claude", return_value="something else"):
            with self.assertRaises(claude_invoke.ClaudeAuthRequired):
                claude_invoke.preflight()

    def test_preflight_success(self):
        with patch("claude_invoke.run_claude", return_value="AUTH_OK"):
            self.assertTrue(claude_invoke.preflight())


class TestClaudePreflightModule(unittest.TestCase):
    def test_auth_failure_reported_and_fails_closed(self):
        with patch("claude_preflight.preflight", side_effect=claude_invoke.ClaudeAuthRequired("nope")):
            ok, status, detail = claude_preflight.check()
            self.assertFalse(ok)
            self.assertEqual(status, "CLAUDE_AUTH_REQUIRED")

    def test_timeout_also_fails_closed_as_auth_required(self):
        with patch("claude_preflight.preflight", side_effect=claude_invoke.ClaudeTimeout("slow")):
            ok, status, detail = claude_preflight.check()
            self.assertFalse(ok)
            self.assertEqual(status, "CLAUDE_AUTH_REQUIRED")

    def test_success(self):
        with patch("claude_preflight.preflight", return_value=True):
            ok, status, detail = claude_preflight.check()
            self.assertTrue(ok)
            self.assertEqual(status, "AUTH_OK")


# ---------------------------------------------------------------------------
# discover_prospects.py -- pure filtering logic
# ---------------------------------------------------------------------------
class TestDiscoveryFiltering(unittest.TestCase):
    def test_empty_candidates_is_valid(self):
        kept, dropped = discover_prospects.filter_candidates([], set())
        self.assertEqual(kept, [])
        self.assertEqual(dropped, [])

    def test_drops_non_independent_chain(self):
        c = {"business_name": "Big Chain HVAC", "website": "https://bigchain.com",
             "independently_owned": False, "commercial_value_signal": "high",
             "google_dependency_evidence": "local pack presence"}
        kept, dropped = discover_prospects.filter_candidates([c], set())
        self.assertEqual(kept, [])
        self.assertEqual(len(dropped), 1)
        self.assertIn("non-independently-operated", dropped[0][1])

    def test_drops_no_commercial_value(self):
        c = {"business_name": "X", "website": "https://x.com", "independently_owned": True,
             "commercial_value_signal": "none", "google_dependency_evidence": "evidence"}
        kept, dropped = discover_prospects.filter_candidates([c], set())
        self.assertEqual(kept, [])

    def test_drops_missing_google_dependency_evidence(self):
        c = {"business_name": "X", "website": "https://x.com", "independently_owned": True,
             "commercial_value_signal": "high", "google_dependency_evidence": ""}
        kept, dropped = discover_prospects.filter_candidates([c], set())
        self.assertEqual(kept, [])

    def test_drops_duplicate_by_name(self):
        known = {("name", "acme hvac")}
        c = {"business_name": "Acme HVAC", "website": "https://acme.com", "independently_owned": True,
             "commercial_value_signal": "high", "google_dependency_evidence": "evidence"}
        kept, dropped = discover_prospects.filter_candidates([c], known)
        self.assertEqual(kept, [])

    def test_drops_duplicate_by_domain(self):
        known = {("domain", "acme.com")}
        c = {"business_name": "Different Name LLC", "website": "https://www.acme.com", "independently_owned": True,
             "commercial_value_signal": "high", "google_dependency_evidence": "evidence"}
        kept, dropped = discover_prospects.filter_candidates([c], known)
        self.assertEqual(kept, [])

    def test_keeps_legitimate_new_candidate(self):
        c = {"business_name": "Fresh Roofing Co", "website": "https://freshroofing.com",
             "independently_owned": True, "commercial_value_signal": "high",
             "google_dependency_evidence": "ranks in local pack for 'roof repair columbus'"}
        kept, dropped = discover_prospects.filter_candidates([c], set())
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped, [])

    def test_null_independently_owned_is_not_auto_dropped(self):
        """independently_owned: null (uncertain) is allowed through -- the
        downstream franchise-check stage, not discovery, makes the final
        call. Only an explicit False is excluded at discovery time."""
        c = {"business_name": "Maybe Franchise LLC", "website": "https://maybe.com",
             "independently_owned": None, "commercial_value_signal": "medium",
             "google_dependency_evidence": "evidence"}
        kept, dropped = discover_prospects.filter_candidates([c], set())
        self.assertEqual(len(kept), 1)

    def test_to_prospect_record_never_fabricates_missing_fields(self):
        c = {"business_name": "Fresh Roofing Co", "website": "https://freshroofing.com", "city": "Columbus", "state": "OH"}
        record = discover_prospects.to_prospect_record(c, "roofing", "roofing / Columbus, OH")
        self.assertIsNone(record["rating"])
        self.assertIsNone(record["review_count"])
        self.assertIsNone(record["maps_position"])
        self.assertIsNone(record["organic_position"])
        self.assertEqual(record["status"], "DISCOVERED")


# ---------------------------------------------------------------------------
# acquisition_worker.py -- orchestration, isolated with a throwaway data dir
# ---------------------------------------------------------------------------
class IsolatedWorkerMixin:
    def setUp(self):
        self.tmp = Path("/tmp") / f"v3_5_test_{id(self)}"
        (self.tmp / "prospects").mkdir(parents=True, exist_ok=True)
        (self.tmp / "runtime").mkdir(parents=True, exist_ok=True)
        (self.tmp / "markets").mkdir(parents=True, exist_ok=True)
        (self.tmp / "leads").mkdir(parents=True, exist_ok=True)
        self._orig = {
            "PROSPECTS": aw.PROSPECTS, "DATA": aw.DATA, "LOCK_PATH": aw.LOCK_PATH,
        }
        aw.DATA = self.tmp
        aw.PROSPECTS = self.tmp / "prospects"
        aw.LOCK_PATH = self.tmp / "runtime" / "acquisition.lock"

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(aw, k, v)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def seed_discovered(self, records):
        with open(self.tmp / "prospects" / "discovered.jsonl", "w") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

    def seed_qualified(self, records):
        with open(self.tmp / "prospects" / "qualified.jsonl", "w") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")


def prospect(pid, status, **extra):
    p = {"id": pid, "business_name": pid, "status": status, "niche": "hvac", "city": "X", "state": "Y"}
    p.update(extra)
    return p


class TestSingleRunLock(IsolatedWorkerMixin, unittest.TestCase):
    def test_second_lock_fails_while_first_held(self):
        fh1 = aw.acquire_lock()
        self.assertIsNotNone(fh1)
        fh2 = aw.acquire_lock()
        self.assertIsNone(fh2)
        import fcntl
        fcntl.flock(fh1, fcntl.LOCK_UN)
        fh1.close()

    def test_lock_released_allows_next_run(self):
        fh1 = aw.acquire_lock()
        import fcntl
        fcntl.flock(fh1, fcntl.LOCK_UN)
        fh1.close()
        fh2 = aw.acquire_lock()
        self.assertIsNotNone(fh2)
        fcntl.flock(fh2, fcntl.LOCK_UN)
        fh2.close()

    def test_run_reports_run_already_active_and_touches_nothing(self):
        self.seed_discovered([prospect("p1", "DISCOVERED")])
        held = aw.acquire_lock()  # simulate a concurrent worker
        try:
            stats = aw.run(log=lambda m: None)
            self.assertTrue(stats["run_already_active"])
            self.assertFalse(stats["acquisition_run_completed"])
            # state untouched
            recs = json.loads((self.tmp / "prospects" / "discovered.jsonl").read_text().strip())
            self.assertEqual(recs["status"], "DISCOVERED")
        finally:
            import fcntl
            fcntl.flock(held, fcntl.LOCK_UN)
            held.close()


class TestAuthPreflightFailsClosed(IsolatedWorkerMixin, unittest.TestCase):
    def test_auth_required_stops_before_any_research(self):
        self.seed_discovered([prospect("p1", "DISCOVERED")])
        with patch("acquisition_worker.claude_preflight.check",
                   return_value=(False, "CLAUDE_AUTH_REQUIRED", "no valid session")):
            with patch("acquisition_worker.run_claude") as m:
                stats = aw.run(log=lambda m_: None)
                m.assert_not_called()
        self.assertEqual(stats["claude_auth_status"], "CLAUDE_AUTH_REQUIRED")
        self.assertFalse(stats["acquisition_run_completed"])
        recs = json.loads((self.tmp / "prospects" / "discovered.jsonl").read_text().strip())
        self.assertEqual(recs["status"], "DISCOVERED")  # nothing advanced


class TestPerLeadFailureIsolation(IsolatedWorkerMixin, unittest.TestCase):
    def test_one_lead_failure_does_not_stop_the_batch(self):
        ctx = aw.WorkerContext(
            {"max_claude_call_seconds_research": 10, "max_claude_call_seconds_short": 10, "max_budget_usd_per_call": 0.1},
            {}, {}, aw.Deadline(60), lambda m: None, None, False,
        )
        calls = []

        def fake_verify(ctx_, pid):
            calls.append(pid)
            if pid == "bad-lead":
                raise claude_invoke.ClaudeInvocationError("simulated research failure")
            # good-lead "succeeds" -- advance it to a stage-A stop status so
            # the loop exits naturally instead of hitting the defensive
            # max-steps cap (the mock doesn't run the real script, so it
            # must simulate the status transition a real success would make).
            all_recs = [json.loads(l) for l in (self.tmp / "prospects" / "discovered.jsonl").read_text().splitlines()]
            for r in all_recs:
                if r["id"] == pid:
                    r["status"] = "REJECTED"
            with open(self.tmp / "prospects" / "discovered.jsonl", "w") as f:
                for r in all_recs:
                    f.write(json.dumps(r) + "\n")

        self.seed_discovered([prospect("bad-lead", "DISCOVERED"), prospect("good-lead", "DISCOVERED")])
        with patch("acquisition_worker.verify_business_stage", side_effect=fake_verify):
            aw.advance_stage_a(ctx, "bad-lead")
            aw.advance_stage_a(ctx, "good-lead")
        self.assertEqual(calls, ["bad-lead", "good-lead"])
        self.assertEqual(len(ctx.failures), 1)
        self.assertEqual(ctx.failures[0]["prospect_id"], "bad-lead")


class TestWorkerTimeout(IsolatedWorkerMixin, unittest.TestCase):
    def test_already_expired_deadline_stops_before_any_call(self):
        ctx = aw.WorkerContext({}, {}, {}, aw.Deadline(-1), lambda m: None, None, False)
        self.seed_discovered([prospect("p1", "DISCOVERED")])
        with patch("acquisition_worker.verify_business_stage") as m:
            aw.advance_stage_a(ctx, "p1")
            m.assert_not_called()

    def test_run_marks_worker_timeout_true_when_exceeded(self):
        self.seed_discovered([prospect("p1", "DISCOVERED")])
        with patch("acquisition_worker.claude_preflight.check", return_value=(True, "AUTH_OK", "ok")):
            with patch("acquisition_worker.load_yaml") as m_yaml:
                def fake_yaml(name):
                    if name == "acquisition.yaml":
                        return {
                            "max_worker_runtime_seconds": 0, "max_fresh_market_cells_per_run": 0,
                            "max_fresh_candidates_researched_per_run": 0, "outreach_worthy_ceiling": 15,
                            "max_claude_call_seconds_research": 10, "max_claude_call_seconds_short": 10,
                            "max_budget_usd_per_call": 0.1, "discovery_markets": {"niches": [], "cities": []},
                            "catchup_window": {"start": "12:00", "end": "14:00", "tz": "Asia/Karachi", "days": []},
                            "claude_invocation": {"allowed_tools": ["Read"], "restricted": True},
                        }
                    return {}
                m_yaml.side_effect = fake_yaml
                with patch("acquisition_worker.call_plain", return_value=fake_completed_process()):
                    stats = aw.run(log=lambda m: None)
        self.assertTrue(stats["worker_timeout"])
        self.assertTrue(stats["acquisition_run_completed"])  # a timeout is still a completed (partial) cycle, not a crash


class TestPendingLeadPrioritization(IsolatedWorkerMixin, unittest.TestCase):
    def test_pending_leads_processed_before_discovery(self):
        self.seed_discovered([prospect("pending-1", "DISCOVERED")])
        order = []
        with patch("acquisition_worker.claude_preflight.check", return_value=(True, "AUTH_OK", "ok")):
            with patch.object(aw, "process_lead", side_effect=lambda ctx, pid: order.append(("pending", pid))):
                with patch.object(aw, "call_plain", return_value=fake_completed_process()):
                    with patch.object(aw, "discovery_phase", side_effect=lambda ctx: order.append(("discovery", None))):
                        with patch("acquisition_worker.load_yaml", return_value={
                            "max_worker_runtime_seconds": 3600, "max_fresh_market_cells_per_run": 1,
                            "max_fresh_candidates_researched_per_run": 1, "outreach_worthy_ceiling": 15,
                            "max_claude_call_seconds_research": 10, "max_claude_call_seconds_short": 10,
                            "max_budget_usd_per_call": 0.1, "discovery_markets": {"niches": [], "cities": []},
                        }):
                            aw.run(log=lambda m: None)
        self.assertEqual(order[0], ("pending", "pending-1"))
        self.assertEqual(order[-1][0], "discovery")


class TestQualityOverQuota(IsolatedWorkerMixin, unittest.TestCase):
    def test_zero_qualified_leads_is_a_valid_completed_run(self):
        """3 excellent prospects, 0, or 15 are all valid -- the ceiling is
        never forced. A run with nothing to advance and nothing discoverable
        must complete cleanly with qualified/high_priority at 0, not error."""
        self.seed_discovered([])
        with patch("acquisition_worker.claude_preflight.check", return_value=(True, "AUTH_OK", "ok")):
            with patch("acquisition_worker.load_yaml", return_value={
                "max_worker_runtime_seconds": 60, "max_fresh_market_cells_per_run": 0,
                "max_fresh_candidates_researched_per_run": 0, "outreach_worthy_ceiling": 15,
                "max_claude_call_seconds_research": 10, "max_claude_call_seconds_short": 10,
                "max_budget_usd_per_call": 0.1, "discovery_markets": {"niches": [], "cities": []},
            }):
                with patch.object(aw, "call_plain", return_value=fake_completed_process()):
                    stats = aw.run(log=lambda m: None)
        self.assertTrue(stats["acquisition_run_completed"])
        self.assertEqual(stats["qualified"], 0)
        self.assertEqual(stats["high_priority"], 0)

    def test_outreach_capacity_remaining_stops_discovery_at_ceiling(self):
        self.seed_qualified([prospect(f"q{i}", "QUALIFIED") for i in range(15)])
        cfg = {"outreach_worthy_ceiling": 15}
        ctx = aw.WorkerContext(cfg, {}, {}, aw.Deadline(60), lambda m: None, None, False)
        self.assertEqual(aw.outreach_capacity_remaining(ctx), 0)

    def test_capacity_remaining_below_ceiling(self):
        self.seed_qualified([prospect(f"q{i}", "QUALIFIED") for i in range(3)])
        cfg = {"outreach_worthy_ceiling": 15}
        ctx = aw.WorkerContext(cfg, {}, {}, aw.Deadline(60), lambda m: None, None, False)
        self.assertEqual(aw.outreach_capacity_remaining(ctx), 12)


class TestMaxProspectsCap(IsolatedWorkerMixin, unittest.TestCase):
    def test_lead_budget_available_respects_max_prospects(self):
        ctx = aw.WorkerContext({}, {}, {}, aw.Deadline(60), lambda m: None, max_prospects=2, sandbox=True)
        self.assertTrue(ctx.lead_budget_available())
        ctx.leads_touched.update({"a", "b"})
        self.assertFalse(ctx.lead_budget_available())

    def test_unlimited_when_max_prospects_none(self):
        ctx = aw.WorkerContext({}, {}, {}, aw.Deadline(60), lambda m: None, max_prospects=None, sandbox=False)
        ctx.leads_touched.update({"a", "b", "c", "d", "e"})
        self.assertTrue(ctx.lead_budget_available())


class TestSpecialistCallFraming(IsolatedWorkerMixin, unittest.TestCase):
    def test_ask_specialist_never_grants_bash_or_write(self):
        """The specialist stage must use the exact same restricted profile
        as every other stage -- it must never request Bash/Write, even
        though the interactive claude-seo Skill it approximates would
        normally have them."""
        self.seed_discovered([prospect("p1", "AGENT_ROUTED")])
        with patch.object(aw, "call_print", return_value="## ROUTE TO: claude-seo:seo-local\n\nquestion text"):
            with patch.object(aw, "call_save", return_value=fake_completed_process(stdout="p1: OPPORTUNITY_IDENTIFIED (1 agent(s) used)")):
                with patch.object(aw, "claude_research") as m_research:
                    m_research.return_value = {
                        "specialist": "claude-seo:seo-local", "hypothesis": "h", "finding": "f",
                        "commercial_mechanism": "m", "evidence": [], "confidence": 0.8,
                        "recommended_action": "a", "limitations": [], "new_facts": [],
                    }
                    ctx = aw.WorkerContext(
                        {"max_claude_call_seconds_research": 10, "max_budget_usd_per_call": 0.1},
                        {}, {}, aw.Deadline(60), lambda m: None, None, False,
                    )
                    aw.ask_specialist(ctx, "p1")
                    prompt_arg = m_research.call_args.args[1]
                    self.assertIn("no local command execution", prompt_arg)
                    self.assertEqual(ctx.counters["one_agent_escalations"], 1)

    def test_route_to_specialist_hard_cap_surfaces_as_failure_not_garbage(self):
        """A non-zero exit from --print-context (e.g. the 2-call hard cap)
        must raise, not be silently treated as usable prompt text."""
        with patch.object(aw, "call_print", side_effect=RuntimeError("2 specialist calls already used")):
            ctx = aw.WorkerContext({}, {}, {}, aw.Deadline(60), lambda m: None, None, False)
            with self.assertRaises(RuntimeError):
                aw.ask_specialist(ctx, "p1")


# ---------------------------------------------------------------------------
# Absolute safety invariants -- verifiable statically, no mocking needed
# ---------------------------------------------------------------------------
class TestNoGmailCodePath(unittest.TestCase):
    # Checks actual capability surface (imports/API usage), not the word
    # "Gmail" in prose -- every V3.5 module's docstrings deliberately
    # *document* the Gmail boundary in plain English, which is expected and
    # good, not a violation.
    FORBIDDEN_IMPORTS = ("smtplib", "imaplib", "googleapiclient", "google.oauth2", "oauth2client", "google_auth_oauthlib")
    V3_5_FILES = (
        "acquisition_worker.py", "claude_invoke.py", "claude_preflight.py",
        "discover_prospects.py", "catchup.py",
    )

    def test_no_gmail_or_smtp_imports_anywhere_in_v3_5(self):
        for fname in self.V3_5_FILES:
            text = (SCRIPTS / fname).read_text()
            for term in self.FORBIDDEN_IMPORTS:
                self.assertNotIn(term, text, f"{fname} must never reference {term}")

    def test_never_imports_downstream_send_modules(self):
        forbidden_modules = ("send_executor", "delivery_reconciliation", "follow_up", "reply_handling")
        for fname in self.V3_5_FILES:
            text = (SCRIPTS / fname).read_text()
            for mod in forbidden_modules:
                self.assertNotIn(f"import {mod}", text, f"{fname} must never import {mod}")

    def test_claude_invocation_never_requests_bash_or_write_by_default(self):
        cfg_text = (ROOT / "config" / "acquisition.yaml").read_text()
        import yaml
        cfg = yaml.safe_load(cfg_text)
        tools = cfg["claude_invocation"]["allowed_tools"]
        for banned in ("Bash", "Write", "Edit", "NotebookEdit", "PowerShell"):
            self.assertNotIn(banned, tools)
        self.assertTrue(cfg["claude_invocation"]["restricted"])


class TestReadyToSendCompatibility(unittest.TestCase):
    def test_no_automatic_gmail_sent_status_anywhere_in_v3_5(self):
        for fname in ("acquisition_worker.py", "run_daily.py"):
            text = (SCRIPTS / fname).read_text()
            self.assertNotIn('"GMAIL_SENT"', text)
            self.assertNotIn("'GMAIL_SENT'", text)

    def test_run_daily_still_calls_export_ready_to_send(self):
        text = (SCRIPTS / "run_daily.py").read_text()
        self.assertIn("export_ready_to_send.py", text)


class TestDailySummaryFields(unittest.TestCase):
    REQUIRED_FIELDS = (
        "run_id", "trigger_type", "started_at", "completed_at", "claude_auth_status",
        "claude_worker_started", "claude_worker_completed", "acquisition_run_completed",
        "pending_leads_processed", "fresh_candidates_discovered", "businesses_verified",
        "fit_scored", "gap_scored", "buying_signals_verified", "needs_enrichment",
        "rejected", "qualified", "high_priority", "deterministic_wedges",
        "one_agent_escalations", "two_agent_escalations", "assets_staged",
        "contacts_verified", "contact_form_ready", "per_lead_failures", "worker_timeout",
        "limitations",
    )

    def test_run_daily_summary_dict_literal_declares_every_required_field(self):
        text = (SCRIPTS / "run_daily.py").read_text()
        for field in self.REQUIRED_FIELDS:
            self.assertIn(f'"{field}"', text, f"run_daily.py summary is missing required V3.5 field: {field}")

    def test_counters_class_covers_the_per_run_metrics(self):
        for field in ("pending_leads_processed", "fresh_candidates_discovered", "businesses_verified",
                      "fit_scored", "gap_scored", "buying_signals_verified", "needs_enrichment",
                      "rejected", "qualified", "high_priority", "deterministic_wedges",
                      "one_agent_escalations", "two_agent_escalations", "assets_staged",
                      "contacts_verified", "contact_form_ready"):
            self.assertIn(field, aw.Counters.FIELDS)


if __name__ == "__main__":
    unittest.main()
