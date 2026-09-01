# Contact identity verification (V3.3)

Research the real, currently-live contact options for one business. This is
re-verification, not a rubber stamp -- even if an earlier phase already
recorded a contact, check it again now; people leave, addresses go stale,
and V3.3 is a higher-stakes stage (it can lead to an actual send) than
earlier stages were.

Find, for the business below:
1. A named decision-maker (owner/founder/partner/managing attorney/office
   manager/marketing lead) with an email address **displayed on a page you
   can point to** (their own site's team/about/contact page, a bar/license
   record, a company document, or an equivalently credible primary source).
2. Failing that, a company inbox email address **displayed on the business's
   own site** (not an aggregator's guess).
3. Failing that, a working contact form URL on the business's own site.

Hard rules -- identical in spirit to V2's verify_contact.py and V3.1.1's
"UNKNOWN != FALSE":
- Never infer `info@domain`, `hello@domain`, `contact@domain`, or any
  local-part pattern from a bare domain. If you did not see it displayed
  somewhere, it is not evidence.
- An address that appears ONLY in a third-party aggregator/scraper listing,
  with no primary-source page to point to, is NOT verified -- record it
  under `rejected_evidence`, not `sources`.
- If a previously-recorded contact still checks out, say so explicitly and
  re-cite the source with today's observed_at date -- do not silently reuse
  a stale timestamp.
- If nothing changed and nothing new was found, that is a valid, completely
  normal result. Do not manufacture a new signal to make the record look
  more complete.

Output one JSON object:
```json
{
  "person_name": "<string or null>",
  "role": "<string or null>",
  "email": "<string or null>",
  "sources": [{"source_type": "...", "url": "...", "observed_at": "...", "note": "..."}],
  "rejected_evidence": [{"source_type": "...", "url": "...", "note": "why rejected"}],
  "has_contact_form": true,
  "contact_form_url": "<string or null>",
  "mailbox_hint": null
}
```
`mailbox_hint` stays null unless you have a real, already-existing
deliverability signal (e.g. a documented prior bounce) -- never guess it.
