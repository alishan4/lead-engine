# Automation

How the Tuesday–Friday scheduled run works, what it does and doesn't do,
and how to operate it. Read `OPERATING-RULES.md` first — this document
assumes its ownership boundaries.

## GitHub repository's role

`https://github.com/alishan4/lead-engine` (private) is the durable, shared
source for the pipeline's *code*: scripts, config, schemas, tests,
documentation, and synthetic fixtures. It intentionally does **not** carry
real prospect/outreach data — see `.gitignore` and `CLAUDE.md`. Pulling a
fresh clone gives you a working pipeline with an empty `data/` tree
(directories preserved via `.gitkeep`); real data accumulates locally as
you run it.

## Fedora scheduler (systemd --user)

Reference copies of the installed unit files live in `systemd/` in this
repo. They hardcode the absolute path of the machine they were generated
on (`/home/user/Development/experiments/lead-engine`) — on a new machine/
user, copy them to `~/.config/systemd/user/`, fix `WorkingDirectory`/
`ExecStart` to the new checkout's absolute path, then:

```
systemctl --user daemon-reload
scripts/run_daily.sh --dry-run          # validate first, every time
loginctl enable-linger "$USER"          # so it still fires while logged out
systemctl --user enable --now lead-engine-daily.timer
```

Two user units, installed at `~/.config/systemd/user/`:

- `lead-engine-daily.service` — a oneshot unit that runs
  `scripts/run_daily.sh` with `WorkingDirectory` set to the repo root.
- `lead-engine-daily.timer` — fires the service Tuesday through Friday at
  12:00 PM.

**Timezone handling.** The timer's `OnCalendar` value is
`Tue,Wed,Thu,Fri 12:00:00 Asia/Karachi` — the timezone is written directly
into the calendar expression, not inherited from the machine's configured
local timezone or `$TZ`. This means the schedule is correct regardless of
what timezone the host itself is set to, and won't silently drift if the
machine's timezone is ever changed. Verify this at any time with:

```
systemd-analyze calendar --iterations=5 'Tue,Wed,Thu,Fri 12:00:00 Asia/Karachi'
```

which prints each upcoming firing in both PKT and UTC so you can confirm
by hand. As installed, this always resolves to 07:00 UTC.

**Runs even when logged out.** User-level systemd units normally only run
while the user has an active login session. `loginctl enable-linger youruser`
was run so the user's systemd instance (and this timer) keeps running
after logout/across reboots. Check with `loginctl show-user youruser -p Linger`
(should read `Linger=yes`); if it ever shows `Linger=no`, the timer will
silently stop firing while logged out, so re-run `loginctl enable-linger
youruser` (no sudo required, as demonstrated during setup).

**Single-run lock.** `scripts/run_daily.sh` takes an exclusive `flock` on
`data/runtime/run.lock` before doing anything. A second invocation
(manual, or an overlapping timer misfire) exits immediately with a clear
message instead of running concurrently against the same data files.

## Lead Engine daily run — what it actually automates

`scripts/run_daily.py` (invoked via `run_daily.sh`) automates exactly the
stages that are genuinely deterministic:

1. Verify workspace (required files present, config loads).
2. Confirm `OPERATING-RULES.md`/`CLAUDE.md` are present (the permanent
   rules this run is bound by).
3. Bulk FIT/GAP qualification routing (`qualify_leads.py --v3`) over
   already-collected evidence.
4. Per-lead: zero-agent deterministic intelligence scan
   (`run_deterministic_scan.py`, real HTTP fetches, no LLM call) →
   dossier build → asset staging, for whichever leads are eligible.
5. Per-lead: email generation → QA → send-window planning, for whichever
   leads already have a saved `contact_record.json` from a prior research
   pass.
6. Export the `READY_TO_SEND` handoff (`export_ready_to_send.py`).
7. Regenerate the reporting exports (`report_pipeline.py`,
   `triage_report.py`).
8. Write a structured run summary to
   `data/runtime/daily_runs/YYYY-MM-DD.json` (gitignored — this is real
   operational data, not code).

**What it deliberately does NOT automate**, because no deterministic
method exists for it in this codebase — these all require a real web
research pass or human/Claude judgment, and a cron job never fabricates
one:

- New-market/business discovery (no discovery script exists at all).
- First-time business-identity verification (`verify_business.py`).
- Buying-signal evidence collection (`assess_buying_signals.py`).
- Franchise-status research once the blocklist flags a match
  (`check_franchise.py`).
- Contact-identity (re-)verification (`contact_identity.py`).
- Specialist-agent escalation when the deterministic scan alone doesn't
  yield a defensible wedge (`route_to_specialist.py`) — this would require
  invoking a real claude-seo agent unattended, which this job never does.

Leads blocked on any of these are counted in the run summary's
`limitations` array, never silently skipped or guessed past.

**What it never does, under any circumstance:** call
`send_executor.py`, `delivery_reconciliation.py`, `follow_up.py`, or
`reply_handling.py`. All four sit downstream of a real Gmail send, which
this repository does not perform (see the Gmail boundary below).

## READY_TO_SEND handoff

`data/outreach/ready_to_send.jsonl` (gitignored — real operational data)
is the single file the Gmail-side automation needs. Each line is one
lead, fully self-contained: business/recipient info, the exact email
subject/body, the wedge summary, FIT/GAP snapshot, and the planned send
window — enough to act on without redoing any SEO/intelligence analysis.
It never contains credentials. Format reference:
`data/fixtures/example_ready_to_send.jsonl`.

## ChatGPT / Gmail boundary

Per `OPERATING-RULES.md` §1: everything after `READY_TO_SEND` — real
Gmail reconciliation, duplicate/prior-email detection, the actual send,
delivery/bounce detection, reply detection, suppression reconciliation
against real outcomes, and follow-up execution — belongs to the
ChatGPT/Gmail side of the system, operating on
`data/outreach/ready_to_send.jsonl`. Lead Engine holds no Gmail
credentials and has no code path that calls a mail API (verified: no
`smtplib`/Gmail API import anywhere in `scripts/`).

`scripts/send_executor.py`, `delivery_reconciliation.py`, `follow_up.py`,
and `reply_handling.py` exist in this repo (built and tested in V3.3) as
manually-invoked, dry-run-only tools for anyone who wants to model the
post-send lifecycle locally — they are not part of the automated
Tuesday–Friday chain and never touch a real transport.

## Monthly tracker

A human-readable reporting mirror only — it reports what Lead Engine and
Gmail each separately recorded, and is never authoritative over either.
See `OPERATING-RULES.md` §2 for the exact non-equivalences this implies
(`READY_TO_SEND != GMAIL_SENT`, `NO_BOUNCE_DETECTED != DELIVERED`).

## Failure recovery

- A single lead's failure is caught, logged into the run summary's
  `failures` array (with `prospect_id`, `stage`, `reason`), and never
  stops the rest of the batch.
- The run only exits non-zero on an infrastructure-level failure
  (workspace verification failed, lock contention) — never merely because
  some leads are blocked on a research stage.
- Nothing in this pipeline deletes prospect history. Status transitions
  are in-place field updates (`_lib.set_status_everywhere`); moving a
  record between qualification-outcome files is the only "move," and it's
  additive, never destructive.
- Logs: `data/runtime/logs/<run_id>.log` (one per run) plus
  `journalctl --user -u lead-engine-daily.service` for the systemd-level
  view (start/stop/exit code).

## Manual execution

```
scripts/run_daily.sh              # a real production run, right now
scripts/run_daily.sh --dry-run    # validation run; writes to a DRY-RUN- prefixed summary
```

## Timer status

```
systemctl --user status lead-engine-daily.timer
systemctl --user list-timers lead-engine-daily.timer
journalctl --user -u lead-engine-daily.service -n 50
```

## Disable the timer

```
systemctl --user disable --now lead-engine-daily.timer
```

This stops future scheduled runs immediately; manual `scripts/run_daily.sh`
invocations still work.

## Re-enable the timer

```
systemctl --user enable --now lead-engine-daily.timer
```

Re-validate with `scripts/run_daily.sh --dry-run` first if the pipeline
code changed since it was last disabled.
