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

## V3.6 shared handoff bridge

`data/outreach/ready_to_send.jsonl` remains the local, always-available
interface (V3.4, unchanged). V3.6 adds a **shared queue** on top of it so
ChatGPT can consume a clean, flattened view without re-reading raw Lead
Engine artifacts, and so Gmail-side result events can flow back in. This
is purely additive — nothing about `READY_TO_SEND`, QA, or the local
`.jsonl` interface changes.

**Flow** (runs every `run_daily.py` invocation, right after
`export_ready_to_send.py` and before the reporting exports, per
`OPERATING-RULES.md` §1's V3.6 update):

```
export_ready_to_send.py (unchanged)
  -> import_outreach_results.py   (apply any new external result events first)
  -> sync_handoff.py               (build + push the two shared queues)
  -> export_tracker_csv.py         (regenerate the monthly-workbook CSVs)
```

**Two queues, never mixed** (`scripts/handoff_lib.py`): a lead with a
verified named/company email (`preferred_channel` `NAMED_EMAIL`/
`COMPANY_EMAIL`) goes to **EMAIL_READY** — the only queue ChatGPT/Gmail may
eventually act on. A lead whose only channel is a contact form
(`CONTACT_FORM`) goes to **CONTACT_FORM_READY** — human/manual channel
only; never treated as email-sendable by anything in this repository or
implied for the ChatGPT side.

**Idempotent, keyed by `lead_id`** (== `prospect_id`): re-running the sync
updates the same row, never appends a duplicate. Fields split into two
ownership groups — Lead-Engine-owned (business/wedge/asset/contact/email/
timing/qualification, recomputed fresh every sync) and external-owned
(`gmail_state`, `delivery_state`, `reply_state`, `follow_up_state`,
`gmail_message_id`, `gmail_thread_id`, `suppression_reason` — set only by
`scripts/import_outreach_results.py` applying a real event, and preserved
by every Lead Engine sync no matter what). See
`schemas/handoff_row.schema.json` and `scripts/handoff_lib.py:merge_row`/
`apply_event`.

**Local backend (default, zero credentials)**: `config/handoff.yaml:
backend: local` — writes `data/handoff/email_ready.{json,csv}` and
`data/handoff/contact_form_ready.{json,csv}` (gitignored, real data). This
always works; nothing about the shared queue requires Google Sheets to be
configured.

**Google Sheets setup** (optional, `config/handoff.yaml: backend:
google_sheets`):
1. Create (or reuse) a Google Cloud project, enable the **Google Sheets
   API only** (never Drive, never Gmail).
2. Create a service account, download its JSON key to a path **outside
   this repo** (or anywhere already covered by `.gitignore`'s
   `credentials*.json`/`service-account*.json`/`token*.json` patterns —
   never inside a location that could be accidentally `git add -A`'d).
3. Create a private Google Sheet (or reuse one) with two tabs named to
   match `config/handoff.yaml: google_sheets.email_ready_tab`/
   `contact_form_ready_tab` (defaults: `EMAIL_READY`/`CONTACT_FORM_READY`),
   plus a results tab named to match `google_sheets.results_tab` (defaults
   to `RESULTS` if the key is omitted — safe for a config saved before
   V3.6.1) for ChatGPT to write result events into. **You do not need to
   add a header row yourself** — if the tab is completely empty, the first
   `import_outreach_results.py` run writes the canonical header
   (`scripts/handoff_lib.py: RESULT_EVENT_COLUMNS`, taken directly from
   `schemas/outreach_result_event.schema.json`) automatically; a tab that
   already has any content is never touched by this step.
4. Share that Sheet with the service account's `client_email` as **Editor**
   — never publish it with a public link.
5. Set `config/handoff.yaml: google_sheets.service_account_file` (the JSON
   key's path) and `spreadsheet_id` (from the Sheet's URL).
6. `pip install google-api-python-client google-auth` (only needed once
   this backend is actually selected — the local backend and every test
   work with zero extra packages).

Until steps 1–6 are done, selecting `google_sheets` is safe: every sync
call fails closed with `SHARED_HANDOFF_AUTH_REQUIRED`, the local queue
stays up to date and authoritative, and nothing is lost (see
`scripts/handoff_backend.py: SharedHandoffAuthRequired`).

**Result events flow back in** via `data/outreach/outreach_results.jsonl`
(local fallback — `schemas/outreach_result_event.schema.json`) or, for the
Sheets backend, its results tab. `scripts/import_outreach_results.py`
applies each event idempotently — the exact same Gmail message/thread
event is never double-applied, and a stale/out-of-order event can never
regress a field a newer one already set (`scripts/handoff_lib.py:
event_dedup_key`/`apply_event`). **Every event is validated
(`scripts/handoff_lib.py: validate_event`) and processed inside its own
isolated `try`/`except` (V3.6.1)** — a row with a missing `lead_id`,
missing `event_type`, an unrecognized `event_type`, or any other
malformed/truncated shape (Google Sheets silently drops trailing empty
cells, which can shorten a row) is recorded as a failure and never blocks
any other event in the same batch; an unknown *extra* field on an
otherwise-valid event is tolerated, not rejected, so a future field
ChatGPT starts sending doesn't break ingestion. A `SUPPRESSED` event also
registers in the existing V3.3 suppression registry so the rest of the
pipeline
respects it on the next run.

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

V3.6 adds four CSVs (`scripts/export_tracker_csv.py`, regenerated every
run, gitignored under `data/handoff/`) matching a monthly Excel workbook's
expected import shape: `leads_master.csv` (one row per prospect ever
discovered), `outreach_log.csv` (one row per shared-queue lead, both
queues), `follow_up_queue.csv` (shared-queue rows currently
`follow_up_state == FOLLOW_UP_DUE`), `daily_pipeline.csv` (one row per
daily run summary). These are CSV exports for the workbook to import, never
a direct `.xlsx` write — Excel remains a reporting mirror, never the
transactional path.

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
python3 scripts/sync_handoff.py                             # V3.6: rebuild + push the shared queue standalone
python3 scripts/import_outreach_results.py                  # V3.6: apply new external result events standalone
python3 scripts/export_tracker_csv.py                       # V3.6: regenerate the monthly-tracker CSVs standalone
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
