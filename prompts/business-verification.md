# Business Verification Prompt

Before a single point is scored, confirm the prospect record actually
describes ONE real, identifiable business — not a name collision between
two different companies, and not an assembly of facts pulled from
mismatched sources.

This step exists specifically to prevent cases like "Example Restoration" —
a name shared by multiple, unrelated local businesses across different
cities — from having one company's website merged with another company's
reviews, address, or ranking data.

## Input
The prospect record as discovered (schemas/prospect.schema.json), plus
whatever public evidence you gathered or can gather (website, GBP listing,
BBB/Yelp/Angi profiles, phone/address records, social profiles).

## Task
Cross-check the fields against EACH OTHER, not just against reality in
isolation. Ask: does the phone number's registered address match the
business's claimed city? Does the website's "About" copy name the same
business as the GBP/directory listing? Is there more than one same-named
business in this niche, and if so, did you confirm which one this record's
signals actually belong to?

You do not need every field to be present — a business can be verified from
website + phone + address alone even if a GBP URL is missing. But do not
assume two records agree just because they weren't checked against each
other.

## Required output (JSON, schemas/verification.schema.json)

```json
{
  "prospect_id": "",
  "business_verified": true,
  "identity_confidence": 0.0,
  "matched_fields": ["phone matches BBB listing address", "website matches GBP business name"],
  "conflicting_fields": [],
  "source_notes": ["BBB profile URL", "live site fetch"]
}
```

## Guardrails
- If you found more than one business with this name in a plausible
  location, say so explicitly in `conflicting_fields` and lower
  `identity_confidence` accordingly rather than picking one arbitrarily.
- Do not inflate confidence to help a lead move forward — this gate exists
  specifically to catch cases that would otherwise silently corrupt a
  dossier or, worse, an outreach email sent to the wrong company's contact.
- If you cannot verify the business is a real, currently-operating entity at
  all, set `business_verified: false` regardless of confidence score.
