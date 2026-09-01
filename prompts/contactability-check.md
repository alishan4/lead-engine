# Contactability Pre-Check

This is **NOT** final contact verification (that's `prompts/contact-verification.md`,
run later at `CONTACT_VERIFICATION`). This is a cheap, early reachability
gate to avoid spending a quick audit / dossier / full verification effort
on a business with no realistic contact path at all.

## Task
Do a quick, bounded check: is there a plausible path to a real person or a
real inbox at this business? You are not confirming an exact email address
here — just whether one is likely to exist and be findable later.

## Required output (JSON)

```json
{
  "prospect_id": "",
  "contactability_score": 2,
  "named_owner_found": true,
  "named_marketing_contact_found": false,
  "named_ops_contact_found": false,
  "official_email_visible": true,
  "official_contact_form_available": true,
  "likely_contact_role": "owner",
  "evidence": [
    "specific, checkable observation with a source"
  ]
}
```

## Scoring

- **2** — a named decision-maker (owner/founder/marketing manager/ops
  manager) AND/OR an official, visible email/contact path was found on a
  credible source.
- **1** — a plausible, legitimate contact path exists (e.g. a working
  contact form, a listed phone with an apparent front-desk/office
  presence) but no named person or visible email yet — final
  identity/mailbox verification will still be required later.
- **0** — no realistic contact path found after a reasonable, cheap check
  (no form, no visible email, no named person, nothing).

## Guardrails
- **Do not guess or construct an email address here.** `official_email_visible`
  means you actually saw one displayed somewhere, not that you inferred a
  likely pattern.
- **Do not do SMTP/deliverability validation here** — that's a later stage.
- This is a cheap gate, not a deep search — a few minutes of checking the
  site's contact/about pages and the GBP listing is enough; don't chase a
  contact through multiple directories at this stage.
