#!/usr/bin/env python3
"""
V3.5 -- the single seam every unattended Claude research call in this
pipeline goes through. Every existing research-stage script
(verify_business.py, assess_buying_signals.py, check_contactability.py,
route_to_specialist.py, contact_identity.py, check_franchise.py,
discover_prospects.py) already follows one contract: print a prompt/context,
a researcher (human or Claude) returns structured JSON, `--save -` ingests
it. This module is what replaces "a human pastes into an interactive Claude
session" with a real, non-interactive `claude -p` call, feeding its reply
straight back into the same `--save -` contract, unchanged.

SAFETY MODEL (structural, not just promptable): every call here runs with
`--restricted` plus an explicit tool allowlist of Read/WebSearch/WebFetch
only. `--restricted` (see `claude --help`) unconditionally strips out Bash/
PowerShell/REPL/other code-execution tools and confines file tools to the
working directory; it also refuses `--dangerously-skip-permissions`. This
process therefore has no tool capable of sending email, accessing Gmail,
submitting a contact form, or writing any file anywhere -- the orchestrator
process (never Claude) is the only thing that ever writes state, by piping
this module's structured result into the existing `--save -` CLI of
whichever stage script called it. This invariant is never relaxed for any
stage, including specialist escalation (see acquisition_worker.py's
`ask_specialist` for why that stage does not shell out to the interactive
claude-seo Skill package, which requires Bash/Write).

Every prompt this module sends opens with an explicit instruction to read
CLAUDE.md and OPERATING-RULES.md via the Read tool -- required because each
`-p` call is a deliberately fresh, memoryless session (no --continue/
--resume): "do not depend on conversational memory" per the operating spec.
"""
import json
import subprocess
import time

from _lib import ROOT, load_yaml

POLICY_PREAMBLE = (
    "Before doing anything else, use the Read tool to read CLAUDE.md and "
    "OPERATING-RULES.md in the current directory in full. Follow every rule "
    "in both files for the rest of this task -- in particular: never "
    "fabricate a fact, contact, ranking, or evidence item; missing "
    "information stays null/UNKNOWN, never guessed and never penalized; "
    "cite where each fact came from; you have no tool capable of sending "
    "email, accessing Gmail, or submitting a web form, and must never "
    "attempt to contact the business directly in any way -- your only job "
    "is research and structured reporting.\n\n---\n\n"
)


class ClaudeAuthRequired(Exception):
    """Preflight or a real call found Claude auth unavailable/expired. The
    caller must fail closed: record CLAUDE_AUTH_REQUIRED and stop starting
    new work, never invent a result and never treat this as a per-lead
    research failure."""


class ClaudeTimeout(Exception):
    """The subprocess exceeded its own timeout. Caller must record this as
    an incomplete unit of work, never as a completed one."""


class ClaudeInvocationError(Exception):
    """Any other invocation failure (non-zero exit, malformed envelope,
    budget exceeded, missing structured_output). Caller turns this into a
    per-lead failure -- never a fabricated or guessed result."""


def _cfg():
    return load_yaml("acquisition.yaml")


def run_claude(prompt, json_schema=None, timeout_s=None, max_budget_usd=None,
               allowed_tools=None, model=None):
    """
    Runs one non-interactive, memoryless `claude -p` call and returns the
    parsed structured result (a dict, when json_schema is given) or the raw
    text reply (when it isn't -- used only for the trivial auth preflight).

    Raises ClaudeAuthRequired / ClaudeTimeout / ClaudeInvocationError instead
    of ever returning a partial/guessed result.
    """
    cfg = _cfg()["claude_invocation"]
    allowed_tools = allowed_tools or cfg["allowed_tools"]
    timeout_s = timeout_s or 120

    cmd = [
        "claude", "-p", POLICY_PREAMBLE + prompt,
        "--allowedTools", " ".join(allowed_tools),
        "--permission-mode", cfg.get("permission_mode", "dontAsk"),
        "--output-format", cfg.get("output_format", "json"),
    ]
    if cfg.get("restricted", True):
        cmd.append("--restricted")
    if cfg.get("no_session_persistence", True):
        cmd.append("--no-session-persistence")
    if json_schema is not None:
        cmd += ["--json-schema", json.dumps(json_schema)]
    if max_budget_usd is not None:
        cmd += ["--max-budget-usd", str(max_budget_usd)]
    if model:
        cmd += ["--model", model]

    started = time.monotonic()
    try:
        # stdin explicitly closed -- this runs unattended (systemd, or this
        # worker's own background invocation) with no one to type into it;
        # an inherited-but-unusable stdin has been observed to make the
        # subprocess exit non-zero with no stderr at all.
        proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout_s, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as e:
        raise ClaudeTimeout(f"claude -p exceeded {timeout_s}s (elapsed {time.monotonic() - started:.1f}s)") from e

    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()[-1:] or (proc.stdout or "").strip().splitlines()[-1:] or ["<no output>"]
        low = (proc.stdout + proc.stderr).lower()
        if "not authenticated" in low or ("auth" in low and "login" in low):
            raise ClaudeAuthRequired(f"claude -p exited {proc.returncode}: {detail[0]}")
        raise ClaudeInvocationError(f"claude -p exited {proc.returncode}: {detail[0]}")

    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise ClaudeInvocationError(f"claude -p returned non-JSON output: {proc.stdout[:200]!r}") from e

    if envelope.get("subtype") == "error_during_execution" or envelope.get("is_error"):
        msg = envelope.get("result") or envelope.get("error") or "unknown error"
        if "authent" in str(msg).lower() or "login" in str(msg).lower():
            raise ClaudeAuthRequired(f"claude -p reported an auth error: {msg}")
        raise ClaudeInvocationError(f"claude -p reported an error: {msg}")

    if json_schema is None:
        return envelope.get("result", "")

    if "structured_output" in envelope:
        return envelope["structured_output"]
    try:
        return json.loads(envelope.get("result", ""))
    except json.JSONDecodeError as e:
        raise ClaudeInvocationError(
            f"claude -p reply did not match the requested json_schema: {envelope.get('result', '')[:200]!r}"
        ) from e


def preflight():
    """Trivial auth check -- 'reply only AUTH_OK', short timeout. Returns
    True/raises ClaudeAuthRequired/ClaudeTimeout/ClaudeInvocationError; the
    caller (claude_preflight.py, acquisition_worker.py) turns any exception
    into a fail-closed CLAUDE_AUTH_REQUIRED result."""
    reply = run_claude(
        "Reply only AUTH_OK, nothing else.",
        json_schema=None, timeout_s=45, max_budget_usd=0.05,
        allowed_tools=["Read"],
    )
    if "AUTH_OK" not in (reply or ""):
        raise ClaudeAuthRequired(f"preflight did not return AUTH_OK, got: {reply!r}")
    return True
