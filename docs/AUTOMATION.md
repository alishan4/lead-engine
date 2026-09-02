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

### V3.5 (current): the Claude acquisition worker runs first

`scripts/run_daily.py` now starts with `scripts/acquisition_worker.py`
(unless `--deterministic-only` is passed), which does everything the V3.4
version of this document said no deterministic method existed for:

- Fresh-prospect discovery (`discover_prospects.py`) over a bounded
  SUB-NICHE × CITY rotation (`config/acquisition.yaml: discovery_markets`).
- First-time business-identity verification (`verify_business.py`).
- Franchise-status research once the blocklist flags a match
  (`check_franchise.py`).
- Buying-signal / why-now evidence collection (`assess_buying_signals.py`).
- Contactability pre-checks (`check_contactability.py`).
- Specialist-agent escalation when the deterministic scan alone doesn't
  yield a defensible wedge (`route_to_specialist.py`), capped at 1–2 calls
  per lead exactly as before.
- Contact-identity (re-)verification (`contact_identity.py`).

It does this by invoking non-interactive `claude -p` (via
`scripts/claude_invoke.py`) instead of waiting for a human to paste
research into an interactive session — see `OPERATING-RULES.md` §4's V3.5
update for the full structural safety model (`--restricted`, a Read/
WebSearch/WebFetch-only tool allowlist, a fail-closed auth preflight, and
the ceilings in `config/acquisition.yaml`), and
`reports/V3.5-UNATTENDED-ACQUISITION-REPORT.md` for the design record and
validation results. A lead still blocked on one of these stages after the
worker's own budget/ceiling/timeout is counted in the run summary's
`limitations` array and `per_lead_failures`, never silently skipped or
guessed past — and re-run's own idempotent status gates mean it's simply
picked up again on the next run.

Pass `--deterministic-only` to `run_daily.py`/`run_daily.sh` to skip this
worker entirely and reproduce the exact pre-V3.5 behavior described next.

### Then, unchanged since V2/V3: the deterministic finalization loop

1. Verify workspace (required files present, config loads).
2. Confirm `OPERATING-RULES.md`/`CLAUDE.md` are present (the permanent
   rules this run is bound by).
3. Bulk FIT/GAP qualification routing (`qualify_leads.py --v3`) over
   already-collected evidence.
4. Per-lead: zero-agent deterministic intelligence scan
   (`run_deterministic_scan.py`, real HTTP fetches, no LLM call) →
   dossier build → asset staging, for whichever leads are eligible.
5. Per-lead: email generation → QA → send-window planning, for whichever
   leads already have a saved `contact_record.json` (now typically
   produced earlier in the same run, by the acquisition worker).
6. Export the `READY_TO_SEND` handoff (`export_ready_to_send.py`).
7. Regenerate the reporting exports (`report_pipeline.py`,
   `triage_report.py`).
8. Write a structured run summary to
   `data/runtime/daily_runs/YYYY-MM-DD.json` (Asia/Karachi-local date;
   gitignored — this is real operational data, not code).

**What it never does, under any circumstance, in either mode:** call
`send_executor.py`, `delivery_reconciliation.py`, `follow_up.py`, or
`reply_handling.py`. All four sit downstream of a real Gmail send, which
this repository does not perform (see the Gmail boundary below) — this is
unaffected by the V3.5 change and is enforced both by never being called
and by the acquisition worker's Claude subprocesses having no tool capable
of reaching Gmail or a contact form in the first place.

### Same-day catch-up (V3.5)

The permanent schedule is still exactly Tue–Fri 12:00 PKT, one timer, no
second timer added. `scripts/catchup.py` (pure functions, see its own
docstring) answers "if the acquisition worker is invoked right now, what
should happen?" for a 12:00–14:00 PKT window: `NORMAL_SCHEDULE` at the
timer's own firing time, `SAME_DAY_CATCH_UP` inside the window if today
has no completed acquisition cycle yet, `ALREADY_COMPLETED_TODAY` if one
already ran, `RUN_ALREADY_ACTIVE` if the acquisition lock is held, and
`MISSED_ACQUISITION_WINDOW` past 14:00 with nothing having run.
`scripts/run_claude_acquisition.sh` is the manual/catch-up entrypoint that
consults it. **This does not add automatic polling** — no new timer or
loop watches the clock between 12:00 and 14:00 on its own; recovering a
run that failed mid-window still requires some manual/follow-up invocation
of `run_claude_acquisition.sh` inside the window, since a second production
timer is explicitly out of scope.

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
scripts/run_daily.sh                          # a real production run, right now (includes V3.5 acquisition)
scripts/run_daily.sh --dry-run                # validation run; writes to a DRY-RUN- prefixed summary
scripts/run_daily.sh --deterministic-only     # pre-V3.5 behavior only, no Claude research
scripts/run_claude_acquisition.sh                          # auto-detect trigger_type via catchup.py, real run
scripts/run_claude_acquisition.sh "" --max-prospects 2      # controlled validation run (real research, capped)
python3 scripts/claude_preflight.py                        # standalone Claude-auth sanity check
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
