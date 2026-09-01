# Franchise / Corporate Marketing Control Check

This prospect's name or domain matched a known franchise/DSO/lead-gen-network
pattern (`config/franchise_blocklist.yaml`). **A match is not a verdict** —
many franchisees are independently owned and buy their own local marketing.
Your job is to determine whether *this specific location* controls its own
marketing spend, or whether a corporate/regional office does.

## Input
The prospect record, plus the matched blocklist pattern/category.

## What to determine
1. **`possible_franchise`** — is this location actually branded under the
   matched franchise/DSO, confirmed (not a coincidental name match)?
2. **`corporate_marketing_controlled`** — for this specific location, does
   corporate/regional/DSO marketing control local SEO spend, or does local
   ownership/management have its own budget and authority? Look for: does
   the location have its own distinct website/domain (a good sign of local
   control) or only a corporate-template subpage? Is there a locally-named
   owner/manager with apparent authority, or only a corporate contact?
3. **`lead_gen_network`** — is this "business" actually a directory/lead-gen
   platform rather than a real, single local business at all (e.g. it lists
   multiple unrelated companies, or redirects leads to different providers)?

## Required output (JSON)

```json
{
  "prospect_id": "",
  "possible_franchise": true,
  "corporate_marketing_controlled": false,
  "lead_gen_network": false,
  "evidence": [
    "specific, checkable observation with a source"
  ]
}
```

## Guardrails
- If you cannot determine `corporate_marketing_controlled` either way after
  a reasonable check, leave it `null` — this correctly routes to
  `FRANCHISE_REVIEW_REQUIRED` for human judgment rather than guessing.
- Do not assume franchise = corporate-controlled. Do not assume franchise =
  independently controlled. Determine it from actual evidence about this
  location.
