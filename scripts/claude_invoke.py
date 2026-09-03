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


class ClaudeCallError(Exception):
    """V3.8.2 -- common base for every exception this module raises after a
    real subprocess was spawned. Always carries `.meta` (see _empty_meta/
    _extract_meta) -- whatever real cost/usage data could be recovered from
    the failed call's own output, never fabricated. A failure can still be
    billable: the live V3.8.1 validation observed a real $0.5358346 charge
    on a call that exited non-zero after hitting its own --max-budget-usd
    circuit breaker mid-research. Callers (scripts/discovery_worker.py)
    MUST read `.meta` off a caught exception before discarding it -- a
    failed call's real cost must still reach the daily ledger."""

    def __init__(self, message, meta=None):
        super().__init__(message)
        self.meta = meta if meta is not None else _empty_meta()


class ClaudeAuthRequired(ClaudeCallError):
    """Preflight or a real call found Claude auth unavailable/expired. The
    caller must fail closed: record CLAUDE_AUTH_REQUIRED and stop starting
    new work, never invent a result and never treat this as a per-lead
    research failure."""


class ClaudeTimeout(ClaudeCallError):
    """The subprocess exceeded its own timeout. Caller must record this as
    an incomplete unit of work, never as a completed one. `.meta` is
    populated from whatever partial stdout/stderr Python's subprocess
    module captured before killing the process (subprocess.TimeoutExpired's
    own .stdout/.stderr attributes) -- often empty/unparseable for a real
    timeout, but checked defensively rather than assumed absent."""


class ClaudeInvocationError(ClaudeCallError):
    """Any other invocation failure (non-zero exit, malformed envelope,
    budget exceeded, missing structured_output). Caller turns this into a
    per-lead failure -- never a fabricated or guessed result. `.meta` is
    populated from the failed process's own stdout when it parses as a real
    `claude -p --output-format json` envelope (this is the common case for
    a --max-budget-usd trip: the CLI still emits a complete JSON envelope
    reporting the real cost before exiting non-zero)."""


def _cfg():
    return load_yaml("acquisition.yaml")


def _empty_meta():
    """V3.8.1 -- the honest 'nothing observed yet' shape. cost_observable/
    tokens_observable are False until a real envelope field is actually
    seen; scripts/cost_ledger.py must never treat a missing field as $0 or
    0 tokens -- that would silently understate real spend."""
    return {
        "total_cost_usd": None, "cost_observable": False,
        "input_tokens": None, "output_tokens": None, "tokens_observable": False,
        "duration_ms": None,
    }


def _extract_meta(envelope):
    """Pure: pulls whatever real cost/usage fields a `claude -p
    --output-format json` envelope actually reports. Every field defaults
    to None/False on absence -- never a guessed number. See the real
    envelope shape observed in production (data/runtime/logs/*.log,
    2026-09-03): `total_cost_usd`, `usage.input_tokens`, `usage.output_tokens`."""
    meta = _empty_meta()
    cost = envelope.get("total_cost_usd")
    if isinstance(cost, (int, float)):
        meta["total_cost_usd"] = float(cost)
        meta["cost_observable"] = True
    usage = envelope.get("usage") or {}
    in_tok, out_tok = usage.get("input_tokens"), usage.get("output_tokens")
    if isinstance(in_tok, (int, float)) and isinstance(out_tok, (int, float)):
        meta["input_tokens"] = int(in_tok)
        meta["output_tokens"] = int(out_tok)
        meta["tokens_observable"] = True
    duration = envelope.get("duration_ms")
    if isinstance(duration, (int, float)):
        meta["duration_ms"] = duration
    return meta


def _meta_from_text(text):
    """V3.8.2 -- best-effort: tries to parse `text` as a `claude -p
    --output-format json` envelope and extract real cost/usage from it.
    Returns _empty_meta() on ANY failure (not valid JSON, not a dict, no
    recognizable fields) -- this must never raise, since it is only ever
    called from inside an already-failing path and must not mask the real
    error with a new one."""
    if not text:
        return _empty_meta()
    try:
        envelope = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return _empty_meta()
    if not isinstance(envelope, dict):
        return _empty_meta()
    return _extract_meta(envelope)


def _first_observable_meta(*texts):
    """Pure: tries each text source in order, returning the first one that
    actually yields observable cost or token data. _meta_from_text() always
    returns a (truthy) dict even on total failure, so plain `or`-chaining
    would never fall through to a later source -- this checks the
    observable flags explicitly instead. Returns the last (empty) attempt
    if none of the sources yielded anything."""
    result = _empty_meta()
    for text in texts:
        result = _meta_from_text(text)
        if result["cost_observable"] or result["tokens_observable"]:
            return result
    return result


def run_claude(prompt, json_schema=None, timeout_s=None, max_budget_usd=None,
               allowed_tools=None, model=None):
    """
    Runs one non-interactive, memoryless `claude -p` call and returns the
    parsed structured result (a dict, when json_schema is given) or the raw
    text reply (when it isn't -- used only for the trivial auth preflight).

    Raises ClaudeAuthRequired / ClaudeTimeout / ClaudeInvocationError instead
    of ever returning a partial/guessed result.

    This is a thin, behavior-preserving wrapper over run_claude_with_meta()
    (V3.8.1) that discards the cost/usage metadata -- every pre-V3.8.1
    caller (verify_business_stage, buying_signals_stage, etc.) is
    unaffected. Use run_claude_with_meta() directly when the caller needs
    real cost/token observability (scripts/discovery_worker.py's cost
    guard)."""
    result, _meta = run_claude_with_meta(
        prompt, json_schema=json_schema, timeout_s=timeout_s,
        max_budget_usd=max_budget_usd, allowed_tools=allowed_tools, model=model,
    )
    return result


def run_claude_with_meta(prompt, json_schema=None, timeout_s=None, max_budget_usd=None,
                          allowed_tools=None, model=None):
    """
    Identical contract to run_claude(), except it returns (result, meta)
    where `meta` is whatever real cost/usage data (see _extract_meta) the
    `claude -p --output-format json` envelope actually reported for THIS
    call -- never fabricated, never estimated. On any raised exception
    (ClaudeAuthRequired/ClaudeTimeout/ClaudeInvocationError) no meta is
    returned at all -- a failed call has no real usage to report, and the
    caller's own retry/failure-isolation logic handles that path exactly
    as before.
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
        # V3.8.2 -- Python captures whatever stdout/stderr the process had
        # already produced before being killed (subprocess.TimeoutExpired's
        # own attributes); best-effort attempt to recover real cost/usage
        # from it, though a genuine timeout usually means the CLI never got
        # far enough to emit a complete envelope.
        partial_meta = _first_observable_meta(getattr(e, "stdout", None), getattr(e, "stderr", None))
        raise ClaudeTimeout(
            f"claude -p exceeded {timeout_s}s (elapsed {time.monotonic() - started:.1f}s)", meta=partial_meta
        ) from e

    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()[-1:] or (proc.stdout or "").strip().splitlines()[-1:] or ["<no output>"]
        low = (proc.stdout + proc.stderr).lower()
        # V3.8.2 -- a non-zero exit (including hitting --max-budget-usd mid-
        # research) commonly still emits a complete JSON envelope on stdout
        # reporting the REAL cost incurred before the CLI aborted -- see the
        # 2026-09-03 live validation, which observed exactly this shape.
        # Never let this parse attempt mask the real error being raised.
        failure_meta = _first_observable_meta(proc.stdout, proc.stderr)
        if "not authenticated" in low or ("auth" in low and "login" in low):
            raise ClaudeAuthRequired(f"claude -p exited {proc.returncode}: {detail[0]}", meta=failure_meta)
        raise ClaudeInvocationError(f"claude -p exited {proc.returncode}: {detail[0]}", meta=failure_meta)

    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise ClaudeInvocationError(f"claude -p returned non-JSON output: {proc.stdout[:200]!r}") from e

    if envelope.get("subtype") == "error_during_execution" or envelope.get("is_error"):
        msg = envelope.get("result") or envelope.get("error") or "unknown error"
        # The envelope itself parsed fine here -- its own cost/usage fields
        # (if any) are real and available even though the call is being
        # treated as an error.
        error_meta = _extract_meta(envelope)
        if "authent" in str(msg).lower() or "login" in str(msg).lower():
            raise ClaudeAuthRequired(f"claude -p reported an auth error: {msg}", meta=error_meta)
        raise ClaudeInvocationError(f"claude -p reported an error: {msg}", meta=error_meta)

    meta = _extract_meta(envelope)

    if json_schema is None:
        return envelope.get("result", ""), meta

    if "structured_output" in envelope:
        return envelope["structured_output"], meta
    try:
        return json.loads(envelope.get("result", "")), meta
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
        # $0.05 was too tight in production: observed real cost for this
        # exact call ranged $0.024-$0.072 depending on how much extended
        # thinking the model used for reading the policy preamble + two
        # files, causing a real, authenticated call to spuriously abort
        # with error_max_budget_usd and get reported as CLAUDE_AUTH_REQUIRED
        # -- a false trip, not an actual auth problem (caught in production
        # on 2026-09-02's second acquisition pass). $0.25 leaves real margin
        # while still acting as a genuine runaway-cost circuit breaker.
        json_schema=None, timeout_s=45, max_budget_usd=0.25,
        allowed_tools=["Read"],
    )
    if "AUTH_OK" not in (reply or ""):
        raise ClaudeAuthRequired(f"preflight did not return AUTH_OK, got: {reply!r}")
    return True
