# Operating Rules — Lead Engine

This is the permanent, standalone operating contract for this system. It
supersedes `reports/OPERATING-RULES-V3.md` (kept for history — that file
was written as a delta against an earlier, unpersisted conversation and is
not self-contained). Any future Claude Code session working in this
repository must read this file before modifying the pipeline, and must not
violate any rule below without the user explicitly overriding it in the
same session.

---

## 1. Ownership boundaries (who does what)

### LEAD ENGINE / CLAUDE owns:
- Research and discovery (market/business identification)
- Business identity verification
- FIT / GAP scoring (commercial fit and Google/local opportunity)
- Buying-signal assessment (evidence-based, never inferred)
- Contactability checks
- Targeted SEO/local intelligence (deterministic scan + at most one
  specialist agent call, per `config/opportunity_router.yaml`)
- Selecting exactly one defensible wedge (never more, never forced)
- Building the compact intelligence dossier
- Staging the outreach asset (deterministic templating, no extra LLM call)
- Contact identity verification (re-verified, never blindly trusted from a
  prior phase)
- Drafting the outreach email
- QA (deterministic, rule-based — not an LLM judgment call)
- Send-window planning
- Producing the `READY_TO_SEND` handoff record

**Lead Engine's automated responsibility ends at `READY_TO_SEND`.** It
never sends a real email and never touches a real Gmail account.

### V3.6 update — the shared handoff bridge

`READY_TO_SEND`/`SEND_WINDOW_PLANNED` leads are additionally packaged into
a **shared queue** (`scripts/sync_handoff.py`, `scripts/handoff_backend.py`)
so ChatGPT/Gmail-side automation can consume them without re-reading raw
Lead Engine artifacts. This does not move the `READY_TO_SEND` boundary —
Lead Engine still never sends anything and never touches Gmail; the shared
queue is a **read view for ChatGPT**, not a new capability for Lead Engine.
The permanent flow:

```
Lead Engine -> READY_TO_SEND -> shared queue (EMAIL_READY / CONTACT_FORM_READY)
  -> ChatGPT reads queue -> Gmail reconciliation -> real send/reply/bounce
  -> result events written back to the shared queue
  -> Lead Engine imports results (scripts/import_outreach_results.py)
  -> monthly reporting updated (scripts/export_tracker_csv.py)
```

Backend is provider-neutral (`config/handoff.yaml: backend`): `local`
(zero credentials, always available, `data/handoff/`) or `google_sheets`
(a real, credential-gated adapter — see `docs/AUTOMATION.md` "Google Sheets
setup"). Neither backend ever touches Gmail; the Sheets adapter uses its
own separate service-account credential with Sheets-only scope, never
Gmail OAuth. See `scripts/handoff_lib.py`'s `EXTERNAL_OWNED_FIELDS` for the
mechanism that makes the next rule structural rather than just documented.

### CHATGPT owns (downstream of the handoff):
- Real Gmail reconciliation (checking the actual mailbox before acting)
- Prior-email / duplicate-thread detection against the real account
- Reply detection and monitoring
- Bounce detection
- Suppression-list reconciliation against real delivery outcomes
- Actual Gmail send execution
- Gmail-side "sent" verification
- Delivery/reply monitoring over time
- Follow-up execution (the actual send of a follow-up message)

### USER owns:
- The human sales conversation that starts after a positive reply
- Final judgment on anything routed to `HUMAN_REVIEW`
- Deciding when/whether to override any of the above boundaries

**Lead Engine must never directly hold or use the user's personal Gmail
credentials.** No script in this repository performs OAuth, stores a
token, or calls a mail-sending API of any kind. `scripts/send_executor.py`
has exactly one working path — `dry_run_send()` — and its
`production_send_DESIGNED_NOT_IMPLEMENTED()` stub always raises. Turning
that into a real send is out of scope for this repository's current phase
and requires a separate, explicitly authorized implementation step.

---

## 2. Source of truth

| System | Is the source of truth for |
|---|---|
| **Lead Engine** (this repo's `data/`) | Research findings, qualification state, FIT/GAP scores, the selected wedge, dossier, staged asset, draft content, QA verdicts, planned send windows |
| **Gmail** (the real account, owned by the user, operated via ChatGPT) | Whether an email was actually sent, delivered, bounced, replied to, or opened |
| **Shared queue** (V3.6, `data/handoff/` local / a private Google Sheet) | Handoff/control plane only — a synchronized VIEW of Lead Engine's and Gmail's own state, never a third independent source of truth. See below. |
| **Monthly tracker** | A human-readable reporting *mirror* of the above — it is never authoritative over any of them |

**Never allow `READY_TO_SEND == GMAIL_SENT`.** Reaching `READY_TO_SEND` in
this repository means a draft cleared every deterministic gate this system
can apply. It says nothing about whether a real email was ever sent —
only Gmail (via ChatGPT's reconciliation) can say that. In the shared queue
(`schemas/handoff_row.schema.json`) this is two separate columns,
`lead_engine_state` and `gmail_state` — never one overloaded status — and
`gmail_state` stays `null` until a real imported Gmail-side event sets it.

**Never allow `NO_BOUNCE_DETECTED == DELIVERED`.** `NO_BOUNCE_DETECTED` in
`scripts/delivery_reconciliation.py` means no bounce signal has been
*observed* — it is the honest absence of negative evidence, not positive
proof of delivery. Absent a real signal, a message correctly stays in
`DELIVERY_CHECK` indefinitely; that is a valid terminal state, not a bug.
Same non-equivalence in the shared queue's `delivery_state` column.

**The shared queue can never override a verified Gmail reply/bounce
event.** `scripts/handoff_lib.py`'s `EXTERNAL_OWNED_FIELDS`
(`gmail_state`, `delivery_state`, `reply_state`, `follow_up_state`,
`gmail_message_id`, `gmail_thread_id`, `suppression_reason`) are fields
Lead Engine's own sync (`merge_row`) never writes after first creation —
only `scripts/import_outreach_results.py`, applying a real external event
(`apply_event`), can change them, and only if the incoming event is newer
than whatever is already recorded for that field (never lets a stale
re-import regress a field a newer event already set).

**One malformed result event can never abort or block another (V3.6.1).**
Every incoming event is validated (`scripts/handoff_lib.py: validate_event`
— requires a real `lead_id` and a recognized `event_type`) and processed
inside its own isolated exception boundary in
`scripts/import_outreach_results.py`. A missing `lead_id`, missing
`event_type`, unrecognized `event_type`, or a malformed/truncated Sheets
row is recorded as a failure and the batch continues — the same
failure-isolation guarantee the rest of this pipeline already holds for a
single lead's research failure, extended to a single external event.

---

## 3. Non-negotiable pipeline rules (unchanged since V1)

- Never run all `claude-seo` specialist agents on every lead. At most one
  specialist call for a normal `QUALIFIED` lead, at most two for
  `HIGH_PRIORITY`, per `config/limits.yaml`.
- **Missing data is never treated as bad data.** A null rank, unknown
  review count, or unresolved buying signal contributes zero to a score —
  never a penalty, never a guess. ("UNKNOWN != FALSE".)
- Never fabricate: rank, email address, review count, traffic, revenue,
  cost-per-click, years in business, licenses, certifications, customer
  count, or a competitor's numbers. If it wasn't found on a real,
  citable source, it does not go in a draft.
- Contact identity and mailbox validity are two separate axes
  (`schemas/contact_record.schema.json`) — never infer one from the other,
  and never infer a `firstname@`/`info@`/`hello@`/`sales@` address from a
  bare domain.
- Contact priority order: owner/founder/partner/managing professional →
  marketing/growth lead → operations/practice manager → verified company
  inbox → contact form. Never skip to a lower-priority channel if a
  higher one is genuinely verifiable.
- Exactly one wedge per lead, selected on
  commercial_relevance/evidence_confidence/specificity/actionability —
  never on raw technical severity, and never forced merely to produce an
  output (`NO_DEFENSIBLE_WEDGE` is a valid result).
- Every wedge must pass the company-swap test: if the business name were
  replaced, the observation must still be false for most other
  businesses.
- Send only Tuesday–Friday, local business hours
  (`config/outreach.yaml: send_window`). Monday is for research/prep, not
  sending.
- QA is deterministic and rule-based, not an LLM judgment call — see
  `scripts/qa_outreach_email.py`. A draft that fails QA is never sent
  anyway (the downstream Gmail step never receives it).
- One suppression, one account-level outreach lock, per real business —
  never send a duplicate thread to the same business under a different
  prospect record.
- A follow-up must add genuine new value; there is no "just checking in"
  template (`scripts/follow_up.py`).
- A reply classifier defaults to conservative: ambiguous text never
  auto-resolves to POSITIVE.
- Recycling a closed lead requires **both** enough elapsed time **and** an
  explicit new trigger signal — time alone is never sufficient.

---

## 4. Automation-specific rules

### V3.5 update (2026-09-02) — supersedes the "never invoked unattended" rule below

Through V3.4, this section said the stages requiring real web research or
Claude judgment (new-market discovery, business verification, buying-signal
evidence, franchise research, contact-identity verification, specialist
escalation) were **never invoked unattended** — a human always had to paste
research into an interactive Claude session. That boundary is deliberately
lifted as of V3.5, by explicit user authorization, now that non-interactive
`claude -p` execution under systemd --user is verified working. It is
replaced with a **structural**, not merely promptable, safety model:

- `scripts/acquisition_worker.py` runs every one of those research stages
  through `scripts/claude_invoke.py`, which invokes `claude -p` with
  `--restricted` plus an explicit `Read, WebSearch, WebFetch` tool
  allowlist. `--restricted` unconditionally strips out Bash/PowerShell/
  REPL/other code-execution tools and refuses `--dangerously-skip-
  permissions`. **The invoked Claude process therefore has no tool capable
  of sending email, accessing Gmail, submitting a contact form, or writing
  any file, under any prompt it could ever receive** — this holds for
  every stage, including specialist escalation, which deliberately does
  not shell out to the interactive claude-seo Skill packages (they require
  Bash/Write) and instead answers the same routed, capped question through
  the same restricted profile (see `acquisition_worker.py: ask_specialist`).
  The orchestrator process — never Claude — is the only thing that ever
  writes a file or runs a script, via the same `--save -` contract every
  research-stage script already used for a human-driven research pass.
- A fail-closed auth preflight (`scripts/claude_preflight.py`) runs before
  any research: if Claude auth is unavailable, the run records
  `CLAUDE_AUTH_REQUIRED` and performs zero research that cycle — it never
  fabricates a result or guesses past a stage it couldn't actually run.
- Every ceiling this worker obeys is in `config/acquisition.yaml`: an
  outreach-worthy ceiling (15, a ceiling not a quota — see V3.5's own
  report for why fewer, better prospects is a fully successful outcome), a
  bounded fresh-discovery market rotation (never "search all of the US"),
  a wall-clock worker timeout, per-call timeouts, and a per-call
  `--max-budget-usd` cost circuit breaker.
- FIT/GAP thresholds (`config/scoring.yaml`), specialist-call caps
  (`config/limits.yaml`), and every V3.1–V3.4 decision function are
  completely unchanged — the acquisition worker calls the exact same
  scripts unattended that a human previously ran interactively; it does
  not reimplement or loosen any of them.
- The four scripts below remain **permanently** un-called by any automated
  path, unattended or not — this specific rule is not superseded by
  anything above.

### V3.8 update (2026-09-03) — Automated Ranking Enrichment; narrowly
### supersedes one V3.7 sentence about `reevaluate_needs_enrichment.py`

Through V3.7, `scripts/import_ranking_observation.py` and
`scripts/reevaluate_needs_enrichment.py` were both described as "human-
operated CLI tools, never called by `acquisition_worker.py` or any
unattended Claude subprocess." V3.8 deliberately narrows that, by explicit
user authorization, for `reevaluate_needs_enrichment.py`'s own re-evaluation
logic ONLY (imported and called as a plain Python function — never invoked
as a "researcher" and never given anything to guess):

- `scripts/rank_enrichment.py` now runs automatically inside
  `scripts/acquisition_worker.py`'s daily cycle, in this order: (1) resume
  incomplete pending-lead work, (2) **drain the ranking-enrichment backlog**,
  (3) run `scripts/reevaluate_needs_enrichment.py`'s deterministic
  re-evaluation for every lead touched, (4) advance anything newly
  `QUALIFIED`/`HIGH_PRIORITY` through the existing intelligence/dossier/
  asset/contact-identity chain, (5) **only then** spend any remaining budget
  on fresh discovery. This ordering is the entire point of V3.8: it stops
  `NEEDS_ENRICHMENT` from being a parking lot that only grows, without
  spending one extra dollar of Claude research to do it.
- `scripts/import_ranking_observation.py` itself is **not** newly called
  unattended — it remains a human-operated CLI for producing a new,
  provenance-checked observation from scratch. What V3.8 automates is
  narrower and safer than that: `scripts/ranking_providers.py`'s
  `ManualImportProvider` and `SemrushFileProvider` only ever **read**
  already-durable, already-vetted evidence — either previously imported
  into `data/rankings/<market_id>.csv` by a human running that CLI, or a
  small pre-vetted observations file (the same
  `schemas/ranking_evidence_observation.schema.json` shape that CLI already
  accepts via `--file`) a human/analyst drops into
  `config/ranking_enrichment.yaml: inbox_dir`. The human still produces and
  vets every fact; the pipeline automates noticing it, validating its
  provenance (reusing `import_ranking_observation.py`'s own
  `validate_observation()` — nothing re-implemented or loosened), and
  running the existing deterministic re-scoring chain.
- **No live ranking-provider call, no SEMrush/Google/any other credential,
  and no Claude call exists anywhere in this path.** `scripts/
  ranking_providers.py: ExternalRankProvider` is a deliberately unimplemented
  interface — it always returns `RANKING_SOURCE_REQUIRED`. Enabling a real,
  credential-backed live rank-tracking provider is explicitly out of scope
  for V3.8 and requires its own future authorization (a real provider key +
  a cost review), exactly like `scripts/send_executor.py`'s real-send path
  requiring its own separate authorization before V3.5/V3.8 changed
  anything about it.
- Every ceiling this stage obeys lives in `config/ranking_enrichment.yaml`:
  `max_enrichment_leads_per_run`, `max_queries_per_lead`,
  `max_provider_requests_per_run`, `freshness_days` (kept in sync with
  `config/limits.yaml: ranking_freshness_days`, guarded by a test). FIT/GAP
  thresholds (`config/scoring.yaml`) are completely unchanged and untouched
  by this stage — ranking enrichment can only supply evidence that feeds
  the existing, frozen scoring functions; it never lowers a bar to qualify
  a lead.
- `MANUAL_REVIEW` leads are never entered into the ranking-enrichment
  queue — see `scripts/rank_enrichment.py: build_enrichment_queue`'s
  defensive status filter. Ranking evidence alone can never qualify a
  `MANUAL_REVIEW` lead; that gate stays a human FIT judgment call.
- `scripts/acquisition_worker.py`'s own Claude research budget/timeouts/
  discovery caps (`max_fresh_market_cells_per_run`,
  `max_fresh_candidates_researched_per_run`) are **completely unchanged** by
  V3.8 — the ranking-enrichment stage never competes for or extends that
  budget, and a growing enrichment backlog is never a reason to raise it.
  See `reports/V3.8-AUTOMATED-RANKING-ENRICHMENT-REPORT.md` for the full
  design and the real backlog snapshot at time of writing.

### V3.8.1 update (2026-09-03) — Discovery-Only Production Mode + hard
### cost governors; changes the SCHEDULED DEFAULT, supersedes nothing

Context: roughly $100 in Claude/API spend accumulated over two days of
building/running this pipeline — unacceptable for the current
lead-acquisition stage. V3.8.1 splits responsibility permanently:

- **Fedora / Lead Engine** discovers candidate businesses, runs cheap
  deterministic verification, saves candidates, syncs them to a
  **CANDIDATES** Google Sheet, and **stops**.
- **ChatGPT + the user** do everything downstream: qualification,
  Google/Maps opportunity research, competitor research, the SEO wedge,
  contact research, personalized outreach, Gmail execution, reply
  handling/sales.

This is a **routing change, not a deletion**. Every V3.1–V3.8 stage
(FIT/GAP scoring, buying signals, ranking enrichment, specialist agents,
contact identity, outreach drafting, QA, send-window planning,
`READY_TO_SEND` export) still exists, is still tested, and is still fully
callable — it simply never runs on the default scheduled path anymore.

- **`config/acquisition.yaml: production_mode`** (default
  `discovery_only`) is the single switch. `scripts/run_daily.py` fails
  closed (`infrastructure_failure`, non-zero exit) on any value other than
  `discovery_only`/`full_pipeline` — never guesses which pipeline to run.
  `full_pipeline` reproduces the complete, unchanged V3.5–V3.8 flow
  exactly (still available for a deliberate manual/explicit run); it is
  never the scheduled timer's default again.
- **`scripts/discovery_worker.py`** is the new default flow: resume →
  preflight → discover (one Claude call per market cell, reusing V3.7's
  tier-weighted rotation unchanged) → **deterministic** basic verification
  (`scripts/candidate_verification.py` — zero additional Claude calls per
  candidate; "CLAUDE DISCOVERS, CLAUDE DOES NOT ANALYZE") → persist at
  `CANDIDATE_VERIFIED`/`CANDIDATE_REJECTED` (new, deliberately terminal
  statuses — never overload `NEEDS_ENRICHMENT`, never touch any existing
  `QUALIFIED`/`NEEDS_ENRICHMENT`/`MANUAL_REVIEW`/`CONTACT_FORM_READY`/
  `READY_TO_SEND` record) → sync the CANDIDATES sheet
  (`scripts/sync_handoff.py: sync_candidates()`, idempotent upsert by
  `lead_id`, additive alongside the unchanged EMAIL_READY/
  CONTACT_FORM_READY/RESULTS tabs) → a simplified report
  (`scripts/report_discovery_only.py`) → stop.
- **Hard cost governors** (`config/discovery_only.yaml`), checked BEFORE
  every Claude call, never only after: a daily `$` budget
  (`daily_claude_budget_usd`) enforced via a durable, date-keyed ledger
  (`scripts/cost_ledger.py`, `data/runtime/cost/<date>.json`, gitignored)
  that is **shared across every invocation that day** — the scheduled run,
  a same-day catch-up run, and any manual retry all draw from the same
  allowance, never a fresh budget each time; an independent
  `max_claude_calls_per_run` circuit breaker that holds even when $ cost
  isn't observable; a `max_worker_runtime_seconds` far smaller than
  `full_pipeline`'s 2700s; and `min_candidates_target`/
  `max_candidates_target`, which are a **goal, never a quota** — hitting a
  budget/call/time limit before the target is reached is reported as
  `budget_status` (e.g. `EXHAUSTED`, `CALL_CAP_REACHED`), never treated as
  a failure, and never triggers a retry, a different market, or another
  Claude call. Real cost/token figures come from `claude -p`'s own JSON
  envelope (`claude_invoke.py: run_claude_with_meta`) — never invented;
  when unobservable, every cost field is honestly `None`/`UNKNOWN` and
  only the call-count/time governors remain enforceable.
- **Auth failure fails closed exactly once** — no retry loop, no repeated
  spawn, no acquisition cycle triggered — reports `CLAUDE_AUTH_REQUIRED`
  and stops, identical in spirit to the V3.5 preflight model.
- See `reports/V3.8.1-DISCOVERY-ONLY-PRODUCTION-REPORT.md` for the full
  design, the call graph, and cost-control rationale.

### V3.8.2 update (2026-09-03) — Cost-Guard Hardening; corrects two real
### defects the first live validation exposed, supersedes nothing above

The V3.8.1 first live validation (same day) succeeded functionally but
surfaced two real cost-accounting gaps: (1) a failed Claude call can still
be billable — a call that hit its own per-call `--max-budget-usd` circuit
breaker mid-research still cost a real $0.5358346, which never reached the
daily ledger because `max_claude_calls_per_run`/the ledger only ever
recorded a call AFTER it succeeded; (2) the worker-runtime ceiling was
checked only BETWEEN market cells, never against an in-flight call, so
actual wall-clock ran to 290s against a configured 180s cap. Both are
closed structurally, not just documented:

- **The call cap now counts every ATTEMPT**, not every success —
  `scripts/discovery_worker.py: CostGuard.reserve_attempt()` reserves a
  call-budget slot and writes a `PENDING` entry to the daily ledger
  (`scripts/cost_ledger.py: start_attempt()`) **before** the real `claude
  -p` subprocess is spawned, and it is never refunded regardless of
  outcome (success, failure, timeout, malformed output, non-zero exit, or
  hitting the call's own budget). A crash/kill between reservation and
  completion still leaves this honest, unknown-cost trace rather than
  losing the attempt from accounting entirely.
- **A failed call's real cost now reaches the daily ledger.**
  `claude_invoke.py`'s exceptions (`ClaudeAuthRequired`/`ClaudeTimeout`/
  `ClaudeInvocationError`, now sharing a `ClaudeCallError` base) carry a
  `.meta` attribute populated from whatever the failed call's own output
  exposed — the common real case (a `--max-budget-usd` trip) still emits a
  complete JSON envelope reporting the real cost before exiting non-zero.
  `CostGuard.record_attempt_result()` records this exactly like a
  success's cost.
- **Conservative accounting when cost is genuinely unknown.** A run's
  stats now report `budget_accounting_status`
  (`COMPLETE`/`INCOMPLETE_UNKNOWN_CALL_COST`) and `unknown_cost_attempts`
  explicitly; `budget_remaining_usd` is `None` — never a confident number
  — the moment any attempt today has unknown cost, and the call cap
  remains the hard backstop in that state, exactly as it already was.
  `observed_total_cost_usd` still reports the sum of every KNOWN cost
  (never suppressed to `None` just because some other attempt is
  unknown) — a known partial figure, honestly labeled incomplete, beats
  reporting nothing.
- **The worker runtime deadline now constrains individual Claude calls,
  not just the gap between market cells.** Before every attempt (each
  retry included), the subprocess timeout is clamped to
  `min(configured per-call timeout, remaining worker seconds)`, and a call
  is never even started if less than `config/discovery_only.yaml:
  min_seconds_to_start_claude_call` (60s) of runtime remains. A call that
  is genuinely killed by the worker deadline mid-flight is recorded
  (attempted, failed, `WORKER_DEADLINE_TIMEOUT`) and never retried — the
  whole run stops cleanly, exactly per the "no replacement/retry call"
  rule.
- `max_budget_usd_per_call` ($0.50) was inspected, not raised — see
  `config/discovery_only.yaml`'s own comment for the documented
  relationship between it, `daily_claude_budget_usd`,
  `max_claude_calls_per_run`, `max_worker_runtime_seconds`, and the
  (never-overriding) candidate target.
- See `reports/V3.8.2-COST-GUARD-HARDENING-REPORT.md` for the full root-
  cause analysis and test coverage.

### Rules unchanged since V1–V3.4

- The daily/scheduled run (`scripts/run_daily.sh`) automates the
  deterministic stages of the pipeline, now preceded by the V3.5
  acquisition worker described above (pass `--deterministic-only` to
  reproduce the pre-V3.5 behavior exactly — the rollback lever if the
  worker is ever disabled). A lead still blocked on a research stage after
  the worker's own budget/ceiling/timeout is counted and reported, never
  guessed past.
- The scheduled run never calls `send_executor.py`,
  `delivery_reconciliation.py`, `follow_up.py`, or `reply_handling.py` —
  those all sit downstream of a real Gmail send, which this repository
  does not perform.
- A scheduled run must never overlap with another (single-run lock — now
  two layers: `run_daily.sh`'s `run.lock` flock, and
  `acquisition_worker.py`'s own `acquisition.lock` for standalone/
  validation/catch-up invocations), must isolate a single lead's failure
  from the rest of the batch, must never delete production prospect
  history, and must exit non-zero only on an infrastructure-level failure
  (not on a per-lead skip).

See `docs/AUTOMATION.md` for the concrete scheduling, locking, dry-run
validation, and enable/disable procedure, and
`reports/V3.5-UNATTENDED-ACQUISITION-REPORT.md` for the full V3.5 design
and validation record.
