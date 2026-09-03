#!/usr/bin/env python3
"""
V3.8 -- ranking-provider abstraction. This is the seam Lead Engine's
automated ranking-enrichment orchestrator (scripts/rank_enrichment.py) uses
to ask "do we have (or can we get) trustworthy ranking evidence for this
lead/query", without being tied to one data source.

Every provider here is read-only over already-vetted, already-durable data
(a human-operated import into data/rankings/<market_id>.csv, or a small
pre-vetted observations file a human/analyst dropped in the inbox
directory) -- NONE of them make a live network call, hold a credential, or
scrape a search engine. That is a deliberate, structural property, not an
oversight: see OPERATING-RULES.md's V3.8 update and
reports/V3.8-AUTOMATED-RANKING-ENRICHMENT-REPORT.md for why no trustworthy
zero-cost fully-automatic localized-ranking source exists to plug in here
safely today, and what ExternalRankProvider becoming real would require.

Four possible outcomes per (lead, query) attempt -- never a fifth, silent
"treat missing as poor rank" outcome:
  ALREADY_SATISFIED -- a fresh, validated, query-matched observation is
    already durably stored (e.g. a prior manual import). No new import is
    needed; scripts/reevaluate_needs_enrichment.py's existing, unchanged
    V3.7.1 matching/selection logic will pick it up from
    data/rankings/<market_id>.csv exactly as it always has.
  OBSERVATION -- a provider found a NEW, validated, query-matched
    observation (e.g. from a freshly-dropped inbox file) that still needs
    to be imported via scripts/import_ranking_observation.py's existing,
    unchanged validate_observation()/import_observations() choke point.
  RANKING_SOURCE_REQUIRED -- no provider in the configured chain could
    produce trustworthy evidence for this query. This is the honest,
    fail-closed default -- never converted into a guessed position.
  FAILURE -- a provider itself broke (timeout, malformed data, an entity
    mismatch it caught, etc.) while trying to answer. Isolated per lead/
    query by the caller -- never blocks any other lead or query, and never
    silently becomes a passing/qualifying result.
"""
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from _lib import ROOT, days_since
from rescore_leads import load_rankings, find_ranking_match, domain_of
from import_rankings import KNOWN_SOURCES
from import_ranking_observation import validate_observation

STATUS_ALREADY_SATISFIED = "ALREADY_SATISFIED"
STATUS_OBSERVATION = "OBSERVATION"
STATUS_SOURCE_REQUIRED = "RANKING_SOURCE_REQUIRED"
STATUS_FAILURE = "FAILURE"

MIN_REASONABLE_POSITION = 1
MAX_REASONABLE_POSITION = 200  # generous upper bound -- rejects obviously malformed/garbage values, not a real ranking claim


# ---------------------------------------------------------------------------
# Provider-call failure types. A real (future) network-backed provider would
# raise one of these; the orchestrator (scripts/rank_enrichment.py:
# attempt_query) catches every one of them (and any other exception, as a
# defensive backstop) and turns it into an isolated per-lead/query FAILURE
# record -- it never propagates and never blocks a different lead or query.
# ---------------------------------------------------------------------------
class ProviderError(Exception):
    """Base class for a real provider-call failure."""


class ProviderTimeout(ProviderError):
    pass


class ProviderAuthError(ProviderError):
    pass


class ProviderRateLimited(ProviderError):
    pass


class ProviderMalformedResponse(ProviderError):
    pass


class ProviderGeoUnresolved(ProviderError):
    pass


@dataclass
class ProviderResult:
    status: str
    provider: str
    reason: Optional[str] = None
    observation: Optional[dict] = None


def _normalize_query(q):
    return re.sub(r"[^a-z0-9]+", " ", (q or "").lower()).strip()


def queries_match(a, b):
    """Pure: deliberately permissive (punctuation/case/comma-insensitive
    substring match) so a hand-typed CSV keyword ('water damage restoration
    aurora co') and a generated query ('water damage restoration aurora
    co') compare equal even with minor formatting differences -- but never
    so loose that an unrelated query silently counts (an empty/blank
    normalized string never matches anything, including another blank)."""
    na, nb = _normalize_query(a), _normalize_query(b)
    if not na or not nb:
        return False
    return na == nb or na in nb or nb in na


def entity_mismatch(row_domain_or_dict, qr_domain):
    """Pure: True if a matched row's OWN explicit domain actively conflicts
    with the query's expected domain -- guards against
    scripts/rescore_leads.find_ranking_match's looser name-substring path
    silently letting a different, similarly-named business's row through.
    A row with no domain at all is not a mismatch (nothing to conflict
    with) -- absence is never treated as a conflict."""
    row_domain = row_domain_or_dict if isinstance(row_domain_or_dict, str) else domain_of(
        row_domain_or_dict.get("domain") or row_domain_or_dict.get("ranking_url")
    )
    if row_domain and qr_domain and row_domain != qr_domain:
        return True
    return False


def valid_position(v):
    """Pure: (ok, int_value_or_None). Rejects non-numeric and obviously
    malformed/out-of-range positions -- never silently coerces garbage into
    a number that would then be treated as a real ranking claim."""
    if v in (None, ""):
        return False, None
    try:
        iv = int(float(v))
    except (TypeError, ValueError):
        return False, None
    if iv < MIN_REASONABLE_POSITION or iv > MAX_REASONABLE_POSITION:
        return False, None
    return True, iv


def _field_for(intended_evidence_type):
    return "maps_position" if intended_evidence_type == "MAPS" else "organic_position"


class RankingProvider:
    name = "base"

    def fetch(self, query_record, freshness_days):
        raise NotImplementedError


class ManualImportProvider(RankingProvider):
    """Reads already-imported data/rankings/<market_id>.csv rows -- written
    exclusively by the two existing human-operated CLIs
    (scripts/import_rankings.py, scripts/import_ranking_observation.py),
    never by this provider or any unattended process. Returns
    ALREADY_SATISFIED when a fresh, validated, query-matched row already
    covers the requested evidence type (Maps or organic, never conflated);
    the durable CSV is the single source of truth scripts/
    reevaluate_needs_enrichment.py already reads, so nothing further needs
    to be written -- the orchestrator's own always-run re-evaluation step
    will pick this up exactly like it always has.
    """
    name = "manual_import"

    def fetch(self, qr, freshness_days):
        rows = load_rankings(qr["market_id"])
        if not rows:
            return ProviderResult(STATUS_SOURCE_REQUIRED, self.name,
                                   reason=f"no ranking data imported yet for {qr['market_id']}")

        candidates = find_ranking_match(rows, qr.get("business_name"), qr.get("domain"))
        if not candidates:
            return ProviderResult(STATUS_SOURCE_REQUIRED, self.name,
                                   reason="ranking data exists for this market but none matched this business")

        query_candidates = [r for r in candidates if queries_match(r.get("keyword"), qr["query"])]
        if not query_candidates:
            return ProviderResult(STATUS_SOURCE_REQUIRED, self.name,
                                   reason="matched business rows exist but none is for this exact query")

        clean = [r for r in query_candidates if not entity_mismatch(r, qr.get("domain"))]
        if not clean:
            return ProviderResult(STATUS_FAILURE, self.name,
                                   reason="entity_mismatch: matched row's own domain conflicts with this lead's domain")

        field = _field_for(qr["intended_evidence_type"])
        usable = []
        for r in clean:
            if r.get("exact_rank_verified") in ("False", "false", False):
                continue  # an absence observation, never a usable position
            if r.get("source") not in KNOWN_SOURCES:
                continue  # unknown/unrecognized source -- unreachable by construction, defensive anyway
            ok, pos = valid_position(r.get(field))
            if not ok:
                continue
            age = days_since(r.get("observed_at"))
            if age is None or age > freshness_days:
                continue  # undated or stale -- cannot back a fresh qualification; re-enrichment stays eligible
            usable.append((r, pos))

        if not usable:
            any_value_at_all = any(r.get(field) not in (None, "") for r in clean)
            reason = (f"matching row(s) for this query exist but are stale/unverified/malformed for {field}"
                      if any_value_at_all else f"matching row(s) for this query have no {field} value")
            return ProviderResult(STATUS_SOURCE_REQUIRED, self.name, reason=reason)

        row, pos = usable[0]
        return ProviderResult(
            STATUS_ALREADY_SATISFIED, self.name,
            reason=f"fresh {field}={pos} already on file (source={row.get('source')}, observed_at={row.get('observed_at')})",
        )


class SemrushFileProvider(RankingProvider):
    """Reads a small, pre-vetted observations file a human/analyst has
    already produced and dropped at <inbox_dir>/<market_id>.json -- the
    SAME shape schemas/ranking_evidence_observation.schema.json /
    scripts/import_ranking_observation.py --file already accepts (a list of
    observation objects). Reuses that script's own
    validate_observation() as the single provenance/shape choke point --
    nothing is re-implemented or loosened here. Never calls a live Semrush
    (or any other) API and never holds a Semrush credential of any kind;
    the "file" in the name is deliberate -- this is a file reader, not a
    client for Semrush's service.
    """
    name = "semrush_file"

    def __init__(self, inbox_dir=None):
        self.inbox_dir = Path(inbox_dir) if inbox_dir else (ROOT / "data" / "rankings" / "inbox")

    def fetch(self, qr, freshness_days):
        path = self.inbox_dir / f"{qr['market_id']}.json"
        if not path.exists():
            return ProviderResult(STATUS_SOURCE_REQUIRED, self.name, reason=f"no inbox file at {path}")

        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            raise ProviderMalformedResponse(f"unreadable/malformed inbox file {path.name}: {e}") from e

        observations = data if isinstance(data, list) else [data]
        field = _field_for(qr["intended_evidence_type"])

        for obs in observations:
            if not isinstance(obs, dict):
                continue
            ok, reason = validate_observation(obs)
            if not ok:
                continue  # a bad row in a batch file never blocks the others -- same rule as import_observations()
            if not queries_match(obs.get("query"), qr["query"]):
                continue
            if entity_mismatch(obs.get("domain") or "", qr.get("domain")):
                continue
            age = days_since(obs.get("observed_at"))
            if age is None or age > freshness_days:
                continue
            valid_ok, _ = valid_position(obs.get(field))
            if not valid_ok:
                continue
            imported_obs = dict(obs)
            imported_obs["location"] = qr["market_id"]
            return ProviderResult(STATUS_OBSERVATION, self.name, observation=imported_obs)

        return ProviderResult(STATUS_SOURCE_REQUIRED, self.name,
                               reason="inbox file present but no fresh, matching, valid observation for this query")


class ExternalRankProvider(RankingProvider):
    """Interface for a real, credential-backed live rank-tracking API
    (e.g. DataForSEO, SerpApi, ValueSERP) that could answer a Maps/organic
    position query on demand. NOT IMPLEMENTED in this environment: no such
    credential is configured, and adding one is a deliberate, separately
    authorized future phase (a real provider key, a cost review, and an
    explicit decision to allow a live query) -- the same "designed, not
    implemented until separately authorized" pattern this repository
    already uses elsewhere for another out-of-scope real-world action.

    Always returns RANKING_SOURCE_REQUIRED -- the honest, fail-closed
    default. This is intentionally NOT in config/ranking_enrichment.yaml's
    default `providers` list; it exists so the abstraction has a concrete
    slot to fill in later without changing the orchestrator's shape.
    """
    name = "external_api"

    def fetch(self, qr, freshness_days):
        return ProviderResult(
            STATUS_SOURCE_REQUIRED, self.name,
            reason="no external rank-tracking provider is configured in this environment "
                   "(see OPERATING-RULES.md's V3.8 update)",
        )


PROVIDER_REGISTRY = {
    "manual_import": ManualImportProvider,
    "semrush_file": SemrushFileProvider,
    "external_api": ExternalRankProvider,
}


def build_providers(cfg):
    names = cfg.get("providers") or ["manual_import"]
    return [PROVIDER_REGISTRY[n]() for n in names if n in PROVIDER_REGISTRY]


def attempt_query(providers, qr, freshness_days, log=print):
    """Tries each provider in order for one query record. A provider that
    raises (a ProviderError subclass, or any other unexpected exception) is
    recorded as a FAILURE for THIS provider only and the chain continues to
    the next provider -- one provider's breakage never gives up on a query
    that a different provider could still answer, and never blocks any
    other query or lead (that isolation happens one level up, in
    scripts/rank_enrichment.py: run_cycle).
    """
    seen_failure = None
    for provider in providers:
        try:
            result = provider.fetch(qr, freshness_days)
        except Exception as e:
            log(f"  ! {provider.name} provider error for {qr.get('business_name')!r} / {qr['query']!r}: {e}")
            seen_failure = ProviderResult(STATUS_FAILURE, provider.name, reason=str(e)[:300])
            continue
        if result.status in (STATUS_OBSERVATION, STATUS_ALREADY_SATISFIED):
            return result
        if result.status == STATUS_FAILURE:
            seen_failure = result
            continue
        # RANKING_SOURCE_REQUIRED -- fall through to the next provider in the chain
    if seen_failure is not None:
        return seen_failure
    return ProviderResult(
        STATUS_SOURCE_REQUIRED, providers[-1].name if providers else "none",
        reason="no configured provider produced trustworthy evidence for this query",
    )
