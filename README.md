# Lead Engine (V2 + V3.1 qualification layer)

Cost-optimized local SEO lead intelligence engine. Finds commercially valuable
local-service businesses (roofing, HVAC, restoration, plumbing, dentistry,
family law, etc.), verifies their identity, scores them with cheap rules
before any AI is involved, fills in missing ranking data cheaply when that's
the only thing blocking qualification, runs a capped, targeted claude-seo
audit only on qualified leads, verifies a real contact before drafting, and
stops the moment a lead is weak, dominant, unverifiable, stale, or
low-confidence. Produces one evidence-backed dossier and one QA'd draft
outreach email per approved lead, plus a local (never sent) Gmail-draft
export.

**V2 does not send email, does not integrate Gmail sending, and never runs
the full claude-seo specialist roster on a lead.** It stops at `QA_PASS`.

## Pipeline

```
DISCOVERED → BUSINESS_VERIFIED → INITIAL_SCORE
                                       ↓
                    ┌── REJECTED (hard rule / low score / bad identity)
                    ├── NEEDS_ENRICHMENT → [import ranking data] → RESCORED ──┐
                    └── MANUAL_REVIEW (55-69, no material data missing)      │
                    └── QUALIFIED ←───────────────────────────────────────────┘
                            ↓
                       QUICK_AUDIT → (REJECTED | OPPORTUNITY_IDENTIFIED)
                            ↓
                      DOSSIER_READY → (REVERIFY_REQUIRED if stale)
                            ↓
                   CONTACT_VERIFICATION → CONTACT_UNVERIFIED (stop)
                            ↓
                     CONTACT_VERIFIED → EMAIL_DRAFT_READY → QA_PASS
```

Every REJECTED / MANUAL_REVIEW / CONTACT_UNVERIFIED / REVERIFY_REQUIRED exit
is a real stop, visible in `scripts/triage_report.py`. Nothing downstream of
a stop runs for that lead until a human (or a cheap enrichment/re-check step)
resolves it.

## V3.1: which businesses reach expensive analysis

V3.1 is layered on top of V2, active only via `qualify_leads.py --v3` (the
default, no-flag path is still pure V2, byte-for-byte unchanged). It answers
a different question than V2's single blended score: not "does this
business have an SEO problem" but **"is this a commercially attractive
business, AND does it have a defensible Google/local opportunity, AND can
we actually reach someone there?"** A business with terrible SEO does not
qualify merely because its SEO is terrible.

```
DISCOVERED → BUSINESS_VERIFIED → [franchise/corporate check]
     → COMMERCIAL_FIT_ASSESSED (partial) → BUYING_SIGNALS_ASSESSED
     → CONTACTABILITY_CHECK → GOOGLE_GAP_ASSESSED → FIT_SCORED (final)
     → GAP_SCORED → REJECTED | NEEDS_ENRICHMENT | MANUAL_REVIEW
                   | QUALIFIED | HIGH_PRIORITY
```

Franchise stop states: `FRANCHISE_REVIEW_REQUIRED` (uncertain — human call),
`CORPORATE_MARKETING_LOCK` (confirmed the location can't buy its own local
SEO), `LEAD_GEN_NETWORK` (it's a directory/aggregator, not a real single
business). None of these fire from a brand-name match alone — see
`config/franchise_blocklist.yaml`'s header comment.

**FIT** (0-100: niche economics + business maturity + buying intent +
contactability + market attractiveness) measures commercial attractiveness
and has no term derived from SEO/website quality at all. **GAP** (0-100:
Maps/GBP + organic visibility + service architecture + review gap +
technical + competitor gap) measures the Google/local opportunity, fully
deterministically, with the same confirmed/potential/completeness
discipline as V2 — unknown rankings are never scored as poor rankings.
`QUALIFIED` requires both FIT ≥ 40 and GAP ≥ 40; `HIGH_PRIORITY` additionally
requires FIT ≥ 65, GAP ≥ 50, a real contact path, a confirmed buying/timing
signal, and an evidence-backed `why_now`/`why_likely_buyer` — GAP size alone
never promotes a lead. Full reasoning and thresholds:
`reports/V3.1-QUALIFICATION-REPORT.md`.

V3.1 never calls a claude-seo agent — every step here is either pure
deterministic Python or a bounded, cheap research prompt (franchise
ambiguity, buying signals, the contactability pre-check — distinct from,
and cheaper than, `verify_contact.py`'s later full verification).

### Running V3.1 (per lead, from `lead-engine/`)

```bash
python3 scripts/verify_business.py --id <slug> --save verification.json   # unchanged V2 step
python3 scripts/check_franchise.py --id <slug>                            # free unless a blocklist match escalates it
python3 scripts/assess_commercial_fit.py --id <slug>                      # partial pass -> COMMERCIAL_FIT_ASSESSED
python3 scripts/assess_buying_signals.py --id <slug> --print-prompt       # then --save signals.json
python3 scripts/check_contactability.py --id <slug> --print-prompt        # then --save contactability.json
python3 scripts/assess_google_gap.py --id <slug>                          # fully deterministic -> GOOGLE_GAP_ASSESSED
python3 scripts/assess_commercial_fit.py --id <slug>                      # final pass -> FIT_SCORED
python3 scripts/qualify_leads.py --v3                                     # routes every FIT_SCORED lead
```

A `QUALIFIED`/`HIGH_PRIORITY` result then continues into V2's unchanged
`route_agents.py` → quick audit → `build_dossier.py` → `verify_contact.py`
→ `generate_email.py` → `qa_email.py` flow exactly as before.

## What's new in V2 (see `reports/V2-COST-REVIEW.md` for the full writeup)

V1 proved the cost model but exposed one bottleneck: **good leads stalled at
MANUAL_REVIEW purely because Maps/organic ranking data was missing** — not
because the lead was actually weak. V2 fixes this without weakening any
threshold or requiring a paid API:

1. **Business verification** (`verify_business.py`) runs before scoring, to
   catch name collisions (e.g. multiple businesses named "Regal
   Restoration") before their mismatched evidence gets scored as one company.
2. **Data completeness is now tracked explicitly.** Every score has a
   `confirmed_score` (known fields only — missing data is worth zero points,
   never a penalty), a `potential_score` (best case if missing fields turned
   out favorable), and a `data_completeness` percentage. `score_leads.py`
   also fixes a real V1 bug where a `null` `service_page_count` was silently
   treated as `0` (a confirmed weakness) instead of "unmeasured".
3. **`NEEDS_ENRICHMENT`**: if `confirmed_score` misses the qualified bar but
   `potential_score` clears it, and the gap is a material field
   (`maps_position`/`organic_position`), the lead routes to cheap enrichment
   instead of MANUAL_REVIEW or REJECTED — still zero AI calls.
4. **A file-based ranking import** (`import_rankings.py`) normalizes a
   Semrush export, a manual CSV, or a JSON snapshot into one schema, no paid
   API required. `rescore_leads.py` then deterministically re-scores using
   that data — still zero AI calls.
5. **Every finding now carries structured evidence** (`evidence_items[]`:
   statement + source + timestamp + type) instead of a free-text string, and
   `check_freshness.py` blocks outreach built on stale evidence
   (`finding_freshness_days` / `ranking_freshness_days`).
6. **Contact verification is a hard gate.** No email is ever drafted for a
   guessed address — `verify_contact.py` and `qa_email.py` both enforce this
   in code, not just in the prompt.
7. **A local, never-sent Gmail-draft export** (`export_gmail_drafts.py`)
   for QA-PASS, contact-verified leads only. `send_status` is always
   `"DRAFT_ONLY"` — nothing in this repo ever calls the Gmail API.

## Why this is cheap (V1 controls, all preserved)

1. **Rule-based scoring runs first**, entirely in Python, zero network/AI
   calls. Hard rejects and low scores never reach an AI call.
2. **Quick audit is capped at 3 specialists** out of the ~18 in claude-seo
   (`config/limits.yaml: max_quick_agents`, `config/routing.yaml`).
3. **Market intelligence is cached per niche+city** (`data/markets/`) and
   reused across every lead in that market.
4. **Dossiers are cached by content hash** (`cache_days`, default 14).
5. **A confidence gate blocks weak outreach**: opportunity confidence below
   `min_opportunity_confidence` (0.75) rejects before a dossier is built.
6. *(new)* **Enrichment is file-based, not a live paid API call** — ranking
   data comes from a CSV/JSON you already have, imported once per market and
   reused across every lead in it.

## Division of labor: Python vs. Claude

The scripts in `scripts/` are deliberately dumb — they do arithmetic, file
I/O, caching, routing, and threshold decisions, and they never call an LLM
API directly. The reasoning steps (business verification, quick audit,
opportunity selection, contact verification, email writing, email QA) are
prompts in `prompts/`, meant to be run **by Claude** (this session, or a
`claude-seo:*` subagent via the `Agent` tool) with the relevant script's
`--print-prompt` output as input. The corresponding `--save` flag then
validates, applies hard guardrails (e.g. "a guessed email can never be
verified", "an unsupported claim can never PASS QA"), and persists whatever
structured JSON Claude returns.

## Directory map

```
config/      niches, scoring weights/thresholds, agent routing table, cost limits
data/
  prospects/ discovered.jsonl, qualified.jsonl, rejected.jsonl,
             manual_review.jsonl, needs_enrichment.jsonl
  markets/   <niche>-<city>-<state>/market.json — reusable competitor + ranking intel
  rankings/  <market_id>.csv — normalized ranking snapshots (Semrush/manual/JSON)
  leads/     <prospect-id>/{agent_plan_quick.json, quick_audit.json, opportunity.json,
             dossier.json, contact.json, email.json}
  outreach/  ready-for-draft.jsonl — DRAFT_ONLY export, never sent
scripts/     orchestration CLIs (see below)
schemas/     JSON Schemas for every artifact the pipeline produces
prompts/     the 6 prompts Claude follows for the AI-driven steps
reports/     report_pipeline.py / triage_report.py output + cost reviews
tests/       unit tests (V1 scorer + V2 completeness/enrichment/verification/QA), no network, no AI
```

## Running the pipeline

All commands run from `lead-engine/`.

### 1. Import discovered prospects

Append one JSON object per line to `data/prospects/discovered.jsonl`,
matching `schemas/prospect.schema.json`. Unknown fields must be `null`, not
omitted.

```bash
python3 -c "
import json
p = {
  'id': 'roofing-charlotte-nc-example-co', 'business_name': 'Example Roofing Co',
  'city': 'Charlotte', 'state': 'NC', 'country': 'US', 'niche': 'roofing',
  'website': 'https://example.com', 'google_business_profile_url': None,
  'maps_position': None, 'organic_position': None, 'rating': 4.4, 'review_count': 22,
  'years_in_business': 6, 'obvious_website_issue': ['thin_service_pages'],
  'obvious_gbp_issue': ['few_photos'], 'service_page_count': 2,
  'competitor_gap': ['no storm-damage landing page vs. top 2 competitors'],
  'commercial_value_signal': 'high', 'verified_business': True,
  'source_notes': 'manual entry', 'discovered_at': '2026-09-01T00:00:00+00:00',
  'last_audited_at': None, 'content_hash': None, 'status': 'DISCOVERED'
}
with open('data/prospects/discovered.jsonl', 'a') as f:
    f.write(json.dumps(p) + '\n')
"
```

### 2. Verify business identity

```bash
python3 scripts/verify_business.py --id roofing-charlotte-nc-example-co --print-prompt
# Claude researches and answers per schemas/verification.schema.json, then:
python3 scripts/verify_business.py --id roofing-charlotte-nc-example-co --save verification.json
```

Sets status to `BUSINESS_VERIFIED`, `MANUAL_REVIEW` (probable name collision,
needs a human), or `REJECTED` (not a real/verifiable business), per
`config/limits.yaml: min_identity_confidence` (default 0.75).

### 3. Score

```bash
python3 scripts/score_leads.py
```

Rule-based, no AI. Computes `confirmed_score`, `potential_score`,
`data_completeness`, and `missing_fields`, and sets status to
`INITIAL_SCORE` (or `REJECTED` for hard rejects).

```bash
python3 scripts/qualify_leads.py
```

Routes each `INITIAL_SCORE` record to `qualified.jsonl` (confirmed ≥70),
`needs_enrichment.jsonl` (confirmed <70 but potential ≥70 and missing
maps/organic position), `manual_review.jsonl` (55-69, nothing material
missing), or `rejected.jsonl` (<55). No AI is called for any of these.

### 4. View NEEDS_ENRICHMENT

```bash
python3 scripts/triage_report.py
```

Shows every blocked lead (`NEEDS_ENRICHMENT`, `MANUAL_REVIEW`,
`CONTACT_UNVERIFIED`, `REVERIFY_REQUIRED`) with its confirmed/potential
score, completeness, missing fields, recommended next action, and an
estimated cost category (`deterministic` / `cheap_enrichment` /
`claude_quick_audit` / `manual_verification`).

### 5. Import a Semrush CSV export

```bash
python3 scripts/import_rankings.py --market roofing-charlotte-nc --file semrush_export.csv
```

Auto-detects a Semrush "Organic Research > Positions" export (by its
`Keyword`/`Position` columns) and normalizes it into
`data/rankings/roofing-charlotte-nc.csv`.

### 6. Import a manual ranking CSV

```bash
python3 scripts/import_rankings.py --market roofing-charlotte-nc --file manual_rankings.csv
```

Any CSV using the canonical column names directly (`business_name`, `domain`,
`keyword`, `organic_position`, `maps_position`, ...) works with zero
mapping — or a JSON snapshot (`--file snapshot.json`, a list of objects).
No paid API is ever required.

Optionally roll the import into the market cache too:

```bash
python3 scripts/enrich_market.py --market roofing-charlotte-nc
```

### 7. Rescore after enrichment

```bash
python3 scripts/rescore_leads.py --id roofing-charlotte-nc-example-co
python3 scripts/qualify_leads.py   # finalize routing with the new score
```

Matches the prospect by domain/name against the imported ranking data, fills
`maps_position`/`organic_position` if found, and fully recomputes the score
— tracking `score_before_enrichment`, `score_after_enrichment`,
`enrichment_fields_added`, `enrichment_source`, `enrichment_observed_at`.
Still zero AI calls.

### 8. Route agents and run the quick audit

```bash
python3 scripts/route_agents.py --id roofing-charlotte-nc-example-co
```

Infers problem types and prints/saves a capped agent plan (≤3 agents,
`config/routing.yaml`) to `data/leads/<id>/agent_plan_quick.json`. In this
Claude Code session, invoke each planned agent (e.g.
`subagent_type: "claude-seo:seo-local"`) per `prompts/quick-audit.md`'s
scope (homepage + up to 5 pages, ≤500 words), citing every fact in
`evidence_items[]` with a real source and timestamp. Save the result to
`data/leads/<id>/quick_audit.json`, then apply
`prompts/opportunity-selector.md` to pick exactly one opportunity and save
`data/leads/<id>/opportunity.json`.

### 9. Build the dossier

```bash
python3 scripts/build_dossier.py --id roofing-charlotte-nc-example-co \
  --agents-used claude-seo:seo-local claude-seo:seo-content
python3 scripts/check_freshness.py --id roofing-charlotte-nc-example-co
```

`build_dossier.py` is the cache/early-stop gate (reuses a fresh dossier by
content hash, rejects if opportunity confidence is below threshold).
`check_freshness.py` blocks a stale dossier (`REVERIFY_REQUIRED`) from
proceeding to contact verification / email generation.

### 10. Verify contact

```bash
python3 scripts/verify_contact.py --id roofing-charlotte-nc-example-co --print-prompt
python3 scripts/verify_contact.py --id roofing-charlotte-nc-example-co --save contact.json
```

Sets `CONTACT_VERIFIED` only if a real email was found on a public source
(`config/limits.yaml: min_contact_confidence`, default 0.8) — a guessed
address (e.g. assuming `info@domain.com`) can never verify, enforced in
code. Otherwise stays `CONTACT_UNVERIFIED` and outreach is blocked.

### 11. Generate the email draft

```bash
python3 scripts/generate_email.py --id roofing-charlotte-nc-example-co --print-prompt
python3 scripts/generate_email.py --id roofing-charlotte-nc-example-co --save draft.json
```

Requires a fresh dossier and a verified contact (or pass `--preview` for a
manual/internal-only draft that can never become `EMAIL_DRAFT_READY`).

### 12. QA

```bash
python3 scripts/qa_email.py --id roofing-charlotte-nc-example-co --print-prompt
python3 scripts/qa_email.py --id roofing-charlotte-nc-example-co --save verdict.json
```

`PASS` → `QA_PASS` (final V2 status). `REWRITE` → back to step 11.
`REJECT` → back to step 9 (evidence itself needs review). `REVERIFY_REQUIRED`
→ back to step 9's freshness check. An unverified recipient or an
unsupported claim can never PASS — enforced in code, not just the prompt.

### 13. Export ready-for-Gmail drafts (still never sent)

```bash
python3 scripts/export_gmail_drafts.py
```

Appends every QA-PASS, contact-verified lead to
`data/outreach/ready-for-draft.jsonl` with `send_status: "DRAFT_ONLY"`. No
network call, no Gmail integration — a human (or a separate tool) turns
these into actual drafts later.

### 14. Reports

```bash
python3 scripts/report_pipeline.py --date 2026-09-01   # funnel, agent usage, cache, QA pass rate
python3 scripts/triage_report.py --date 2026-09-01      # what's blocking each stuck lead
```

## Adding things

**A new niche** — add an entry to `config/niches.yaml`, including a `tier`
(1/2/3 — see `reports/V3.1-QUALIFICATION-REPORT.md` §4 for what tier means
for FIT scoring). No code changes needed; V2 scoring/routing and V3.1
FIT/GAP scoring all read this file.

**A new franchise/DSO/lead-gen-network brand** — add a lowercase substring
pattern under the right category in `config/franchise_blocklist.yaml`. A
match only triggers a research pass (never an auto-reject) to determine
whether the specific location controls its own marketing.

**A new city/market** — create `data/markets/<niche>-<city>-<state>/market.json`
(slug from `scripts/_lib.py: market_slug`). Populate `top_competitors`,
`review_benchmarks.median_top3`, and `common_service_architecture` at
minimum — `score_leads.py` uses the review benchmark automatically.

**A Semrush export** — `python3 scripts/import_rankings.py --market <slug>
--file export.csv`, then optionally `scripts/enrich_market.py --market
<slug>` to roll it into the market cache.

**Another claude-seo agent route** — add a `problem_type: {agents,
max_agents}` entry to `config/routing.yaml: quick_audit_routes` (and
optionally `deep_audit_routes`). Never-auto-run agents (paid API, rarely
relevant) go in `never_auto_run` instead.

## Future (not built in V2, documented only)

`APPROVED → SENT → REPLIED → AUDIT_SENT → CALL → PROPOSAL → WON → LOST`, and
any live Gmail send/schedule integration. No code in this repo implements or
assumes these; see `reports/V2-COST-REVIEW.md` for the recommended V3 scope.
