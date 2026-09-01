# Contact Verification Prompt

Find a real, publicly-sourced recipient for this lead's outreach email. This
step exists to make sure V2 never emails a guessed address.

## Input
The dossier (data/leads/<slug>/dossier.json) — business name, website, city.

## Priority order (stop at the first one you can actually verify)
1. Owner / founder
2. Marketing manager
3. Operations manager
4. A verified company inbox (e.g. an email actually published on the site's
   Contact page or footer — not a guess at its format)

## Hard rules
- **Never invent an email address.** Do not construct `info@domain.com` or
  `firstname@domain.com` just because it's a common pattern — that is a
  guess, not a verification, even if it turns out to be right.
- Only mark `contact_verified: true` if the exact email string was found on
  a public, credible source (the company's own site, their GBP listing,
  LinkedIn, a directory, a press release) — record that source in
  `source_url`.
- If you can only infer that an email probably exists (e.g. "they likely use
  a Gmail-hosted inbox") without finding the actual address published
  somewhere, that is NOT a verified contact. Set `contact_verified: false`
  and `source_type: "guessed"` or `"none"`.
- If `email_domain` doesn't match the business's own website domain, note it
  in `domain_matches_business: false` and lower confidence — could be a
  third-party inbox, a personal address, or a mismatch worth flagging.

## Required output (JSON, schemas/contact.schema.json)

```json
{
  "business_name": "",
  "contact_name": "",
  "role": "owner | founder | marketing_manager | operations_manager | company_inbox | null",
  "email": "the exact address found, or null if none verified",
  "email_domain": "",
  "source_url": "where this was found, or null",
  "source_type": "company_website | gbp | linkedin | public_directory | press_release | guessed | none",
  "domain_matches_business": true,
  "contact_verified": false,
  "verification_confidence": 0.0,
  "observed_at": "ISO timestamp",
  "notes": ""
}
```

## If nothing can be verified
Return `contact_verified: false`, `email: null`, `source_type: "none"`. This
is a valid, expected outcome for many small businesses — the pipeline stops
there (`CONTACT_UNVERIFIED`) rather than guessing. Do not lower your
standards just to produce a "complete" pipeline run.
