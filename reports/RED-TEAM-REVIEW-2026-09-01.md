# Red-Team Review — Client Acquisition System — 2026-09-01

Reviewer stance: adversarial. Grounded in the actual codebase (`config/*.yaml`,
`schemas/*.json`, `scripts/*.py`) and the actual test results to date (5
leads run through V1+V2, twice, real fabrication-check production test on
Example Roofing), not the aspirational description of the system.

---

## 1. EXECUTIVE VERDICT

**The system is directionally sound but operationally unproven at zero
volume.** The engineering discipline (no fabrication, evidence-backed
findings, capped agent spend, contact verification) is genuinely rare and
is the real competitive asset here. But it has not yet produced a single
sendable email across two full test passes on five real leads. That is not
a rounding error — it's the headline fact this review has to start from.

**Three largest weaknesses:**

1. **The qualification funnel currently has a ~0% throughput rate in
   practice, and the system doesn't know why fast enough.** Of 5 real
   leads, 4 landed in `NEEDS_ENRICHMENT` (blocked on ranking data with no
   approved acquisition path for that data) and the 1 that scored
   `QUALIFIED` died at `CONTACT_UNVERIFIED`. Two separate hard gates
   (ranking data, contact data) are each individually strict and correct,
   but stacked, they may be strangling the top of the funnel. Nobody has
   yet answered "at what volume of real Semrush/Maps data does this
   actually produce 15 qualified leads/week?" — that's a data-acquisition
   problem hiding behind a scoring problem.
2. **Niche prioritization is asserted, not derived, and the #1 priority
   (personal injury / criminal defense law) is very likely the worst
   cold-email niche on the entire list.** PI and criminal defense/DUI are
   the single most heavily cold-marketed legal verticals in the US — a
   large, well-funded agency ecosystem (Scorpion, FindLaw/Thomson Reuters,
   PaperStreet, and dozens of PI-only shops) already saturates these
   inboxes. Reply propensity from an unbranded first-touch email in this
   niche is likely to underperform restoration, dental implants, or even
   family/immigration law by a wide margin, regardless of ticket size. See
   §5.
3. **The system has no answer yet for what happens after "sure, send it."**
   The CTA is the entire mechanism by which a cold email becomes a sales
   conversation, and it promises a specific deliverable ("the other
   differences I found," "the short comparison") that no script in
   `lead-engine` currently generates. This is a real, unbuilt gap between
   the outreach hook and the sales motion it depends on. See §12.

This is not a system that should be scaled up as-is. It should be run at
very low volume against a redesigned funnel (§4, §7) until it produces its
first 10 real qualified-contact-verified-QA-passed leads, and the numbers
from that run should drive every subsequent change — not more upfront
design.

---

## 2. WHAT TO KEEP

- **The no-fabrication discipline, enforced in code, not just prompts.**
  `exact_rank_verified`, the `NEVER_VERIFIED_SOURCE_TYPES` guard in
  `verify_contact.py`, the QA guards in `qa_email.py` that force REJECT on
  `facts_supported: false` regardless of what the LLM says — these are real
  defenses, not aspirational rules, and they've already been proven under
  pressure (they correctly refused to fabricate a Maps position for
  Example Roofing even with two real external data sources in hand).
- **`confirmed_score` vs. `potential_score` vs. `data_completeness`.** This
  is a genuinely good idea, rare in lead-scoring systems: it turns "why
  didn't this lead qualify" from a black box into an actionable, specific
  answer. Keep it, but see §7 for why the current weighting makes it less
  useful than it should be.
- **The 3-agent cap and problem-type routing.** Capping quick-audit spend
  and routing by inferred problem type instead of running everything is
  correct and should not be relaxed under volume pressure — it should
  probably get *tighter* (§8).
- **Market caching by niche+city.** Real, measurable reuse value
  (`data/markets/roofing-charlotte-nc/`, etc.) once a market is seeded.
- **The reconciliation discipline just demonstrated** (catching the
  `discovered.jsonl`/`qualified.jsonl` desync, the stale QA verdict) is
  exactly the kind of operational rigor a system handling real outreach on
  a live Gmail account needs. Keep running it, not just once.

---

## 3. WHAT TO REMOVE

- **`seo-cluster` as a default route for `service_architecture_gap`.**
  `score_leads.py` *already deterministically detects* thin service-page
  architecture (`service_page_count` vs. niche norm from `niches.yaml`).
  `seo-cluster`'s actual methodology (SERP-overlap topic clustering to
  design a hub-and-spoke content architecture) is built for planning a
  content strategy, not for confirming "this business has 3 pages, its
  competitors have 6." Running it here is expensive relative to the
  question being asked. Replace with a deterministic script that diffs the
  prospect's service-page URLs against `market.json`'s
  `common_service_architecture` and only escalate to `seo-content` (not
  `seo-cluster`) when the deterministic diff needs qualitative judgment
  about an *existing* page's thinness.
- **The 4-condition `already_dominant` penalty as currently written.** It
  requires `maps_position<=3 AND organic_position<=4 AND rating>=4.7 AND
  review_count>=50` *simultaneously* — in practice this almost never fires
  because most real records are missing at least one of those four fields
  (as every single test lead showed). It's dead code in the common case. It
  should trigger on partial evidence too (see §7).
- **The abstract "LOCAL GROWTH SYSTEM" framing as anything the first 3
  touches ever reference.** It's a good retainer-stage narrative, wrong for
  a first email or even a first call — see §9/§13.
- **Running full identity verification (BBB/Chamber/multi-source
  cross-check) on every discovered lead before any cheap score exists.**
  This is backwards for cost: verification research should happen *after*
  a free, no-research pre-filter on whatever's already known from
  discovery (niche fit, obvious dead site, no commercial intent), not
  before it. Right now a lead that would score 15 on pure discovery data
  still gets a full multi-source verification pass. See §8/§13.

---

## 4. WHAT TO CHANGE IMMEDIATELY

Before the next 100 emails (which, at current throughput, is theoretical —
before the *next 20 qualified leads*):

1. **Add a paid-ads / LSA signal to the prospect schema and scorer.**
   Nothing in `prospect.schema.json` or `scoring.yaml` currently captures
   whether a business runs Google Ads or Local Services Ads. This is very
   likely the single highest-value missing field in the entire system —
   see §6. Add `runs_google_ads` / `runs_lsa` (bool/null, never guessed —
   only from an actual observed ad on a SERP) with real scoring weight.
2. **Split `family_law` into practice-area sub-niches, and re-rank the law
   priority list.** `config/niches.yaml` currently has one flat
   `family_law` entry; there is no PI, criminal/DUI, immigration, or elder
   law entry at all despite PI/criminal being called "highest priority."
   Fix the data model before the strategy, then re-rank per §5.
3. **Loosen contact verification's dead end.** Right now
   `CONTACT_UNVERIFIED` is a full stop with no secondary path. Add the
   `CONTACT_FORM_READY` state the operating brief already named, route it
   to a *manual, high-priority-only* contact-form touch instead of
   discarding the lead — this is real recovered pipeline, not a
   compromise of the no-guessing rule.
4. **Build the reply-triggered deliverable generator now, not after the
   first "yes."** The CTA promises "the other differences I found" / "the
   short comparison." Nothing generates that document today. If the first
   positive reply arrives before this exists, the promise breaks in real
   time in front of a real prospect.
5. **Re-weight `already_dominant` and the ranking bonuses to accept partial
   evidence** (see §7) — the current all-or-nothing design is why every
   real test lead landed in the same 50-55 point band regardless of how
   different their actual situations were.
6. **Do not authenticate/scale Gmail sending until a bounce-safe warm-up
   plan exists** (§11) — the operating context already admits real bounces
   have happened; sending 15-20/day on a not-yet-warmed sending identity
   compounds that risk right when reputation matters most.

---

## 5. IDEAL CLIENT PROFILE

### Derived principle (not assumed)

Expected value of a cold-outreach target ≈
`ticket_size × P(reply | inbox_saturation) × P(close | engagement_quality)`.

Ticket size is the least differentiating of these three terms across the
niche list — every listed niche has a plausible path to a $2k-15k+
engagement. **Inbox saturation is where the list should actually be
re-ranked**, because it varies enormously and directly determines whether
any of the rest of the system's quality matters.

### Law firms — re-ranked, not accepted as given

| Practice area | Ticket size | Cold-inbox saturation | Recommended priority |
|---|---|---|---|
| Personal injury | High | **Very high** (dedicated agency ecosystem: Scorpion, FindLaw, PI-specific shops) | **Deprioritize as a first-wave niche.** Reply rate risk is high; a generic-sounding "I found a gap" email is exactly what this inbox already ignores by the dozen. |
| Criminal defense / DUI | Medium-high | High | Same logic — deprioritize relative to the alternatives below. |
| Family / divorce law | Medium-high | Medium | Keep, but target solo/small (2-6 attorney) firms outside the top-20 metro markets, where agency saturation is lower and the owner still personally reads email. |
| Immigration law | Medium-high | **Low-medium** (underserved by big legal-marketing agencies relative to PI) | **Promote.** Real, durable demand; smaller agency ecosystem; often self-managed marketing. |
| Elder law / estate planning | High (not on the original list) | Low | **Add to the list.** High per-client value, aging-population demand, low outreach saturation — an underexploited adjacent niche. |

**Ideal law-firm ICP**: 2-8 attorneys (solo excluded — often can't afford
retainer pricing; 10+ attorney firms often already have an agency or
in-house marketing), 20-150 reviews at 4.3+, a named managing partner or a
marketing coordinator (not just "the founding partner" for larger firms),
visible evidence of *some* paid search activity (a strong positive signal —
see §6), a clear single practice-area page gap (e.g., handles immigration
appeals but has no dedicated page for it), and — critically — **not** in
the 5 most saturated metro markets for that practice area, where the
agency-noise floor is highest.

### Restoration / roofing / HVAC

Our own real data already re-derived this ICP better than any assumption
could: **every independent restoration/roofing business we researched was
being outranked by national franchises (SERVPRO, Aire Serv, Coolray,
Happy Hiller)**. That's the actual finding, and it sharpens the ICP:

- **Target independently-owned operators specifically excluded from
  franchise/co-op marketing budgets** — not franchise-affiliated locations
  of the same brands, which already have corporate SEO.
- **Roofing**: bias toward companies whose service lines emphasize
  *replacement* and *insurance/storm-damage claims* (high ticket, urgency)
  over repair-only shops.
- **HVAC**: bias toward *installation/replacement*-forward businesses
  ($5k-15k systems) over repair-only.
- **Restoration**: this is the highest-urgency, highest-ticket niche on
  the entire list ($10k-100k+ insurance jobs) and probably deserves to be
  *promoted above* several of the law-firm sub-niches, not ranked below
  them by default.
- **Foundation repair/waterproofing**: very high ticket ($8k-30k+), high
  urgency, smaller total addressable business count per market — good
  quality target, expect lower volume per city.

### High-value healthcare / dental

Dental implants / cosmetic / emergency dentistry is a strong niche
specifically *because* it already skews toward practices that run Google
Ads (implant cases are expensive enough to justify real ad spend) — which
makes the missing ads-signal (§6) especially costly to not have here. ICP:
a practice with 1-4 dentists, visible cosmetic/implant service lines,
50-300 reviews, and (this is the tell) **paid ads running alongside weak
organic visibility** — the single cleanest "you're paying for what you
could get for less" pitch in the whole system.

---

## 6. BUYING-SIGNAL MODEL

Ranked by expected reply-rate lift per unit of research cost:

| Signal | Cost to detect | Priority | Why |
|---|---|---|---|
| **Running Google Ads / LSA** | Low (one SERP check) | **Critical — add now** | Proves budget + willingness to pay for leads + a marketing decision-maker already thinking about this exact problem. Currently entirely absent from the schema. |
| **Paid ads + weak organic** (combined) | Low | **Critical** | The single strongest cold-email angle available: "you're paying per click for traffic you could get organically." |
| **Reviews present but GBP under-optimized** (categories, photos, Q&A missing) | Low-medium (already partially built) | High | Legitimate business, cheap fix, believable. |
| **High review velocity recently** (many reviews in the last 90 days) | Medium (needs a timestamped pull, not just a count) | Medium-high | Signals active customer volume outpacing digital investment — good "you're busy but invisible" angle. Not currently tracked at all (schema only has a point-in-time `review_count`). |
| **Recent website redesign** (Wayback Machine diff) | Medium | Medium | Signals recent marketing spend/attention — could mean they're already primed for a pitch, or could mean they just signed with someone else. Ambiguous enough to not weight heavily alone. |
| **Multiple locations/providers** | Low (usually visible on discovery) | Medium | Correlates with ability to pay retainer pricing; already partially implicit in `years_in_business`/`service_page_count` but not scored directly. |
| **Recently opened / new location** | Medium | Low-medium | Interesting but double-edged — could mean no budget yet, could mean growth-mode budget. Don't overweight. |
| **Hiring marketing staff** (job postings) | High (needs a separate search per lead) | Low | Real signal, too expensive to check universally — reserve for tie-breaking on `MANUAL_REVIEW`, not blanket use. |
| **Agency dissatisfaction signals** (e.g. outdated agency badge, obviously templated site from a defunct vendor) | High, unreliable | Low | Tempting but speculative; easy to over-interpret a coincidence as a signal. |
| **Recent ownership change / funding** | High | Low | Rarely detectable cheaply for a local SMB; not worth the research cost at this scale. |

**Recommendation**: add exactly two new scored fields —
`runs_paid_search` (bool/null) and `paid_search_vs_organic_gap` (derived:
true when ads confirmed AND organic/maps position is weak or unknown) —
and give the combination real weight (see §7). Everything else on this
list is either too expensive to check at scale or too ambiguous to trust;
don't build tracking for signals you won't act on.

---

## 7. PROPOSED LEAD SCORING MODEL

The current model's core flaw isn't its logic, it's that its two heaviest
weights (`maps_position_4_to_15`: 20, `organic_position_5_to_30`: 10 — 30
of 100 points) are also the two fields real discovery can basically never
fill without a paid data source, and its strongest negative signal
(`already_dominant`: -30) requires four simultaneous confirmed fields that
also almost never all exist. The result, empirically, is a scorer that
mostly produces the same 50-55 band regardless of real differences between
leads — exactly what happened to 4 of 5 test leads.

**Redesign principles:**

1. **Reduce ranking-data dependency, increase buying-signal weight.**
   Ranking data should still matter, but it shouldn't be the deciding
   factor for whether a lead is even discussable.
2. **Make `already_dominant` fire on partial evidence, scaled**, not
   require all four fields at once. A business with `rating>=4.8` and
   `review_count>=200` is very likely dominant even with unknown Maps
   position — don't require the unknowable field to apply the penalty.
3. **Introduce a fourth tier: `HIGH_PRIORITY`**, sitting above `QUALIFIED`,
   reserved for leads where buying signals AND opportunity AND contactability
   all align — this is the tier that should get faster human attention and,
   eventually, the deeper audit.

**Proposed weights** (illustrative deltas from current `scoring.yaml`):

```
maps_position_4_to_15:        14   (was 20)
organic_position_5_to_30:      8   (was 10)
weak_service_pages:           15   (unchanged — deterministic, reliable)
weak_website_conversion:      15   (unchanged)
clear_competitor_gap:         10   (unchanged)
low_moderate_reviews:         10   (unchanged)
high_value_niche:              8   (was 10 — see niche re-ranking, §5)
verified_business:             5   (unchanged)
public_contact_discoverable:   5   (unchanged)
runs_paid_search:             12   (NEW)
paid_search_vs_organic_gap:   10   (NEW, additive with above)
named_decision_maker_findable: 6   (NEW — cheap proxy for contactability, checked at discovery)

already_dominant (scaled):
  full evidence (4/4 fields): -30
  strong partial (3/4, incl. rating+reviews): -20
  weak partial (2/4):          -10
```

**Tiering** (using `confirmed_score` unless noted):

- `REJECT`: hard rules (unchanged) OR confirmed `<50`
- `NEEDS_ENRICHMENT`: confirmed `<70`, potential `>=70`, missing a material
  field (unchanged concept, but now "material" should also include
  `runs_paid_search` when it's the only thing separating the lead from
  qualifying — a cheap SERP check, not a ranking-data blocker)
- `QUALIFIED`: confirmed `>=70`
- `HIGH_PRIORITY` (new): confirmed `>=70` **AND** (`runs_paid_search` true
  **OR** `paid_search_vs_organic_gap` true) **AND** a findable named
  decision-maker at discovery time. This tier should get the deeper audit
  (§8) and jump the outreach queue.

**Explicitly do not reward a lead merely because it's weak.** The model
above still requires *evidence of ability/willingness to pay*
(`runs_paid_search`, review volume, niche ticket size) as independent
terms from *evidence of a gap* — a business with a terrible website but no
other buying signal and no findable contact should top out at
`MANUAL_REVIEW`, not `QUALIFIED`, because "needs us" without "can pay us
and will respond" is not a good use of the pipeline.

---

## 8. OPTIMIZED AGENT PIPELINE

**Is 2-3 agents optimal? Mostly yes, but the routing table over-assigns
agents to problems that don't need them, and under-defines when to stop
immediately.**

Concrete issues in `config/routing.yaml` today:

- `service_architecture_gap` → `seo-cluster` + `seo-content` (2 agents) for
  a question (`service_page_count` vs. niche norm) that's already answered
  deterministically. **Fix**: deterministic diff script first; escalate to
  a single `seo-content` call only if the diff is ambiguous or the
  qualitative depth of an *existing* page needs judgment.
- `technical_gap` → `seo-technical` + `seo-sitemap` (2 agents). Sitemap
  validity is a mechanical check (does `/sitemap.xml` exist, is it
  well-formed, does it 200) — this does not need an LLM agent at all.
  **Fix**: deterministic sitemap/robots.txt fetch-and-validate script;
  reserve `seo-technical` for cases where the deterministic check finds a
  real anomaly worth an agent's judgment (mixed content, broken
  canonicals, JS-rendering issues).
- `website_gap` → `seo-sxo` + `seo-technical` (2 agents) — same pattern,
  same fix.
- **Missing a "stop immediately" rule.** Nothing in the routing table
  currently says "if the quick audit's *first* agent call comes back with
  `confidence < 0.5`, do not spend the second/third agent call chasing it
  down." Right now the cap is on count, not on marginal value — a
  low-confidence first result should short-circuit the rest of the plan,
  not just cap at 3.
- **When a deeper audit is actually justified**: only for `HIGH_PRIORITY`
  leads (§7) where the quick audit found a real opportunity but flagged
  `deeper_audit_needed: true` at `confidence >= 0.75` — this should
  remain rare by design, not a routine second pass.

**Recommended routing architecture:**

```
Tier 0 (deterministic, $0, always runs):
  - service-page count vs. niche norm      (already exists in score_leads.py)
  - sitemap/robots.txt fetch+validate       (NEW script, replaces seo-sitemap
                                              in the common case)
  - homepage schema.org presence/type check (NEW lightweight script,
                                              replaces seo-schema in the
                                              common case)

Tier 1 (1 claude-seo specialist, only on QUALIFIED+):
  - the SINGLE agent matching the dominant inferred problem_type,
    per existing routing.yaml, minus the redundant pairs above

Tier 2 (2nd specialist, only if Tier 1 confidence >= 0.6 AND
        problem spans two domains, e.g. conversion + technical):
  - the second-most-relevant specialist

Tier 3 (deep audit, HIGH_PRIORITY only, deeper_audit_needed=true,
        confidence >= 0.75):
  - config/routing.yaml: deep_audit_routes, unchanged
```

Expected effect: most `QUALIFIED` leads resolve on 0-1 real agent calls
(deterministic checks + one specialist) instead of the current 2-3 by
default, with the freed budget available for `HIGH_PRIORITY` leads to
occasionally justify a genuine deep audit.

---

## 9. OPTIMIZED OUTREACH WORKFLOW

**Research quality — mandatory vs. wasted effort** (ranking what actually
matters for *first-touch* prospecting, not a full audit):

| Research area | Value for first touch | Verdict |
|---|---|---|
| Service pages present/missing | High | Mandatory |
| GBP completeness/categories | High | Mandatory |
| Named competitors + their gaps | High | Mandatory (drives the "why you" framing) |
| Reviews (count, recency, rating) | High | Mandatory |
| Ads presence | High (currently missing entirely) | **Add as mandatory** |
| Conversion flow (phone visibility, form, booking) | Medium-high | Mandatory if cheap (1-2 page check) |
| Contact form / decision-maker findability | High (gates the whole pipeline) | Mandatory |
| Maps/organic position | Medium (nice-to-have, not blocking per current design) | Include when available, never block on it alone |
| Schema/entity markup | Low for first touch | Skip unless it's the single defensible finding — this is a "we noticed and fixed it" line-item value-add, not usually the hook |
| Backlinks | Low | Skip for first touch — expensive to get right, rarely the most compelling angle to a business owner (owners understand "you're not showing up," not "your domain authority is low") |
| Full content quality | Low | Skip — only relevant if it's directly tied to the one chosen opportunity |
| Pricing | Low-medium | Useful context for the human sales conversation, not for the cold email itself |
| Traffic / Search-Console-like signals | Low (we don't have GSC access to the prospect) | Skip — can't verify anyway |

**Send timing**: Tuesday-Friday and the two default windows
(8:30-9:30 law/professional, 7:00-8:30 home-service) are *reasonable
starting heuristics and nothing more* — say so explicitly in the system,
not as claimed optimal. To test scientifically: log `sent_at` (resolved
local time), `reply_at` (if any), and `positive_reply` per send, and once
you have 50+ sends, cut reply rate by day-of-week and by 15-minute local
send bucket. Don't touch the heuristic before there's a real sample.

**Follow-up cadence**: the 3-touch + close-loop model is directionally
fine but underspecified. Recommend concrete spacing: **Day 0 (first
email), Day 4 (follow-up 1: second specific finding), Day 9 (follow-up 2:
competitor/strategy insight), Day 16 (final close-loop)** — roughly a
2.5-week arc, consistent with normal B2B cold cadence. Add: **channel
switch at touch 3 for HIGH_PRIORITY leads only** (a LinkedIn connection
request/comment referencing the same finding) — not universally, it's
labor/tool-intensive. **Recycle non-responders at 120-180 days**, but only
after re-running enrichment (a stale finding resent verbatim is worse than
not recontacting at all).

**Offer evolution** (first email → proposal): don't sell "Local Growth
System" at any point before a paid engagement exists. Recommended arc:
first email sells nothing, just proves research + gives free value → reply
gets a short comparison/one-pager (§12) → call sells a **narrow, priced
first engagement** (a scoped audit-plus-roadmap, or a 90-day sprint on the
ONE opportunity already proven in the dossier) → only after that engagement
delivers does the conversation expand toward the full Local Growth System
as a retainer. Selling the full system on call #1 is a slower, harder close
than selling the thing you already proved you found.

---

## 10. EMAIL STRATEGY

**Critique of the observation → consequence → free fix → CTA model**: the
structure is correct. The risk isn't the structure, it's that the
structure is exactly what every other "personalized" cold-SEO-email
generator also produces now — the shape itself is at risk of being
pattern-matched as AI-generated by a recipient who's seen five of these
this month. Fighting that requires specificity, not restructuring.

- **Length**: 120-180 words is right; if anything, bias toward the lower
  end (120-150) for home-service niches (busy owners, phone-first) and the
  higher end (150-180) for law/professional niches (more comfortable with
  written communication).
- **Subject line**: avoid anything with "SEO," "grow your business," or
  the company name alone. Best-performing pattern for this kind of
  outreach is typically a specific, slightly odd, true detail: e.g. *"no
  emergency-repair page on [domain]"* or *"[Competitor] has a page you
  don't."* Specificity in the subject line is itself a personalization
  signal before the email is even opened.
- **Whether to mention SEO at all**: no, not by name, in the first email.
  Describe the mechanism ("shows up when someone searches X"), not the
  industry term. "SEO" primes the "generic agency pitch" pattern-match
  instantly.
- **Whether to mention Google Maps**: yes, if it's the actual finding —
  concretely, not as a category ("your Maps listing needs work" is
  generic; "your GBP has no photos of finished jobs, and three competitors
  each have 20+" is not).
- **Whether to mention competitors by name**: yes — this is one of the
  strongest de-commoditizing moves available and it's already in the
  system's evidence model. Named competitors are the single easiest way to
  prove real research in one sentence.
- **Whether to mention commercial opportunity/consequence**: yes, but as
  mechanism ("that's the exact search someone makes when their AC dies at
  night"), never as a fabricated number ("you're losing $X/month") — this
  matches the existing no-fabrication rule and should stay strict.
- **Links/screenshots in the first email**: be careful — inline images and
  multiple links increase spam-filter risk (see §11) for a *cold* first
  touch from a new/low-volume sender. Prefer describing the finding in
  text; save a screenshot/mini-report for the **reply-triggered
  follow-through** (§12), where deliverability risk matters less because
  the recipient already opted in by replying.
- **Owner vs. marketing contact**: owner for businesses under ~15
  employees / solo-to-small law firms (they hold budget and reply faster);
  marketing director/coordinator for larger firms and multi-location
  operators (it's literally their job to read this, and they can move
  faster internally than routing it past an owner).

**Differentiation ideas, ranked by realistic scalability:**

| Idea | Scalable at 10-20/day? | Recommendation |
|---|---|---|
| Named competitors + specific gap in the copy | Yes (already built) | Keep as the primary differentiator |
| One-line, non-fabricated opportunity framing (mechanism, not dollar figure) | Yes | Keep |
| Mini 1-page comparison/opportunity report | **No for first touch** — yes for reply follow-through | Build for §12, not §10 |
| Screenshot of the specific gap (e.g. missing GBP category) | Marginal — adds real production time per lead | Reserve for `HIGH_PRIORITY` leads only, not universal |
| Loom/video | No | Too expensive per lead at any real volume; reserve for a warm follow-up, not cold |
| Personalized landing page per prospect | No | Not worth the build cost at this stage; revisit only if reply rate plateaus and this specific lever is tested |
| Free fix delivered inline (not just described) | Sometimes (e.g. a one-line schema snippet) | Use opportunistically when the fix is genuinely one line; don't force it |

**QA critique**: the "would this survive a company-name swap" test is
good and should stay the hard REWRITE trigger. Add one more check the
current QA list doesn't have: **"does this email read like it was written
by a template with variables filled in, independent of factual accuracy?"**
— an email can pass every factual check and still have a mechanically
templated *voice*. This needs to stay a human-judgment-callable flag, not
something to over-formalize into another boolean.

---

## 11. DELIVERABILITY PLAN

The operating context already admits bounces have happened. Treat this as
the highest-operational-risk part of the entire system right now, because
reputation damage is slow to fix and directly threatens the ability to
send *any* future email, not just this campaign's.

- **Do not scale to 15-20/day on an unwarmed sending identity.** Start at
  3-5/day for the first 1-2 weeks on the actual Gmail account being used,
  ramping gradually. This matters more than any targeting improvement in
  this document if the sending identity gets flagged.
- **Verify SPF/DKIM/DMARC alignment** on whatever domain is actually
  sending (a personal Gmail address has different — generally worse —
  deliverability dynamics than a properly authenticated custom domain
  sending through Gmail/Workspace). If this hasn't been explicitly
  checked, check it before increasing volume.
- **Bounce-rate threshold**: treat >2-3% hard-bounce rate on any given
  day's batch as a stop signal, not just a per-contact
  `CONTACT_REVERIFY_REQUIRED` — a rising bounce rate is an early warning
  the *verification process itself*, not just one contact, is degrading.
- **Stagger** (already specified, e.g. 08:32/08:44/08:57) is good practice
  and should stay — avoid exact-interval sends (e.g. every 5 minutes on
  the dot), which look automated even when staggered, in favor of
  irregular gaps.
- **Links/images**: minimize in the first touch (§10) — a cold email with
  zero or one plain-text link scores better on most spam filters than one
  with an embedded image or multiple links.
- **Subject-line risk**: avoid ALL-CAPS words, excessive punctuation, and
  spam-trigger vocabulary ("free," "guarantee," "act now") — the current
  copy guidance already avoids these in body copy; extend the same
  discipline explicitly to subject lines.
- **Never** compensate for a low reply rate by increasing volume — that's
  the most common way a legitimately good targeting/content system
  destroys its own sender reputation.

---

## 12. SALES HANDOFF

**The gap named in §1/§4: nothing currently generates the deliverable the
CTA promises.** Design the shortest real path:

1. **Reply arrives ("sure, send it").** Automated reply-check (already
   planned) flags `REPLIED` within the monitoring interval, automation
   stops immediately for that prospect (already correct).
2. **Same business day, ideally within 1-2 hours**: generate the promised
   comparison — a short (1-page, non-designed, plain-text-or-simple-HTML)
   document listing 2-3 *additional* real findings already sitting in the
   dossier's evidence model (the dossier is built to hold more evidence
   than the single email uses) plus the named-competitor comparison. **This
   generator needs to be built now** — it's a small script, not a new
   research pass, since the evidence should already exist from the quick
   audit; it just needs a second, slightly more open compact template.
3. **Send the comparison with a direct, specific CTA**: "Want to do a
   quick 15-minute call this week to walk through it?" — *now* is the
   right moment for a meeting ask, not before.
4. **Ali takes the call.** System's job ends at: surfaced reply + dossier +
   prior outreach history + suggested talking points, not a written
   response draft that risks sounding scripted at exactly the moment a
   real relationship is starting.
5. **Call → scoped, priced first engagement** (§9's offer-evolution logic,
   not the full Local Growth System) **→ proposal → paid client.**

**Where automation should stop, explicitly**: the moment a human has
replied with actual interest, not just at "REPLIED" status generically —
a one-line "not interested, remove me" reply should also stop automation
(already implied) but should NOT surface to Ali as a sales-ready lead; only
genuinely positive/curious replies should generate the call-prep packet.

---

## 13. COST OPTIMIZATION

- **Deterministic** (already true, keep): scoring, thresholds, caching,
  agent-cap enforcement, freshness checks, contact-guard logic, ranking
  import/normalization.
- **Should become deterministic** (currently LLM-cost, shouldn't be):
  sitemap/robots.txt validation, schema.org presence/type detection,
  service-page-count-vs-norm diffing (§8) — these are pattern/mechanical
  checks masquerading as agent tasks.
- **Cheap/fast model tier**: business-identity cross-checking (matching
  phone/address/domain across sources — mechanical fact comparison, not
  deep reasoning), first-pass email QA screening for the objective checks
  (word count, banned phrases, presence of a CTA) before the harder
  judgment calls.
- **Strong reasoning model tier, worth the cost**: the opportunity-selector
  step (choosing the ONE best angle among several real candidates is a
  genuine judgment call), the adversarial final email QA pass (trust-critical,
  should stay skeptical-by-default as designed), and the identity-collision
  resolution step (Example Restoration-style disambiguation genuinely
  benefits from careful reasoning, not pattern matching).
- **Human**: final sales judgment, pricing, anything touching an actual
  reply thread.
- **Cache/reuse aggressively**: market intelligence (already built),
  ranking imports (already built), and — new — **ads-presence checks
  per market's top competitors**, since "who in this market runs ads" is a
  market-level fact worth caching alongside `top_organic_competitors`, not
  re-derived per lead.
- **Terminate immediately** when: hard reject rules fire (unchanged);
  identity verification confidence is low with unresolved conflicts
  (unchanged); first quick-audit agent call returns confidence `<0.5`
  (NEW — don't spend the 2nd/3rd agent call chasing a weak signal); no
  contact channel of any kind exists, not even a contact form (currently
  this still allows a dossier/email attempt to be built before dying at
  contact verification — check contactability *before* the quick audit,
  not after, since a business with zero findable contact path can't be
  outreached to regardless of how good the opportunity is).

---

## 14. FAILURE MODES (18, with mitigations)

**Technical**
1. *Gmail API auth token expires mid-batch.* → Pre-flight token check
   before every batch; abort remaining sends in that batch cleanly, don't
   silently skip.
2. *MCP tool call fails silently, no send confirmation logged.* → Every
   send must positively log a Gmail message/thread ID or be marked
   `SEND_ATTEMPTED_UNCONFIRMED`, never assumed sent.
3. *DST transition miscalculates a local send window.* → Always resolve
   through a real IANA-timezone-aware library, log both local and UTC
   time, never hand-roll offset math.
4. *Concurrent script writes corrupt shared JSONL files* (already
   observed as a real class of bug this session). → Route all status
   writes through `set_status_everywhere()` (already fixed); never run
   two write-capable scripts against the same prospect file concurrently.

**Data**
5. *Business-identity collision* (Example Restoration-style). → Verification
   gate (already built), but extend it: flag any name matching a common
   franchise/generic-word pattern for mandatory multi-source cross-check.
6. *Stale market cache used for a "current" competitor claim.* → Cache
   entries should carry `observed_at`; a claim sourced from a >90-day-old
   market cache entry should require a fresh spot-check before being cited
   in an email.
7. *Ranking data imported for the wrong business* (name/domain mismatch in
   `import_rankings.py`). → Already partially guarded by domain/name
   matching in `rescore_leads.py`; add a confirmation echo of matched rows
   before applying.

**Automation**
8. *A fork/parallel run "simulates" a specialist agent instead of really
   invoking it* (happened during earlier parallel test runs, disclosed
   honestly at the time). → In real production single-lead runs this
   constraint doesn't apply, but the reporting must always distinguish
   "real agent tool call" from "reasoning applied without the tool" —
   never let the two look identical in a report.
9. *Follow-up fires after a reply that landed in a different
   thread/alias.* → Reply-check must search by prospect email address
   across all threads, not just the original sent thread ID.

**Research**
10. *WebFetch's own summarization step introduces a subtle inaccuracy not
    caught by the evidence model.* → Evidence items citing a WebFetch
    result should quote the specific observed text/element when the claim
    is load-bearing, not just paraphrase.
11. *Franchise/multi-location confusion* (wrong location's data attributed
    to the target). → Address-match check as part of identity verification
    (already built for name collisions; extend explicitly to multi-location
    brands).

**Deliverability**
12. *Bounce spiral from a stale/generic-guess-adjacent contact list.* →
    Already mitigated by the no-guessing rule; the residual risk is
    volume ramp speed (§11).
13. *Sender flagged as spam by pattern, not content* (identical stagger
    intervals, identical CTA phrasing across every email). → Vary phrasing
    templates and stagger irregularity, not just timing.

**Targeting**
14. *Chasing an over-competed niche wastes the whole funnel's effort even
    when every downstream step executes perfectly.* → §5's re-ranking;
    track reply rate by niche from day one and be willing to deprioritize
    PI/criminal law fast if the data confirms the prediction.
15. *Targeting businesses too small to afford real engagements.* → §7's
    buying-signal-gated scoring, not just gap-size.

**Personalization**
16. *Evidence goes stale between dossier build and actual send* (queue
    delay). → Freshness check (already built) should re-run immediately
    before send, not just before draft generation.

**Sales**
17. *A fast positive reply isn't handled fast enough and interest cools.* →
    §12's same-business-day deliverable generator is the direct mitigation.

**Legal/reputation**
18. *CAN-SPAM/CASL compliance gaps* (missing physical address/opt-out in
    commercial email; Canada's CASL is notably stricter than US CAN-SPAM
    for commercial electronic messages). → Before any Canada expansion,
    confirm CASL consent-exception applicability explicitly; every US
    email should carry a valid opt-out regardless.

---

## 15. TOP 10 IMPROVEMENTS (ranked by expected impact)

1. **Add `runs_paid_search` / ads-vs-organic-gap signal** — highest
   expected lift per unit of build effort; currently completely absent.
2. **Re-rank niche priority away from PI/criminal law toward
   restoration, immigration/family law, dental implants, and foundation
   repair** as the first wave.
3. **Rebalance scorer weights off pure ranking-data dependency** onto
   buying signals — directly addresses the 4/5 `NEEDS_ENRICHMENT`
   bottleneck.
4. **Build the reply-triggered comparison/one-pager generator** before the
   next positive reply arrives, not after.
5. **Add `CONTACT_FORM_READY` as a real secondary path**, not a dead end,
   for `HIGH_PRIORITY` leads only.
6. **Cut agent routing redundancy** (`seo-cluster`+`seo-content`,
   `seo-technical`+`seo-sitemap` pairs) to deterministic-first, single-agent
   fallback.
7. **Institute a bounce-safe volume ramp** (3-5/day → 8-15/day) instead of
   assuming the target volume is safe to start at.
8. **Introduce the `HIGH_PRIORITY` tier** so the deeper audit and faster
   human attention go to leads that are actually likely to close, not just
   technically qualified.
9. **Move identity verification research after a free discovery-data
   pre-filter**, not before it.
10. **Instrument niche/city/finding-type reply-rate tracking from send #1**
    — every other recommendation here is a hypothesis until real reply
    data confirms or kills it; the system's own weekly-learning-loop
    section already exists on paper but has no data to run on yet.

---

## 16. REVISED MASTER AUTOMATION PROMPT

See companion file `reports/OPERATING-RULES-V3.md` for the full,
standalone replacement operating document — it incorporates only the
changes justified above (niche re-ranking, buying-signal scoring, the
`HIGH_PRIORITY` tier, `CONTACT_FORM_READY`, the reply-deliverable
requirement, and the deliverability ramp plan), explicitly marked as
deltas from the prior version rather than a ground-up rewrite.
