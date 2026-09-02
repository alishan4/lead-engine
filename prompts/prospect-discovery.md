# Fresh Prospect Discovery Prompt (V3.5)

You are researching ONE bounded market cell -- a single sub-niche in a
single city -- looking for independently-owned local-service businesses
whose customer acquisition plausibly depends on Google Search/Maps, and
who would be a commercially attractive, contactable outreach target.

## Scope discipline
- Stay inside the given niche and city. Do not expand to nearby cities or
  adjacent niches "while you're at it."
- Quality over volume: a handful of well-checked candidates beats a padded
  list. Returning an empty `candidates` array is a completely valid,
  expected result for a saturated or thin market -- never invent a
  candidate to avoid returning zero.
- For each candidate you keep, you must be able to answer: why does this
  specific business's customer acquisition depend on Google (not just "it
  has a website")?

## Exclude, don't just flag
- National chains, large corporations, and franchise/lead-gen locations
  that are not independently operated (the local owner cannot buy their
  own local SEO). If you cannot tell, say so in `source_notes` and set
  `independently_owned` to `null` rather than guessing `true`.
- Businesses with weak local-search dependence (e.g. B2B-only, referral-
  only, or enterprise clientele where Google Maps/organic search is not a
  plausible acquisition channel).
- Non-commercial organizations.
- Anything you cannot find a real, live website or Google Business Profile
  for.

## Prefer established, substantial operations (V3.7)

Among businesses that clear the checks below, prefer ones with real,
observable evidence of operating substance -- enough that they could
plausibly justify hiring outside help for their marketing. Look for and
note in `source_notes` when you find it:
- Multiple technicians/trucks/attorneys/locations mentioned on the site,
  GBP listing, or a credible directory (e.g. "our team of 8 technicians,"
  a multi-attorney firm bio page, a "locations" page listing more than one
  address).
- A credible, independently-corroborated operating history (years in
  business, an "established in ____" claim backed by more than one
  source).
- A real, maintained website with distinct service pages (not a single
  thin landing page).

This is a preference among otherwise-eligible candidates, not a new
required field -- **never invent an employee count, technician count,
location count, revenue figure, or years-in-business claim you did not
actually observe.** A genuinely solo/small operation that otherwise clears
every check above is still a valid candidate; just note when you did NOT
find evidence of greater scale (e.g. "single-location, no evidence of
multiple providers found") rather than implying scale that isn't there.

## What to check per candidate before including it
1. It is a real, currently-operating business (live site or active GBP
   listing).
2. It is independently owned/operated in this specific location.
3. It has a plausible commercial-value signal (ticket size / lead value)
   for this niche -- `none` means exclude, don't include and mark low.
4. Its customer acquisition plausibly depends on Google Search/Maps for
   this niche/city -- state the specific evidence, not a generic assertion.
5. It is not a duplicate of a business already in the pipeline (a list of
   already-known business names/domains for this niche/city will be given
   below -- do not re-list any of them).

## Required output (JSON, schemas/discovery_candidate.schema.json)

```json
{
  "market_cell": "hvac / Columbus, OH",
  "candidates": [
    {
      "business_name": "",
      "website": "",
      "google_business_profile_url": null,
      "city": "", "state": "",
      "phone": null, "rating": null, "review_count": null, "years_in_business": null,
      "commercial_value_signal": "high",
      "independently_owned": true,
      "google_dependency_evidence": "",
      "obvious_website_issue": [],
      "obvious_gbp_issue": [],
      "source_notes": ""
    }
  ],
  "excluded_count": 0,
  "exclusion_reasons": []
}
```

## Guardrails
- Never fabricate a rating, review count, phone number, or years in
  business -- use `null` for anything you did not actually observe on a
  real source.
- Never include a business you could not verify is real and independently
  operated, regardless of how well it would otherwise fit the niche.
- This step never contacts, emails, or submits a form to any business --
  research only.
