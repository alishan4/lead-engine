# Operating Rules V3 — Client Acquisition System

Supersedes the prior operating context. **Marked deltas only** — anything
not called out below is unchanged from the prior rules and remains in
force. See `reports/RED-TEAM-REVIEW-2026-09-01.md` for the reasoning
behind every change.

---

## UNCHANGED (confirmed sound, keep as-is)

- Workspace/project layout, claude-seo as an external dependency, never
  modified unless essential.
- Gmail used only per outreach rules; connectivity is not permission to
  send.
- Permanent pipeline rule: VERIFY BUSINESS → VERIFY PERSON/EMAIL →
  RESEARCH → INSPECT → IDENTIFY ONE OPPORTUNITY → FREE VALUE → PERSONALIZE
  → DRAFT → QA → SEND AT LOCAL TIME → TRACK REPLY → VALUE-ADD FOLLOW-UP →
  STOP ON REPLY.
- No-fabrication rule for rank, email, review count, traffic, revenue,
  CPC, years in business, location, services, licenses, certifications,
  customer count, competitor claims — enforced in code, not just prompts.
- Contact verification priority order (owner/founder → marketing manager →
  operations manager → verified inbox); never guess `info@`/`sales@`/
  `firstname@`/`firstname.lastname@`.
- Tuesday-Friday sending; Monday for research/enrichment/prep.
- Email QA verdicts: PASS / REWRITE / REJECT / REVERIFY_REQUIRED, and the
  "would this survive a company-name swap" REWRITE trigger.
- Bounce/reply monitoring architecture and per-lead failure isolation (one
  lead's failure never blocks the batch).
- Human handoff boundaries (Claude: pipeline, research, drafting, sending
  mechanics, reporting. Ali: strategy approval, sales conversations,
  proposals, pricing, closing, delivery).
- Weekly learning loop cadence (Friday metrics review) — now with an
  explicit note that until real send volume exists, every scoring/niche
  recommendation in this document is a hypothesis, not a proven default.

---

## DELTA 1 — Niche priority (re-ranked, not as originally given)

**Old**: PI → criminal/DUI → divorce/family → immigration, then
restoration/roofing/HVAC/dental/plumbing/etc.

**New wave-1 priority** (highest expected reply-rate × ticket-size, not
just ticket size):

1. Independently-owned restoration (water/fire/mold) — explicitly
   excluding franchise-affiliated locations (SERVPRO, etc., which already
   have corporate marketing).
2. Dental implants / cosmetic / emergency dentistry.
3. Immigration law.
4. Family/divorce law — solo-to-small firms (2-6 attorneys) outside
   top-20 metro markets.
5. Foundation repair / waterproofing.
6. Roofing (replacement/insurance-claim-focused, not repair-only) and HVAC
   (installation/replacement-focused, not repair-only).
7. Elder law / estate planning (NEW addition — high value, low outreach
   saturation).
8. Plumbing (repiping/sewer, high-ticket sub-segment).

**Deprioritized to wave-2, not removed**: Personal injury and criminal
defense/DUI law. Ticket size is real, but cold-inbox saturation from
existing legal-marketing agencies is very high and expected to suppress
reply rate. Re-test with real data before restoring to wave-1 — this is a
prediction, not a proven fact.

**Unchanged**: tree removal, electricians, garage doors remain lower
priority than the above given lower typical ticket size, unless targeting
commercial accounts specifically.

## DELTA 2 — Buying signals (new required fields)

Add to `schemas/prospect.schema.json` and score in `config/scoring.yaml`:

- `runs_paid_search` (bool/null) — only from an actually observed ad on a
  live SERP, never inferred or guessed.
- `paid_search_vs_organic_gap` (bool/null, derived) — true when
  `runs_paid_search` is true AND maps/organic position is weak or unknown.

These two signals should carry more scoring weight than a single
ranking-position bonus (see Delta 3) because they directly evidence both
willingness and ability to pay, not just the presence of a gap.

Other signals (review velocity, redesign detection, hiring, ownership
change, funding) are **explicitly not** required per-lead — too expensive
or too ambiguous at this stage. Do not build tracking for a signal without
a defined action tied to it.

## DELTA 3 — Scoring model changes

- Reduce `maps_position_4_to_15` and `organic_position_5_to_30` weights
  (ranking data should matter, not dominate — see red-team review §7 for
  exact suggested values).
- Add `runs_paid_search` and `paid_search_vs_organic_gap` as new weighted
  signals.
- `already_dominant` penalty now scales with partial evidence (3/4 or 2/4
  confirmed fields) instead of requiring all four simultaneously.
- **New tier: `HIGH_PRIORITY`** — `QUALIFIED` **and** a paid-search signal
  **and** a findable named decision-maker at discovery time. This tier
  gets the deeper audit path and jumps the outreach queue. Introducing
  this tier does not lower the bar for `QUALIFIED` — it raises the bar for
  who gets the *most* attention.
- Do not reward a lead for weakness alone: `QUALIFIED`/`HIGH_PRIORITY`
  still require an independent buying-ability signal, not just a gap.

## DELTA 4 — Contact verification: add a secondary path

`CONTACT_UNVERIFIED` remains a hard stop for automated email drafting —
unchanged, never send to an unverified mailbox. **New**: add
`CONTACT_FORM_READY` for `HIGH_PRIORITY` leads with no verifiable email but
a working contact form — routed to a **manual, human-triggered** form
submission, not automated. This does not weaken the no-guessing rule; it
recovers pipeline that was previously discarded outright.

## DELTA 5 — Agent routing (deterministic-first)

Before routing a `service_architecture_gap` to `seo-cluster`+`seo-content`,
or a `technical_gap`/`website_gap` to paired specialists: run the
equivalent deterministic check first (service-page-count diff against
`market.json`, sitemap/robots.txt fetch-and-validate, schema.org
presence/type detection). Only escalate to a single specialist agent when
the deterministic check is ambiguous or needs qualitative judgment.
**New stop rule**: if the first quick-audit agent call returns
`confidence < 0.5`, do not spend a second/third agent call chasing the
same problem_type — drop to `deeper_audit_needed: false, reject_lead` logic
instead.

## DELTA 6 — Reply-triggered deliverable (new required capability)

Before the next positive reply arrives, build the "send the comparison"
generator: a short, evidence-model-backed one-pager assembled from
`evidence_items[]` already gathered during the quick audit, plus the named
competitor comparison. This must exist and be tested **before** relying on
the CTA ("I mapped the other differences I found, want me to send it?")
in live outreach — the CTA is a promise the pipeline doesn't yet fulfill.

## DELTA 7 — Deliverability ramp (new, mandatory before scaling send volume)

- Start at **3-5 sends/day** for the first 1-2 weeks on the actual sending
  identity, not the target 8-15/day.
- Confirm SPF/DKIM/DMARC alignment on the sending domain before any ramp.
- Treat **>2-3% hard-bounce rate on a single day's batch** as a full-stop
  signal for that day, not just a per-contact reverify.
- Vary phrasing templates and stagger irregularity — do not rely on timing
  variation alone to avoid pattern-based spam detection.
- Minimize links/images in the first cold touch; save richer content
  (screenshots, comparison one-pagers) for the reply-triggered
  follow-through, where deliverability risk matters less.

## DELTA 8 — Offer sequencing (clarified, not new)

Never lead with "Local Growth System" before a paid engagement exists.
Sequence: free-value first email → reply-triggered comparison → a **narrow,
priced first engagement** (scoped audit-plus-roadmap or a 90-day sprint on
the one proven opportunity) → only after delivery does the conversation
expand toward the full system as a retainer.

## DELTA 9 — Research ordering (cost fix)

Run the free, no-research discovery-data pre-filter (niche fit, obvious
dead site, no commercial intent) **before** spending a multi-source
identity-verification research pass. Verification research should only
run on leads that already clear this free filter.

---

## STANDING PRINCIPLE (unchanged, restated)

This is not a mass-email system. Every lead must pass an explicit,
evidence-backed answer to "why are we contacting THIS exact business," at
every stage, with real financial/behavioral signal that they can and will
pay — not just that their SEO is imperfect. Quality gates that produce
zero volume are a data problem to solve (more/better enrichment sources,
broader discovery), not a reason to lower the gates.
