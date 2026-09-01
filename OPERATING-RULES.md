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
| **Monthly tracker** | A human-readable reporting *mirror* of the above two — it is never authoritative over either |

**Never allow `READY_TO_SEND == GMAIL_SENT`.** Reaching `READY_TO_SEND` in
this repository means a draft cleared every deterministic gate this system
can apply. It says nothing about whether a real email was ever sent —
only Gmail (via ChatGPT's reconciliation) can say that.

**Never allow `NO_BOUNCE_DETECTED == DELIVERED`.** `NO_BOUNCE_DETECTED` in
`scripts/delivery_reconciliation.py` means no bounce signal has been
*observed* — it is the honest absence of negative evidence, not positive
proof of delivery. Absent a real signal, a message correctly stays in
`DELIVERY_CHECK` indefinitely; that is a valid terminal state, not a bug.

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

- The daily/scheduled run (`scripts/run_daily.sh`) automates only the
  deterministic stages of the pipeline. Stages that require real web
  research or human/Claude judgment (new-market discovery, first-time
  business verification, buying-signal evidence collection,
  franchise-status research, contact-identity (re-)verification,
  specialist-agent escalation) are never invoked unattended — a lead
  blocked on one of these is counted and reported, never guessed past.
- The scheduled run never calls `send_executor.py`,
  `delivery_reconciliation.py`, `follow_up.py`, or `reply_handling.py` —
  those all sit downstream of a real Gmail send, which this repository
  does not perform.
- A scheduled run must never overlap with another (single-run lock), must
  isolate a single lead's failure from the rest of the batch, must never
  delete production prospect history, and must exit non-zero only on an
  infrastructure-level failure (not on a per-lead skip).

See `docs/AUTOMATION.md` for the concrete scheduling, locking, dry-run
validation, and enable/disable procedure.
