# Lead Engine

Cost-bounded, discovery-first B2B lead acquisition engine for Claude Code.

Lead Engine finds independently-owned local-service businesses (roofing,
HVAC, restoration, plumbing, moving, family law, and similar niches) that
plausibly depend on Google Search/Maps for new customers, verifies them
with cheap deterministic checks, and hands off a clean list of candidates
for a human (optionally assisted by a separate LLM session) to qualify,
research, and contact. It does not send email, does not touch Gmail, and
does not run deep autonomous research by default.

## Why it exists

Early experiments with this project ran a much deeper, fully autonomous
pipeline: discovery, business verification, commercial-fit scoring,
Google-opportunity scoring, buying-signal research, specialist SEO
analysis, contact-identity research, and outreach drafting, all chained
together and triggered on a schedule. It worked, but it revealed a real
problem — multi-stage LLM research on a schedule can become surprisingly
expensive very quickly, with cost that scales in ways that are easy to
underestimate until you're looking at the bill.

The production path was redesigned around a different principle:

> **Claude discovers. Claude does not analyze.**

Lead Engine now separates **candidate discovery** (a single bounded Claude
call per market, plus fully deterministic verification) from
**qualification, sales intelligence, and outreach** (research-heavy work
that a human — optionally working with a separate LLM session — takes on
deliberately, one candidate at a time, once the cost of doing so is a
choice rather than something scheduled software does automatically).

The system is designed around cost limits, resumability, auditability,
failure isolation, and explicit capability boundaries — not around
maximizing how much a single scheduled run can autonomously accomplish.

## Architecture

The default scheduled path is deliberately short:

```
systemd timer
      |
      v
discovery_worker
      |
      +--> Claude discovery (bounded: capped calls, capped $ budget,
      |     capped runtime, capped market cells)
      |
      v
deterministic verification (zero additional Claude calls)
      |
      v
candidate persistence (local JSONL, idempotent)
      |
      v
CANDIDATES handoff (Google Sheets, optional)
      |
      v
STOP
```

Everything after "STOP" — FIT/GAP scoring, ranking enrichment, SEO
intelligence, contact verification, outreach drafting, QA, send-window
planning — is real, tested, and still present in this repository. It is
simply **not** invoked by the default scheduled path. These modules remain
available as an explicit, manually-invoked `full_pipeline` mode for anyone
who deliberately wants to spend the extra research budget on a specific
lead or backlog.

## Key principles

- **Discovery-first** — the scheduled path's only job is finding and
  cheaply verifying candidates, never analyzing them.
- **Fail closed** — an auth failure, a budget exhaustion, or a runtime
  deadline stops the run cleanly rather than guessing past it.
- **Deterministic where possible** — verification, deduplication,
  candidate-record construction, and Sheet serialization are all plain
  Python; Claude is used only where genuine judgment/research is required.
- **Cost bounded** — every Claude call is governed by an explicit daily
  dollar budget, a call cap, and a runtime ceiling, checked *before* the
  call is made, not after.
- **No invented evidence** — a missing fact stays missing (`null`/
  `UNKNOWN`); nothing is guessed to fill a gap, including rankings,
  contacts, or costs.
- **Idempotent persistence** — rediscovering the same business never
  creates a duplicate record or a duplicate handoff row.
- **Failure isolation** — one candidate's or one market's failure never
  blocks the rest of a run.
- **Auditable state** — every prospect has an explicit status; every run
  produces a summary; every Claude cost attempt (success or failure) is
  recorded in a durable, append-only ledger.
- **Explicit capability boundaries** — the scheduled worker's Claude
  subprocess is launched with a restricted tool profile that has no
  capability to send email, access Gmail, or submit a web form, regardless
  of what any prompt asks for.

## Cost controls

Every Claude call the default scheduled path makes is governed by config
in `config/discovery_only.yaml`:

| Setting | What it bounds |
|---|---|
| `daily_claude_budget_usd` | A hard dollar ceiling per calendar day, shared across every invocation that day (scheduled run, same-day catch-up, manual retry) via a durable, date-keyed ledger. |
| `max_claude_calls_per_run` | An independent circuit breaker on the number of Claude call *attempts* in one run, enforced even when dollar cost can't be observed. |
| `max_budget_usd_per_call` | The per-call `--max-budget-usd` circuit breaker passed straight to the Claude CLI. |
| `max_worker_runtime_seconds` | The wall-clock ceiling for the whole discovery cycle. |
| `min_seconds_to_start_claude_call` | The minimum runtime that must remain before a *new* Claude call is even started. |

What "attempt-based, not success-based" cost accounting means in practice:

- A call counts against the call cap the moment it is **attempted**, not
  only when it succeeds — a failed call is never refunded.
- A failed call can still be billable (a call that hits its own per-call
  budget mid-research still costs real money) — when the failure's own
  output exposes a real, observed cost, that cost is recorded in the
  daily ledger exactly like a successful call's.
- When a failure exposes **no** recoverable cost, the ledger honestly
  marks that day's accounting **incomplete** rather than assuming $0 —
  the call cap becomes the enforceable backstop in that state, and the
  remaining-budget figure is reported as unknown rather than a false
  precise number.
- Every Claude subprocess's own timeout is clamped to whatever worker
  runtime actually remains — an in-flight call can never itself blow past
  the worker deadline, and a call that is killed by the runtime deadline
  is recorded, never retried, and stops the run cleanly (no replacement
  call, no different market cell).

This software cannot mathematically guarantee an exact dollar figure never
gets exceeded (a live API call's true cost is only known after the fact,
and a genuinely unobservable failure means the ledger's own accounting is
honestly incomplete for that day) — what it guarantees is that every
governor is checked before spending, every attempt is counted whether it
succeeds or fails, and uncertainty is reported as uncertainty rather than
papered over.

## Production modes

`config/acquisition.yaml: production_mode` selects the flow the scheduled
worker runs:

- **`discovery_only`** (default) — the flow described above. This is the
  only mode the scheduled timer should ever run.
- **`full_pipeline`** — the complete, legacy research pipeline (business
  verification, FIT/GAP scoring, buying-signal research, ranking
  enrichment, specialist SEO analysis, contact-identity research, outreach
  drafting, QA, send-window planning). Fully functional and tested, but an
  explicit, manual/advanced invocation only — never the scheduled default.

An invalid `production_mode` value fails the run closed (non-zero exit,
no work performed) rather than guessing which pipeline to run.

## Candidate schema

Discovery-only mode's output is deliberately lightweight — factual
discovery information only, never a score or a judgment call:

```
lead_id
business_name
domain
website
city
state
country
niche
phone
discovery_source
discovered_at
verification_status
basic_business_facts
```

Example (synthetic — not a real business):

```json
{
  "lead_id": "roofing-exampleville-ex-example-roofing-co",
  "business_name": "Example Roofing Co.",
  "domain": "example-roofing.test",
  "website": "https://example-roofing.test/",
  "city": "Exampleville",
  "state": "EX",
  "country": "US",
  "niche": "roofing",
  "phone": null,
  "discovery_source": "roofing / Exampleville, EX",
  "discovered_at": "2026-01-01T00:00:00+00:00",
  "verification_status": "CANDIDATE_VERIFIED",
  "basic_business_facts": {
    "rating": 4.6,
    "review_count": 32,
    "years_in_business": 12,
    "commercial_value_signal": "high"
  }
}
```

## Google Sheets handoff

An optional handoff surface for downstream tools (a separate LLM session,
a spreadsheet-based workflow, a CRM import) to consume without reading raw
local state. Four tabs, each with a distinct, non-overlapping purpose:

- **`CANDIDATES`** — the discovery-only output described above.
- **`EMAIL_READY`** / **`CONTACT_FORM_READY`** — populated only by the
  optional `full_pipeline` mode once a lead reaches a fully-qualified,
  QA-passed, ready-to-send state.
- **`RESULTS`** — where an external Gmail-side process (never this
  repository) writes back real send/reply/bounce events.

The default backend (`backend: local` in `config/handoff.yaml`) requires
zero credentials and writes to local JSON/CSV files. Google Sheets is an
optional backend, configured generically: a Google Cloud service account
with *Sheets-only* scope (never Drive, never Gmail), shared as an editor
on your own private spreadsheet. See `config/handoff.example.yaml` for the
exact fields and `docs/AUTOMATION.md`'s "Google Sheets setup" section for
the full walkthrough. Your real service-account file path and spreadsheet
ID belong only in your own local `config/handoff.yaml` — never commit
them.

## Setup

```bash
git clone <repo-url>
cd lead-engine
python3 -m venv .venv && source .venv/bin/activate   # optional
pip install pyyaml
```

The Google Sheets backend additionally needs:

```bash
pip install google-api-python-client google-auth
```

Credentials are entirely optional — the local backend works with none at
all. If you do configure Google Sheets, keep your service-account file
outside the repository, e.g. under `$HOME/.config/lead-engine/`.

## Claude authentication

The discovery worker shells out to the `claude` CLI, which must already be
authenticated in your environment. A quick smoke test:

```bash
claude -p "Reply with exactly AUTH_OK"
```

If that doesn't return `AUTH_OK`, run `claude login` (or your
organization's equivalent) before running the discovery worker — it fails
closed with `CLAUDE_AUTH_REQUIRED` rather than guessing past an auth
problem, and never loops or retries authentication.

## Running manually

The narrowest entry point for one discovery-only cycle:

```bash
python3 scripts/discovery_worker.py
```

This performs exactly the flow in the architecture diagram above and
stops. It never touches qualification, ranking, contact research, or
outreach.

## systemd

An optional Tue–Fri scheduled run via `systemd --user`. Reference unit
files live in `systemd/`, using a placeholder path:

```bash
cp systemd/lead-engine-daily.* ~/.config/systemd/user/
# edit WorkingDirectory / ExecStart in lead-engine-daily.service to your
# own absolute checkout path (replace /path/to/lead-engine)
systemctl --user daemon-reload
scripts/run_daily.sh --dry-run          # validate first, every time
loginctl enable-linger "$USER"          # so it still fires while logged out
systemctl --user enable --now lead-engine-daily.timer
```

The timer is deliberately **not** persistent — if the machine is off or
asleep at the scheduled time, that day's run is simply skipped rather than
firing at an arbitrary later time. A separate, bounded same-day catch-up
window is available (`scripts/catchup.py`) for recovering a missed run
without turning into an unbounded backlog processor. See
`docs/AUTOMATION.md` for the full scheduling design.

## Google Sheets setup

Generic steps (no real values shown — see `config/handoff.example.yaml`):

1. Create a Google Cloud project and a service account with the Sheets API
   enabled (Sheets scope only — never Drive, never Gmail).
2. Download the service account's JSON key to a path *outside* this repo.
3. Create a spreadsheet with tabs named `CANDIDATES`, `EMAIL_READY`,
   `CONTACT_FORM_READY`, and `RESULTS` (header rows self-heal on first
   sync if a tab starts empty).
4. Share the spreadsheet with the service account's own email address as
   an Editor — never publish it with a public link.
5. Set `backend: google_sheets` plus your own `service_account_file` and
   `spreadsheet_id` in your local `config/handoff.yaml`.

Full walkthrough: `docs/AUTOMATION.md`.

## Testing

```bash
python3 -m unittest discover -s tests
```

At the V3.8.2 milestone, the project had 578 passing tests — pure-function
unit tests, sandboxed end-to-end subprocess tests (never touching real
data), and static guards that assert specific dangerous capabilities
(Gmail, outreach sending, unrestricted SEO-agent invocation) are
structurally unreachable from the default scheduled path.

## Safety model

The default `discovery_only` scheduled path:

- does not access Gmail
- does not send email
- does not submit contact forms
- does not automatically run SEO/specialist agents
- does not automatically perform contact research or enrichment
- does not fabricate rankings, contacts, costs, or any other evidence —
  a missing fact stays `null`/`UNKNOWN`

Every Claude subprocess the scheduled worker launches runs with
`--restricted` and an explicit tool allowlist (`Read`, `WebSearch`,
`WebFetch` at most) — this is a structural property of how the subprocess
is invoked, not a promise embedded only in a prompt.

## Repository layout

- `config/` — every threshold, cap, and budget as an editable value, never
  hardcoded in a script.
- `scripts/` — one script per pipeline stage; most expose pure,
  independently-testable decision functions alongside their CLI.
- `schemas/` — JSON Schema for every artifact a script produces.
- `tests/` — the full test suite.
- `docs/` — automation/scheduling design and setup guides.
- `reports/` — dated engineering reports documenting each build phase's
  design and validation record (real prospect/contact data redacted or
  replaced with synthetic examples before being committed).

## Roadmap

Ideas under consideration, not commitments:

- Alternate/pluggable discovery providers.
- Browser- or API-independent discovery sources.
- Cheaper deterministic company-discovery techniques.
- A real external ranking-data provider integration (the current provider
  interface is deliberately unimplemented — see `scripts/ranking_providers.py`).
- Optional CRM integrations for the handoff surface.
- Richer cost telemetry (e.g. per-call token-cost breakdowns over time).

## Contributing

Issues and pull requests are welcome. Before opening a PR:

- Run the full test suite (`python3 -m unittest discover -s tests`) and
  make sure it stays green.
- Never commit real prospect/contact/outreach data, credentials, or
  personal file paths — see `.gitignore` for what's already excluded.
- Keep the discovery-only default path's capability boundaries intact;
  changes that let the scheduled path reach Gmail, outreach sending, or
  unrestricted agent invocation need a very deliberate, explicit design
  discussion first.

## License

License: not yet selected.
