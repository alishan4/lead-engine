# Lead Engine — Technical Evolution

A technical history of how this project's architecture arrived at its
current discovery-only default. Written for engineers extending or
auditing the codebase, not as end-user documentation (see the root
`README.md` for that) — this is more useful as a map of *why* each layer
exists than as a setup guide.

All examples in this document use synthetic business names, markets, and
identifiers. No real company names, contact details, spreadsheet IDs, or
production results appear here.

## V1–V2: deterministic-first qualification

The earliest version of this pipeline established the pattern every later
version reuses: score a discovered business with cheap, deterministic
rules *before* any AI is involved, and only let a lead reach an expensive
research stage once a rule-based gate says it's worth the spend. A missing
data point (an unmeasured ranking, an unknown review count) was already
treated as *unknown*, never as a penalty — the "UNKNOWN != FALSE"
principle that every later scoring layer still follows.

## V3.1: FIT/GAP as separate axes

V3.1 split "is this a good lead" into two independent questions:

- **FIT** — is this a commercially attractive business (niche economics,
  maturity, buying intent, contactability, market attractiveness)? Has no
  term derived from SEO/website quality at all.
- **GAP** — is there a defensible Google/local acquisition opportunity
  (Maps/GBP gap, organic visibility gap, service-page thinness, review
  gap, technical/indexation issues, competitor gap)?

A business with terrible SEO does not qualify merely because its SEO is
terrible — it must also show real commercial fit. `QUALIFIED` requires
both FIT and GAP above their own thresholds; `HIGH_PRIORITY` additionally
requires a confirmed buying/timing signal, never GAP size alone.

## V3.2–V3.4: deterministic-first intelligence, one wedge, outreach QA

Later V3.x phases added a deterministic-first "quick audit" layer (a
capped, targeted specialist-agent call only when the deterministic scan
itself is ambiguous — never a full specialist roster run on every lead),
a rule requiring exactly one defensible "wedge" per lead (never multiple,
never forced), and a deterministic (not LLM-judged) QA gate before any
draft could be considered ready.

## V3.5: unattended acquisition, structurally restricted

V3.5 replaced "a human pastes research into an interactive Claude session"
with a real, non-interactive `claude -p` invocation — but every one of
those calls runs under `--restricted` plus an explicit tool allowlist
(`Read`, `WebSearch`, `WebFetch` only). This is a *structural* property of
how the subprocess is invoked (`--restricted` unconditionally strips
Bash/Write/any code-execution tool and refuses
`--dangerously-skip-permissions`), not merely an instruction inside a
prompt — the invoked process has no tool capable of sending email,
accessing Gmail, or submitting a form, regardless of what any prompt asks
for.

## V3.6: shared handoff bridge

V3.6 added a shared queue (local files, or an optional Google Sheets
backend) so a downstream consumer (a separate LLM session handling actual
outreach) could read a flattened view of ready-to-send leads without
re-reading raw internal state. Two ownership rules made this safe rather
than just documented:

- `READY_TO_SEND` (this repository's own state) is never conflated with
  `GMAIL_SENT` (a real external event) — they are separate columns in the
  shared row, and only a real imported event can ever set the latter.
- A small set of fields (`gmail_state`, `delivery_state`, `reply_state`,
  `follow_up_state`, message/thread IDs, suppression reason) are
  "externally owned" — this repository's own sync logic never overwrites
  them once an external event has set them, and a stale re-import can
  never regress a field a newer event already set.

## V3.7 / V3.7.1: diversified discovery, query-aware ranking

V3.7 fixed a real production bug: an early market-rotation algorithm
processed niches in long, non-interleaved blocks, so a bounded discovery
window could spend nearly its entire budget on one niche. The rotation was
rewritten to interleave niches (tier-weighted, so higher-value niches
still appear more often, but no niche is ever starved).

V3.7.1 fixed a related scoring bug, using a synthetic case shaped exactly
like a real one this project encountered: a business tracked under three
distinct search queries with local-pack positions of, say, #6, #4, and #2.
The old scoring logic reduced multiple ranking observations for one
business to a single `min()` value — which is correct when multiple
sources are confirming the *same* query's rank, but wrong once genuinely
distinct queries are tracked: taking the minimum (#2) silently erased two
real, independently-verified opportunities (#6 and #4, both inside the
scoring model's "worth pursuing" band) purely because a third, unrelated
query happened to rank strongly. The fix selects a *real* observed
position for a *real* query that actually falls in the opportunity band —
never an average, never invented — while preserving every per-query
observation for provenance. This "never collapse per-query evidence into
one number" invariant is now a permanent regression test.

## V3.8: automated ranking enrichment

V3.8 added a provider abstraction for closing ranking-evidence gaps
without spending Claude research budget on it:

```
NEEDS_ENRICHMENT queue
  -> prioritize (FIT confirmed, GAP potential, niche tier, contactability)
  -> select a small, bounded set of money queries per lead
  -> ask a provider chain for evidence (manual import file, or a
     pre-vetted analyst-provided file — never a live scrape)
  -> import + deterministically re-evaluate
  -> QUALIFIED, or still NEEDS_ENRICHMENT
```

The provider interface includes a deliberately **unimplemented**
`ExternalRankProvider` slot — there is no free, credential-free way to get
a defensible localized Maps/organic ranking today (Google has no such
API, and scraping a live SERP to infer a rank is exactly the kind of
"generic ordering claimed as a geo-local rank" this project's own rules
forbid). Enabling a real, paid, credential-backed provider is a
deliberate, separately-authorized future step, not something this
codebase does automatically.

## V3.8.1: discovery-only production mode

By this point the fully autonomous pipeline had become expensive enough,
in a short enough window, to justify a permanent architecture change
rather than another tuning pass. V3.8.1 introduced
`config/acquisition.yaml: production_mode`, defaulting the scheduled path
to `discovery_only` — the flow described in the root README — while
preserving every prior stage as an explicit, still-fully-tested
`full_pipeline` mode.

Key design choices:
- Verification is 100% deterministic (presence checks on what discovery
  itself already returned) — zero additional Claude calls per candidate.
- A dedicated `CANDIDATE_VERIFIED` / `CANDIDATE_REJECTED` status pair,
  deliberately distinct from `BUSINESS_VERIFIED` and every V3.1+ status —
  a discovery-only candidate never overloads or gets confused with a
  `full_pipeline` lead's richer state.
- A durable, date-keyed cost ledger (`data/runtime/cost/<date>.json`,
  gitignored) makes "one shared daily budget" real across separate
  process invocations — a scheduled run, a same-day catch-up, and a
  manual retry all draw from the same allowance.

## V3.8.2: cost-guard hardening

A first live validation of V3.8.1 (against a small, deliberately
budget-capped test run) surfaced three real defects, found the way most
interesting bugs are found — by actually running the thing:

1. A Claude call that *fails* can still be billable. One validation call
   hit its own per-call budget circuit breaker mid-research and still
   incurred a real charge — which the daily ledger never recorded,
   because the ledger was only ever written to after a *successful* call.
2. The call cap counted only successes, not attempts — meaning a failed
   call didn't count against `max_claude_calls_per_run` at all.
3. The worker's runtime ceiling was checked only *between* market cells,
   never against an in-flight call — actual wall-clock time exceeded the
   configured cap because a call already in progress had no awareness of
   how much runtime remained.

V3.8.2's fix, in outline (see `reports/V3.8.2-COST-GUARD-HARDENING-REPORT.md`
for the full detail):

- **Reserve before spawn.** A call-budget slot and a `PENDING` ledger
  entry are both written *before* the real subprocess starts — a
  crash/kill/timeout between reservation and completion still leaves an
  honest, unknown-cost trace rather than losing the attempt from
  accounting entirely. Never refunded, regardless of outcome.
- **Recover cost from failures.** Every exception this pipeline's Claude
  invocation layer raises now carries whatever real cost/usage data the
  failed call's own output exposed (a `--max-budget-usd` trip commonly
  still emits a complete envelope reporting real cost before exiting
  non-zero) — recorded in the ledger exactly like a success's cost.
- **Conservative accounting under uncertainty.** When a failure's cost is
  genuinely unrecoverable, the day's accounting is marked explicitly
  incomplete, the remaining-budget figure becomes unknown rather than a
  false precise number, and the call cap remains the enforceable
  backstop.
- **Clamp the subprocess timeout to remaining runtime**, and never start a
  new call if less than a configurable minimum window remains. A call
  that is genuinely killed by the worker deadline is recorded, classified
  distinctly from an ordinary timeout, never retried, and stops the
  entire run — not just that one market cell.

This fix was itself verified with a second live validation reproducing
the original failure conditions on purpose: the hardened worker correctly
counted the failed attempt against its call cap, correctly marked that
day's cost accounting incomplete rather than inventing a number, correctly
clamped the second call's subprocess timeout to the runtime that actually
remained, and stopped the run cleanly with no retry once that clamped
timeout was reached.

## Where things stand

The scheduled default is `discovery_only`. Every stage described in
V3.1–V3.8 above still exists, is still tested, and remains available
under the explicit `full_pipeline` mode — this project's philosophy is
that expensive capability should be a deliberate choice, not a default a
timer makes for you.
