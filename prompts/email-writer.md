# Email Writer Prompt

Write ONE outreach email draft. V2 never sends this — it is saved to
data/leads/<slug>/email.json for human review, and only reaches
`EMAIL_DRAFT_READY` after a verified contact exists and QA passes.

## Input
Use ONLY the dossier (data/leads/<slug>/dossier.json,
schemas/dossier.schema.json). Do NOT re-audit the site, do NOT fetch the
website again, do NOT call any claude-seo agent from this step. **Every
factual claim must be backed by a specific entry in `dossier.evidence_items[]`
— if a fact isn't in that array, it cannot appear in the email, even if it's
true and even if it's sitting right there in `source_notes` or another
field.** This is what the V1 A-Action rewrite loop caught: true facts pulled
from the wrong place still fail QA.

## Structure (in this order)
1. First 2-3 sentences prove real, business-specific research (name a
   concrete fact from `evidence_items` — a service, a location detail, a
   specific finding — not a template compliment).
2. Lead with the strongest finding (`dossier.strongest_finding`).
3. Explain why it matters in business terms (`dossier.business_impact`) —
   describe the mechanism (what a searcher/customer would experience), not
   an unsupported causal leap.
4. Give one useful, free recommendation (`dossier.free_value`) — no strings
   attached.
5. Low-friction CTA — vary the exact wording, but keep the shape: e.g.
   "I mapped the other differences I found. Want me to send it?" or
   "Want me to send the short comparison?"

## Hard rules
- 120–180 words total (config/limits.yaml: max_email_words is the ceiling;
  don't pad to reach it).
- No generic openers like "I noticed your SEO needs improvement."
- Never guarantee rankings. Never say anything like "we can get you top 3"
  or any specific rank promise.
- **Do not state an exact ranking position** (e.g. "you're #14 for X")
  **unless** `dossier.ranking_observed_at` is present and fresh (this is
  what makes the claim a dated fact, not a guess) — otherwise describe the
  gap qualitatively ("you don't show up on page 1 for...") instead of citing
  a number.
- **Do not cite a review count** unless it traces to an `evidence_items`
  entry with its own source and date — same rule as any other fact.
- Do not make causal claims from correlation ("this is why you're losing
  customers") — describe the mechanism you actually observed, not an
  inferred outcome.
- Never say "Google is penalizing you" or similar algorithmic-punishment
  framing — it's rarely accurate and reads as a scare tactic.
- Never claim a specific revenue-loss figure unless it's directly
  supportable by an evidence item (in practice: essentially never for V2).
- Do not ask for a meeting/call in this draft.
- Do not mention price, packages, or "SEO services" generically — this is a
  research-led note, not a pitch.
- Every factual sentence must map to a specific `evidence_items` entry. If
  you can't point to the entry, cut the sentence.

## Output (JSON, schemas/email.schema.json — subject + body only; qa is filled
in by the separate QA step)

```json
{
  "prospect_id": "",
  "subject": "short, specific, no clickbait",
  "body": "the email text",
  "word_count": 0
}
```
