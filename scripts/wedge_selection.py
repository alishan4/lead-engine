#!/usr/bin/env python3
"""
V3.2 wedge scoring and selection -- pure functions, no I/O, shared by both
the zero-agent path (run_deterministic_scan.py) and the specialist path
(route_to_specialist.py) so the exact same rules apply regardless of how
many agents were involved.

Does NOT automatically choose the most technically severe issue -- scores
on commercial_relevance/evidence_confidence/specificity/actionability
(config/opportunity_router.yaml: wedge_weights), and enforces the
company-swap specificity test before a candidate can become the wedge.
"""
import re


def score_candidate(candidate, weights):
    """0-100 composite. Never rewards technical severity directly -- there is
    no severity term at all, only commercial_relevance/confidence/specificity/actionability."""
    return round(
        candidate["commercial_relevance"] * weights["commercial_relevance"]
        + candidate["confidence"] * weights["evidence_confidence"]
        + candidate["specificity"] * weights["specificity"]
        + candidate["actionability"] * weights["actionability"]
    )


def passes_company_swap_test(candidate, known_specific_terms=None):
    """
    "If the company name were replaced, would this observation still
    describe many businesses?" A candidate passes only if its
    observation/evidence contains at least one prospect-specific element:
    a named competitor/market fact (known_specific_terms), a concrete
    number (page count, review count, verified position), or a specific
    URL. Purely generic category language (no number, no name, no URL)
    fails -- "your service pages could be better" is not a wedge.
    """
    known_specific_terms = [t for t in (known_specific_terms or []) if t]
    texts = [candidate.get("statement") or candidate.get("observation") or ""]
    for ev in candidate.get("evidence", []):
        texts.append(ev.get("statement", "") if isinstance(ev, dict) else str(ev))
        if isinstance(ev, dict) and ev.get("source"):
            texts.append(ev["source"])

    for text in texts:
        if not text:
            continue
        if any(term.lower() in text.lower() for term in known_specific_terms):
            return True
        if re.search(r"\d", text):
            return True
        if re.search(r"https?://", text):
            return True
    return False


def select_primary_wedge(candidates, known_specific_terms, weights, min_confidence=None):
    """
    Given a list of OpportunityCandidate dicts, return (best_candidate_or_None,
    wedge_score_or_None, reason). Filters out NO_CLEAR_OPPORTUNITY and any
    candidate that fails the company-swap test before scoring; the highest
    scorer among the survivors wins. Returns (None, None, reason) if nothing
    survives -- this is a valid, expected NO_DEFENSIBLE_WEDGE outcome, not
    an error.
    """
    real_candidates = [c for c in candidates if c.get("type") != "NO_CLEAR_OPPORTUNITY"]
    if not real_candidates:
        return None, None, "no candidate opportunities were generated -- deterministic scan and any specialist found nothing worth flagging"

    if min_confidence is not None:
        real_candidates = [c for c in real_candidates if c["confidence"] >= min_confidence]
        if not real_candidates:
            return None, None, f"every candidate fell below the usable-confidence threshold ({min_confidence})"

    specific = [c for c in real_candidates if passes_company_swap_test(c, known_specific_terms)]
    if not specific:
        return None, None, "every candidate failed the company-swap test -- observations were generic enough to describe many businesses, not evidence of a real prospect-specific wedge"

    scored = [(score_candidate(c, weights), c) for c in specific]
    scored.sort(key=lambda t: t[0], reverse=True)
    best_score, best = scored[0]
    return best, best_score, "selected highest-scoring, company-swap-passing candidate"


FORBIDDEN_MECHANISM_PHRASES = (
    "this hurts seo", "could improve rankings", "may increase traffic",
    "needs more content", "seo could be improved", "your seo needs",
)

FABRICATED_REVENUE_PATTERNS = (
    r"you('re| are) losing \$", r"costs? you \d+ (cases|leads|customers)? ?(per|/)\s*month",
    r"you could gain \d+ leads", r"\$\d+[\s,]*(per month|/mo|monthly)? (in )?(lost|missed)",
)


def commercial_mechanism_is_defensible(mechanism_text):
    """
    Rejects generic non-mechanism statements and fabricated revenue-loss
    claims. Returns (ok: bool, reason_if_not).
    """
    low = mechanism_text.lower()
    for phrase in FORBIDDEN_MECHANISM_PHRASES:
        if phrase in low:
            return False, f"generic non-mechanism statement detected: {phrase!r}"
    for pattern in FABRICATED_REVENUE_PATTERNS:
        if re.search(pattern, low):
            return False, f"fabricated revenue-loss framing detected matching pattern: {pattern!r}"
    return True, None
