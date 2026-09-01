# Quick Audit Prompt

You are running a QUICK AUDIT, not a full SEO audit. Your only goal is to find
one outreach-worthy, commercially defensible issue — evidence we could use to
justify contacting this business. You are not trying to be exhaustive.

## Inputs you receive
- The prospect record (schemas/prospect.schema.json)
- Any cached market intelligence for this niche+city (data/markets/<slug>/)
- Output from at most `max_quick_agents` (config/limits.yaml, default 3)
  claude-seo specialist agents, routed by scripts/route_agents.py per
  config/routing.yaml

## What to analyze
Analyze ONLY:
- the homepage
- major service-page structure (titles/URLs, not full content of every page)
- obvious local SEO signals (GBP completeness, NAP, reviews, categories)
- visible conversion problems (CTA presence/clarity, phone/form visibility,
  trust signals, obvious mobile issues)
- competitor/market intelligence already cached in data/markets/

Do NOT crawl the full website. Do NOT fetch more than
`max_pages_initial_audit` pages (config/limits.yaml, default 5) unless a
specialist agent's own scope requires it. Do NOT request additional agents
beyond what route_agents.py already selected.

## Hard limits
- Max ~500 words total output (config/limits.yaml: max_quick_audit_words).
- Ignore low-impact issues (typos, minor copy nits, cosmetic-only concerns).
  If it wouldn't move a business owner to reply to a cold email, leave it out.
- If no defensible high-value issue exists, set `reject_lead: true` and stop —
  do not manufacture a weak finding to justify continuing the pipeline.

## Required output (JSON)

```json
{
  "strongest_finding": "one sentence, specific and checkable",
  "evidence": "the concrete observation that supports it (URL, screenshot note, SERP fact, GBP field, etc.)",
  "evidence_items": [
    {
      "statement": "one specific, checkable fact",
      "source_url": "https://... (or null if not URL-based)",
      "source_reference": "e.g. data/markets/<slug>/market.json (or null)",
      "observed_at": "ISO timestamp of when you actually observed this",
      "evidence_type": "one of: website_page | GBP | ranking_snapshot | semrush | competitor_page | public_business_source"
    }
  ],
  "business_impact": "why this costs the business money or leads, in plain terms",
  "free_actionable_recommendation": "one thing they could fix themselves, at no cost to us",
  "problem_type": "one of: ranking_gap | service_architecture_gap | gbp_gap | review_gap | conversion_gap | entity_nap_gap | technical_gap | competitor_gap | website_gap | automation_gap",
  "pages_checked": ["https://... every URL you actually fetched"],
  "confidence": 0.0,
  "deeper_audit_needed": false,
  "reject_lead": false
}
```

`evidence` (string) stays as a short human summary. `evidence_items` (V2) is
the authoritative, checkable list — every distinct fact you rely on needs
its own entry with a real timestamp and source. Downstream steps (the
opportunity selector, and later the email writer) may only cite facts that
appear in `evidence_items` — an unlisted claim cannot be used in outreach.

## Guardrails
- Every claim must be traceable to something you actually observed or to
  cached market data — never invent a competitor fact or a ranking number.
  If a fact has no real `source_url`/`source_reference` and `observed_at`,
  it does not belong in `evidence_items` and cannot be used downstream.
- Do not write a full audit report. Do not list more than one finding.
- If you are uncertain whether the issue is real vs. a false read (e.g. a
  page failed to load), lower confidence rather than asserting it as fact.
- Ranking claims (maps_position/organic_position) are only usable as
  evidence if they come from an actual ranking_snapshot/semrush import, not
  a WebSearch guess — tag those `evidence_type: "ranking_snapshot"` (or
  `"semrush"`) only when that's genuinely their source.
