#!/usr/bin/env python3
"""
V3.5 -- standalone Claude authentication preflight, runnable on its own
(for manual/systemd-level sanity checks) and imported by
acquisition_worker.py before any expensive acquisition work starts. Fails
closed: on any doubt, report CLAUDE_AUTH_REQUIRED and exit non-zero rather
than let the caller guess whether Claude is usable.

Usage:
  python3 scripts/claude_preflight.py
"""
import sys

from claude_invoke import preflight, ClaudeAuthRequired, ClaudeTimeout, ClaudeInvocationError


def check():
    """Returns (ok: bool, status: str, detail: str)."""
    try:
        preflight()
        return True, "AUTH_OK", "claude -p replied AUTH_OK under the restricted acquisition-worker profile"
    except ClaudeAuthRequired as e:
        return False, "CLAUDE_AUTH_REQUIRED", str(e)
    except ClaudeTimeout as e:
        return False, "CLAUDE_AUTH_REQUIRED", f"preflight timed out (treated as auth-unavailable, fail closed): {e}"
    except ClaudeInvocationError as e:
        return False, "CLAUDE_AUTH_REQUIRED", f"preflight invocation failed (fail closed): {e}"


def main():
    ok, status, detail = check()
    print(f"{status}: {detail}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
