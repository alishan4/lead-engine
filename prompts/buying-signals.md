# Buying Signal / Why-Now Evidence Gathering (V3.1.1)

You are gathering **evidence**, not answers. Every signal you can support
must come back as one or more structured evidence objects
(`schemas/signal_evidence.schema.json`) — never a bare `true`/`false`. The
resolution engine (`scripts/signal_evidence.py`) decides the final value
from your evidence; your job is only to report what you actually observed,
with a source, a timestamp, and how confident you are that it's really
about THIS business.

This is **acquisition intent + growth + timing + commercial readiness**,
not just "do they run ads." Investigate the categories below, cheapest and
highest-value first, and stop once you have enough to be useful — do not
exhaustively chase all ten signals for every lead (see the cost-order note
at the end).

**UNKNOWN != FALSE.** If you can't find evidence either way, report nothing
for that signal_type — do not guess, and do not report a low-confidence
"probably not" as if it were a confirmed false.

## Per-signal rules — read before researching each one

### `runs_google_ads`
Acceptable evidence: an actual observed advertiser placement for this
business/domain on a live search, or an equivalent direct data source.
**Never infer from**: high CPC, Semrush keyword-volume data alone,
existence of a landing page, tracking-script presence, or "this niche
runs ads." If you cannot directly observe an ad, report nothing.

### `runs_lsa`
Acceptable evidence: an actual observed Local Services Ads
badge/placement for this business. **Never infer from**: being a
service-area business, having a GBP, appearing in Maps, or a competitor
running LSA.

### `recent_expansion` / `new_location`
Acceptable high-confidence evidence: an official location page, a company
announcement, official news, credible local news, or GBP evidence of a new
location — each must clearly reference a *recent* opening, not just
existence. A third-party directory listing **alone is not sufficient** —
directories are frequently stale/duplicated (report it as WEAK-confidence
supporting evidence at most, never your only source). **Multiple locations
existing does NOT prove recent expansion** — that's a separate, structural
signal (`multiple_locations`).

### `marketing_hiring_signal`
Look for current roles: marketing manager, digital marketing, growth,
business development, intake manager (where commercially relevant),
marketing coordinator, SEO/SEM, director of marketing. Evidence must come
from an official careers page or a credible current job-board posting.
**An old/cached posting is not current** — note the posting's own date if
shown; the resolution engine will discard it if it's past the freshness
window regardless of what you report.

### `review_velocity_signal`
**Do not report this from a single review-count observation.** This
requires two or more timestamped snapshots (`data/leads/<id>/review_snapshots.jsonl`,
via `scripts/record_review_snapshot.py`) and is computed deterministically,
not by your judgment — if fewer than two snapshots exist, the correct
answer is `UNKNOWN`, full stop. If you observe a current review count,
record it as a snapshot (call for that separately) rather than reporting a
velocity signal.

### `recent_site_investment`
Hard to evidence well — hold a high bar. Acceptable: an explicit
launch/redesign announcement, or a real historical-snapshot comparison
(e.g. Wayback Machine) showing a materially different prior version with a
dated capture. **Never infer from**: the site looking modern, a 2026
copyright notice, modern framework/asset fingerprints, or a recent
page-modified timestamp alone — all consistent with a template that was
simply never touched.

### `new_high_value_service`
Acceptable: an official announcement, a dated new-service-launch notice, a
historical-site comparison showing the service was absent before, or
credible news. **Never infer from**: the business currently offering a
valuable service — that's just their offering, not evidence it's new.

### `multiple_locations`
This one CAN be resolved with ordinary current evidence (official site,
GBP, government/licensing registry) showing N≥2 real distinct addresses —
no "recency" requirement, it's a structural fact about scale, distinct from
`new_location`/`recent_expansion`.

## Required output — one evidence object per finding

```json
{
  "prospect_id": "",
  "evidence": [
    {
      "signal_type": "runs_google_ads",
      "value": true,
      "confidence": 0.9,
      "source": "https://... (the specific page/search you observed this on)",
      "source_type": "google_serp",
      "observed_at": "ISO timestamp, right now",
      "published_at": null,
      "evidence": "the specific, checkable observation",
      "entity_match_confidence": 0.95,
      "notes": null
    }
  ]
}
```

Return an empty `evidence` array if nothing was found — that's a valid,
expected, honest result. Do not pad it with low-confidence guesses to look
thorough.

## Cost order — cheapest, highest-value evidence first

1. Existing cached evidence (market cache, prior dossier, prior evidence
   files) — check before researching anything new.
2. Official website (contact/about/careers pages).
3. Current contact/location pages.
4. Existing SERP/rank imports (`data/rankings/`) — do not re-derive what's
   already there.
5. Existing review snapshots.
6. A targeted, bounded live lookup (one or two searches) for the highest
   remaining-value signal.
7. Note what would require manual/import enrichment
   (`scripts/import_buying_signals.py`) instead of further live research.

**Stop once you have enough high-confidence evidence to support a routing
decision.** If FIT and GAP are already strong and you've confirmed one real
VERIFIED+ signal, do not keep spending research budget trying to prove six
more weaker ones.
