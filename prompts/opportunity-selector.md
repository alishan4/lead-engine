# Opportunity Selector Prompt

Your only question: **"What is the ONE strongest reason we should contact
this company?"**

## Input
The quick-audit JSON output (prompts/quick-audit.md) for one prospect, plus
the prospect record and any cached market file.

## Task
Select exactly ONE primary opportunity classification for this lead. Do not
output a list of issues — if the quick audit surfaced more than one
candidate, pick the one most likely to (a) be commercially relevant to the
business owner and (b) be something a competitor visibly does better.

Classifications (schemas/opportunity.schema.json):
`ranking_gap`, `service_architecture_gap`, `gbp_gap`, `review_gap`,
`conversion_gap`, `entity_nap_gap`, `technical_gap`, `competitor_gap`,
`website_gap`, `automation_gap`.

## Hard limits
- Max ~250 words (config/limits.yaml: max_opportunity_words).
- One opportunity only. Do not hedge with "there are also several other
  issues" — if it's worth mentioning, it should have won the selection.

## Required output (JSON, schemas/opportunity.schema.json)

```json
{
  "prospect_id": "",
  "finding": "one sentence, same substance as quick_audit.strongest_finding",
  "primary_opportunity": "",
  "supporting_evidence": "concrete, checkable — not a generic claim",
  "evidence": [
    {
      "statement": "carried forward from quick_audit.evidence_items, or narrowed to just the facts that support the WINNING opportunity",
      "source_url": null,
      "source_reference": null,
      "observed_at": "ISO timestamp",
      "evidence_type": "website_page | GBP | ranking_snapshot | semrush | competitor_page | public_business_source"
    }
  ],
  "business_consequence": "plain-terms cost to the business",
  "free_recommendation": "one no-strings-attached fix",
  "confidence": 0.0,
  "sources": ["urls or cache file paths this is grounded in"],
  "observed_at": "ISO timestamp this opportunity was determined",
  "pages_checked": ["carried forward from quick_audit.pages_checked"],
  "reject_lead": false,
  "deeper_audit_needed": false
}
```

`evidence` here is the array format (schemas/opportunity.schema.json), not
a single string — carry over only the `quick_audit.evidence_items` entries
that actually back the ONE opportunity you selected; drop anything that
supported a candidate you didn't pick. Every fact used in
`business_consequence` must appear here, with its own timestamp — this is
what lets the email writer and QA gate later verify nothing unsupported
sneaks into outreach.

## Reject / escalate rules
- If confidence would be below `min_opportunity_confidence`
  (config/limits.yaml, default 0.75), set `reject_lead: true` — do not pass a
  low-confidence opportunity downstream to dossier/email generation.
- Set `deeper_audit_needed: true` only if a specialist agent beyond the quick
  audit's routing would meaningfully raise confidence on an otherwise
  promising finding (see config/routing.yaml: deep_audit_routes). This should
  be rare — most leads should resolve at the quick-audit stage.
