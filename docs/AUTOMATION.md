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
repo. `WorkingDirectory`/`ExecStart` in `lead-engine-daily.service` are
placeholders (`/path/to/lead-engine`) — on any machine, copy the unit
files to `~/.config/systemd/user/` and replace that placeholder with your
own checkout's absolute path, then:

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

## V3.7 acquisition-quality updates

Three targeted changes, none of which touch FIT/GAP thresholds or any
V3.6.1 safety rule:

- **Market rotation** (`scripts/acquisition_worker.py: build_market_rotation`/
  `pick_discovery_cells`) is now city-major (interleaves niches) and
  tier-weighted (a smooth weighted round-robin over `config/niches.yaml`'s
  existing `tier` field) instead of a plain niche-major loop -- the old
  version could spend an entire run's discovery budget inside one niche's
  contiguous city block (confirmed on 2026-09-02, when two production
  passes explored almost nothing but `family_law`). Every configured niche
  is still selected over time -- never fully excluded, just weighted.
  `config/acquisition.yaml: discovery_markets.niches` was also broadened
  (added `foundation_repair`, `estate_law`, `moving_relocation`).
- **Cheap prequalification**: `scripts/discover_prospects.py: filter_candidates`
  now also drops a candidate whose OWN discovery-stage
  `commercial_value_signal` is `"low"` (previously only `"none"` was
  dropped) before spending three more expensive Claude calls on it. This
  does not add a review-count/years-in-business/rating threshold --
  empirical analysis of a real production run found niche tier, not
  those fields, was what actually correlated with low FIT (see
  `docs/LEAD-ENGINE.md`).
- **Bounded timeout retry**: `scripts/acquisition_worker.py: claude_research`
  now retries a `ClaudeTimeout` exactly once (`config/acquisition.yaml:
  reliability.max_timeout_retries`) before propagating as a per-lead/
  per-cell failure -- never for any other error type, never more than
  once. `max_claude_call_seconds_research`/`_short` were also bumped
  (300s/120s) after the same production run showed calls landing right at
  the old caps.

Ranking-evidence ingestion and re-evaluation (also V3.7) are documented in
their own section below, after the READY_TO_SEND handoff.

## Discovery-Only Production Mode (V3.8.1) — the current scheduled default

`config/acquisition.yaml: production_mode` decides which flow
`scripts/run_daily.py` runs. **`discovery_only` is the default** and is
what the scheduled timer actually runs day to day. `full_pipeline`
reproduces every V3.5–V3.8 stage described in the rest of this document
unchanged, still fully available for an explicit, deliberate invocation.
`run_daily.py` fails closed (non-zero exit) on any other value.

Why: real production spend hit roughly $100 over two days of building/
running the full pipeline unattended — far more than this stage of
lead-acquisition should cost. The new operating principle is **CLAUDE
DISCOVERS, CLAUDE DOES NOT ANALYZE**: Fedora's job shrinks to discovering
candidates and doing cheap, deterministic verification; ChatGPT + the user
own everything from qualification onward.

**Discovery-only flow** (`scripts/discovery_worker.py`, invoked instead of
`scripts/acquisition_worker.py`):

```
Claude auth preflight (fails closed once, no retry loop)
  -> for each market cell (config/acquisition.yaml: discovery_markets,
     V3.7's tier-weighted rotation, unchanged):
       check cost/call/time governors BEFORE calling Claude
       -> ONE Claude call: discover_prospects.py (unchanged)
       -> deterministic basic verification (candidate_verification.py,
          ZERO additional Claude calls): real business? contact surface
          present? city/state/niche present?
       -> CANDIDATE_VERIFIED or CANDIDATE_REJECTED (new, terminal statuses
          -- never touches QUALIFIED/NEEDS_ENRICHMENT/MANUAL_REVIEW/
          CONTACT_FORM_READY/READY_TO_SEND records)
  -> sync_handoff.py: sync_candidates() -- upserts the CANDIDATES Google
     Sheet by lead_id (idempotent -- rediscovery never duplicates a row)
  -> report_discovery_only.py -- a short report (candidates discovered/
     verified/saved, duplicates skipped, markets explored, cost/budget
     status, and an explicit SKIPPED line for every downstream stage)
  -> STOP
```

Nothing below basic verification runs automatically: no `qualify_leads.py
--v3`, no deterministic intelligence scan, no dossier/asset, no contact
identity, no draft/QA/send-window planning, no `export_ready_to_send.py`,
no ranking enrichment, no specialist agent, no Gmail access, no
contact-form submission.

**Hard cost governors** (`config/discovery_only.yaml`), checked BEFORE
every Claude call:

| Governor | Default | Enforcement |
|---|---|---|
| `daily_claude_budget_usd` | 3.00 | Shared across every invocation that calendar day (scheduled run, same-day catch-up, manual retry) via `scripts/cost_ledger.py`'s durable `data/runtime/cost/<date>.json` ledger (gitignored) |
| `max_claude_calls_per_run` | 8 | Independent of $ observability -- holds even if cost can't be measured |
| `max_worker_runtime_seconds` | 600 | Far smaller than `full_pipeline`'s 2700s |
| `max_market_cells_per_run` | 8 | Bounds research scope independently of the call cap |
| `min_candidates_target` / `max_candidates_target` | 10 / 20 | A **goal**, never a quota -- never manufactured, never a reason to exceed any governor above |

Hitting a governor is reported as `budget_status` (`OK` / `EXHAUSTED` /
`CALL_CAP_REACHED`) and handled by **saving completed work, syncing the
CANDIDATES sheet, writing the report, and stopping** -- never retried,
never a different market cell, never treated as a pipeline failure.

Real cost/token figures come straight from `claude -p`'s own JSON envelope
(`scripts/claude_invoke.py: run_claude_with_meta`) -- `total_cost_usd`,
`usage.input_tokens`/`usage.output_tokens` -- never invented. When a call's
envelope doesn't report them, every cost field for that day is honestly
`None`/`UNKNOWN` rather than guessed, and only the call-count/time
governors remain enforceable.

```
python3 scripts/discovery_worker.py                       # one discovery-only cycle
python3 scripts/discovery_worker.py --trigger-type SAME_DAY_CATCH_UP
```

See `reports/V3.8.1-DISCOVERY-ONLY-PRODUCTION-REPORT.md` for the full
design, call graph, and cost-control rationale.

## Lead Engine daily run — what it actually automates (full_pipeline mode)

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

## Ranking evidence ingestion + deterministic re-evaluation (V3.7)

Claude cannot reliably obtain a real Google Maps/organic rank itself
(`config/opportunity_router.yaml`'s `never_auto_run` already excludes
`claude-seo:seo-dataforseo`/`seo-google`, and the acquisition worker's
`--restricted` profile has no tool for it either). Two human-operated CLI
scripts close that gap — neither is ever called by
`scripts/acquisition_worker.py` or any unattended Claude subprocess, and
neither is given any SEMrush/Google credential:

- **`scripts/import_ranking_observation.py`** — the clean, minimal
  external-enrichment interface (`schemas/ranking_evidence_observation.schema.json`):
  `maps_position`, `organic_position`, `query`, `location`, `observed_at`,
  `source` at minimum. Accepts a manually-checked Maps/Search observation
  or a single fact hand-transcribed from a real SEMrush export, one at a
  time or as a JSON batch file. Normalizes into the same
  `data/rankings/<market_id>.csv` storage `scripts/import_rankings.py`
  (the pre-existing bulk CSV/Semrush-export importer, unchanged) already
  uses — nothing duplicated. A missing `maps_position`/`organic_position`
  is never converted into a guessed value; an observation must supply at
  least one to be accepted, and neither field is ever inferred.
- **`scripts/reevaluate_needs_enrichment.py`** — once new ranking data
  exists for a `NEEDS_ENRICHMENT` lead's market, fills in ONLY the
  currently-null `maps_position`/`organic_position` field(s) (never
  overwrites an existing value), appends a `ranking_reevaluations`
  provenance entry to the lead's `qualification_v3.json` (source,
  observed_at, fields added — every prior section, e.g. `fit`/`gap`/
  `buying_signals`, is left in place, never deleted), then re-runs the
  exact same deterministic chain a first-time pass already uses
  unchanged — `assess_google_gap.py` → `assess_commercial_fit.py` →
  `qualify_leads.py --v3` — so the lead is re-routed to
  `QUALIFIED`/`HIGH_PRIORITY`/`MANUAL_REVIEW`/`REJECTED` exactly as if the
  ranking data had been available on day one. No re-discovery, no
  re-research, no Claude call. (The pre-existing `scripts/rescore_leads.py`
  is the V2-only equivalent — it does not understand the V3.1 FIT/GAP
  track and is unchanged/still V2-only; this new script is what a V3.1+
  `NEEDS_ENRICHMENT` lead actually needs.)

```
python3 scripts/import_ranking_observation.py --niche roofing --location "Columbus, OH" \
    --query "roof replacement columbus oh" --maps-position 6 --observed-at 2026-09-06 \
    --source manual_maps_check --business-name "..." --domain "..."
python3 scripts/reevaluate_needs_enrichment.py --id <slug>        # or --market <market_id> / --all
```

## Automated Ranking Enrichment (V3.8)

V3.7 gave a human two CLIs to close the ranking-evidence gap; V3.8 makes
draining that backlog part of the daily automated cycle, without adding any
new Claude spend, live provider call, or credential. See
`OPERATING-RULES.md`'s V3.8 update for the full policy context and
`reports/V3.8-AUTOMATED-RANKING-ENRICHMENT-REPORT.md` for the design writeup.

**Daily order**, inside `scripts/acquisition_worker.py`'s `run()`:

```
resume pending-lead work (unchanged V3.5)
  -> qualify_leads.py --v3
  -> scripts/rank_enrichment.py: run_cycle()      # V3.8, new
       -> build_enrichment_queue()                  (NEEDS_ENRICHMENT only, prioritized,
                                                       MANUAL_REVIEW never included)
       -> select_queries() per lead                  (2-4 money queries, config/niches.yaml
                                                       money_keywords, never invented)
       -> ranking_providers.attempt_query() per query, providers tried in
          config/ranking_enrichment.yaml order:
            ManualImportProvider  -- reads data/rankings/<market_id>.csv
            SemrushFileProvider   -- reads a pre-vetted file dropped in
                                      config/ranking_enrichment.yaml: inbox_dir
            (external_api slot exists but is NOT enabled -- see below)
       -> import_ranking_observation.import_observations() for anything new
       -> reevaluate_needs_enrichment.reevaluate_one() for every lead touched
       -> qualify_leads.py --v3 (only if any fields were actually added)
  -> advance newly QUALIFIED/HIGH_PRIORITY leads downstream (unchanged V3.2/V3.3 chain)
  -> fresh discovery (unchanged V3.5/V3.7, same caps as before -- never enlarged by V3.8)
```

**Provider abstraction** (`scripts/ranking_providers.py`) -- four possible
outcomes per (lead, query), never a fifth silent "treat missing as poor
rank" outcome: `ALREADY_SATISFIED` (fresh, valid evidence already on file --
nothing to import), `OBSERVATION` (a new, valid observation to import),
`RANKING_SOURCE_REQUIRED` (no provider could answer -- the honest,
fail-closed default), `FAILURE` (a provider itself broke -- timeout,
malformed data, an entity mismatch -- isolated per lead/query, never
blocking another lead or query, never a fabricated pass).

**Why no automatic provider is enabled today**: `ExternalRankProvider` is a
deliberately unimplemented interface -- there is no free/zero-cost way to
get a defensible, real Maps/organic position (Google has no such API;
scraping a SERP is exactly the "generic WebSearch ordering claimed as a
geo-local rank" this project explicitly forbids), and no paid rank-tracking
credential (DataForSEO/SerpApi/ValueSERP/Semrush API/similar) is configured
in this environment. Enabling one is a separate, explicitly authorized
future phase -- a real provider key, a cost review -- exactly like
`scripts/send_executor.py`'s real-send path.

**Config** (`config/ranking_enrichment.yaml`): `max_enrichment_leads_per_run`,
`max_queries_per_lead` / `min_queries_per_lead`,
`max_provider_requests_per_run`, `freshness_days` (kept equal to
`config/limits.yaml: ranking_freshness_days`, guarded by a test),
`inbox_dir`, and the ordered `providers` list.

**Reporting**: `scripts/rank_enrichment.py: run_cycle()`'s returned stats
(`ranking_backlog_before/after`, `ranking_leads_attempted`,
`ranking_queries_attempted`, `ranking_observations_imported`,
`ranking_provider_failures`, `qualified_after_ranking`,
`still_needs_enrichment`, `ranking_cost_estimate`) flow straight into
`scripts/acquisition_worker.py`'s returned stats dict and from there into
`data/runtime/daily_runs/<date>.json`, exactly like every other V3.5+ field.

```
python3 scripts/rank_enrichment.py --dry-run-queue   # inspect the prioritized backlog, touches nothing
python3 scripts/rank_enrichment.py                   # run one bounded enrichment cycle, prints stats JSON
```

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
