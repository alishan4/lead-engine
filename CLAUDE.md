# Lead Engine — instructions for Claude Code

**Before operating on or modifying this pipeline in any way, read
`OPERATING-RULES.md` in full.** It defines the permanent ownership
boundaries (Lead Engine vs. ChatGPT/Gmail vs. the human user), the source-
of-truth rules, and the non-negotiable pipeline rules (no fabrication, one
wedge per lead, deterministic-first, Tuesday–Friday sending, etc.). Nothing
in this file overrides it.

## What this repository is

A cost-optimized, evidence-driven local-SEO client-acquisition system. It
discovers and qualifies businesses, builds one defensible, evidence-backed
opportunity per lead, drafts an outreach email, runs deterministic QA, and
hands off a `READY_TO_SEND` record. **It stops there.** It does not send
email and does not hold Gmail credentials — see `OPERATING-RULES.md` §1.

## Absolute rules for any session touching this repo

1. **Never implement real Gmail/email sending in this repository.**
   `scripts/send_executor.py`'s only working path is a dry-run simulation.
   If a future task asks you to make sending real, treat that as a new,
   explicitly-authorized phase requiring its own confirmation — never as
   an incidental part of another task.
2. **Never fabricate data.** Missing evidence stays missing (`null`/
   `UNKNOWN`), never a guessed value, never a penalty. This applies to
   ranks, contacts, reviews, revenue, buying signals — everything.
3. **Never modify `../claude-seo`.** It is an external dependency, read
   from (agent definitions, routing) but never edited.
4. **Never commit real prospect/outreach data.** `data/leads/`,
   `data/prospects/`, `data/markets/`, `data/rankings/`, `data/outreach/`,
   `data/handoff/` (V3.6 shared queue + tracker CSVs), and `data/runtime/`
   are gitignored by design (see `.gitignore`) because they accumulate real
   third-party contact information and operational history. Use
   `data/fixtures/` for anything that needs to be committed as an example.
   If you generate a new kind of runtime artifact, gitignore it at the same
   time you create it — don't wait for a security pass to catch it later.
   This repo is private and must stay code-only: no real prospect/contact/
   outreach records, no Google Sheets service-account keys, no OAuth
   tokens, no Gmail cookies/session data, ever, under any filename.
5. **Run the full test suite before and after any change**
   (`python3 -m unittest discover -s tests`). Do not alter a test merely
   to make a number match expectations — report the real result.
6. **Preserve failure isolation.** A single lead's failure must never stop
   processing of the rest of the batch, in any script you write or modify.
7. **Preserve one-wedge and no-fabrication guarantees when extending
   scoring, routing, or drafting logic.** These are safety properties, not
   style preferences — see `OPERATING-RULES.md` §3 for the full list.

## Where things live

- `config/` — every threshold, weight, and window is an editable config
  value, never hardcoded in a script.
- `schemas/` — JSON Schema for every artifact a script produces.
- `scripts/` — one script per pipeline stage; most expose pure,
  independently unit-testable decision functions alongside their CLI.
- `tests/` — the pure-function test suite (see `OPERATING-RULES.md` §4 for
  what a scheduled run is and isn't allowed to touch).
- `data/fixtures/` — synthetic example records showing every artifact's
  real shape, safe to read and safe to commit.
- `docs/AUTOMATION.md` — the Tuesday–Friday scheduled-run design, the
  systemd units, the dry-run validation procedure, how to enable/disable
  the timer, and (V3.5) the unattended Claude acquisition worker and
  same-day catch-up design.
- `config/acquisition.yaml` / `scripts/acquisition_worker.py` (V3.5) — the
  ceilings/timeouts/budgets and orchestration for the unattended Claude
  research worker; see `OPERATING-RULES.md` §4's V3.5 update for the
  structural safety model before touching either.
- `config/handoff.yaml` / `scripts/handoff_lib.py` / `scripts/handoff_backend.py`
  / `scripts/sync_handoff.py` / `scripts/import_outreach_results.py` /
  `scripts/export_tracker_csv.py` (V3.6) — the shared READY_TO_SEND queue
  ChatGPT/Gmail-side automation consumes, and the path external Gmail
  result events come back through. See `OPERATING-RULES.md` §1/§2's V3.6
  updates before touching any of these — in particular, never let Lead
  Engine's own sync write to the `EXTERNAL_OWNED_FIELDS` in
  `schemas/handoff_row.schema.json`.
- `scripts/import_ranking_observation.py` / `scripts/reevaluate_needs_enrichment.py`
  (V3.7) — the external ranking-evidence interface and the V3.1-track
  deterministic re-evaluation it feeds; see `docs/AUTOMATION.md` "Ranking
  evidence ingestion + deterministic re-evaluation." Both are human-
  operated CLI tools, never called by `acquisition_worker.py` or any
  unattended Claude subprocess, and neither is ever given a SEMrush/Google
  credential.
- `reports/` — dated engineering reports for each build phase (V1 through
  V3.3). Real contact-channel identifiers (emails, phone numbers, personal
  names tied to real third-party businesses) are redacted from these
  before they're committed — do not reintroduce them when editing a
  report; keep full-fidelity research in the local, gitignored `data/`
  tree only.

## When in doubt

If a task would require this repository to send a real email, hold a real
Gmail credential, or commit real third-party contact data, stop and ask
the user rather than proceeding — these are exactly the boundaries
`OPERATING-RULES.md` exists to protect.
