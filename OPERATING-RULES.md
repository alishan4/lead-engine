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
