"""Transform a Harmonic company response into the enrichment field registry.

The tag, YC and safe-cast heuristics came from the Salesforce enrichment transforms in
the enterprise tree, which this build does not contain. They are defined below instead,
against the same Harmonic payload shapes the rest of this module reads.
"""

from typing import Any, Optional

from products.growth.backend.enrichment.countries import country_name_to_iso_code
from products.growth.backend.enrichment.fields import EnrichmentFields


def _safe_dict(value: Any) -> dict[str, Any]:
    """Give callers a dict to read from, so a missing or malformed field is not an error."""
    return value if isinstance(value, dict) else {}


def _safe_list(value: Any) -> list[Any]:
    """Give callers a list to iterate, so a missing or malformed field is not an error."""
    return value if isinstance(value, list) else []


def _extract_primary_tag(tags: list[Any], tags_v2: list[Any]) -> Optional[str]:
    """Pick the company's headline industry tag.

    tagsV2 entries are objects with a displayValue, the shape _is_ai_native also reads.
    The older tags list holds plain strings. Harmonic returns the most relevant entry
    first, so the first usable one wins.
    """
    for tag in tags_v2:
        display = _safe_dict(tag).get("displayValue")
        if isinstance(display, str) and display.strip():
            return display.strip()
    for tag in tags:
        if isinstance(tag, str) and tag.strip():
            return tag.strip()
        display = _safe_dict(tag).get("displayValue")
        if isinstance(display, str) and display.strip():
            return display.strip()
    return None


def _is_yc_funded(investors: Any) -> Optional[bool]:
    """Tell if Y Combinator is one of the investors.

    Returns None when there is no investor data, so absence is not recorded as a no.
    This is the same distinction _is_ai_native makes for tags. Company investors carry
    name and angels carry fullName, as _investor_names shows.
    """
    if not isinstance(investors, list) or not investors:
        return None
    for investor in investors:
        entry = _safe_dict(investor)
        name = entry.get("name") or entry.get("fullName")
        if isinstance(name, str) and "y combinator" in name.lower():
            return True
    return False


def _latest_metric(traction: dict[str, Any], metric: str) -> Optional[int]:
    value = _safe_dict(traction.get(metric)).get("latestMetricValue")
    return int(value) if isinstance(value, (int, float)) else None


def _founded_year(founding: dict[str, Any]) -> Optional[int]:
    date = founding.get("date")
    if isinstance(date, str) and "-" in date:
        year = date.split("-", 1)[0]
        if year.isdigit():
            return int(year)
    return None


def _funding_amount(value: Any) -> Optional[int]:
    # Harmonic reports funding as whole USD; store as int to keep it off floats.
    return int(value) if isinstance(value, (int, float)) else None


def _funding_date(value: Any) -> Optional[str]:
    # Harmonic returns an ISO datetime (e.g. "2025-02-25T00:00:00Z"); keep the date.
    if isinstance(value, str) and value:
        return value.split("T", 1)[0]
    return None


# Harmonic's catch-all when it has no real funding-stage signal, distinct from a company it has
# genuinely confirmed raised nothing. ~83% of fetched stages are this shell with no round data
# behind it (prod-checked Aug 2026), so passing it through as funding_stage reads as a real signal
# to downstream consumers when it is actually an absence of one.
_FUNDING_STAGE_UNKNOWN_PLACEHOLDER = "VENTURE_UNKNOWN"


def _has_substantiating_round_data(funding: dict[str, Any]) -> bool:
    total = funding.get("fundingTotal")
    rounds = funding.get("numFundingRounds")
    return (isinstance(total, (int, float)) and total > 0) or (isinstance(rounds, (int, float)) and rounds > 0)


def _funding_stage(funding: dict[str, Any]) -> Optional[str]:
    stage = funding.get("fundingStage")
    if stage == _FUNDING_STAGE_UNKNOWN_PLACEHOLDER and not _has_substantiating_round_data(funding):
        return None
    return stage


# Bound the passthrough so the group property can't grow unboundedly for a heavily-funded company.
MAX_INVESTORS = 25


def _investor_names(investors: Any) -> Optional[list[str]]:
    # Company entries carry `name`, Person (angel) entries carry `fullName`; keep both.
    if not isinstance(investors, list):
        return None
    names = [
        name
        for investor in investors
        if isinstance(investor, dict) and isinstance(name := investor.get("name") or investor.get("fullName"), str)
    ]
    return names[:MAX_INVESTORS] or None


# Harmonic's own tagsV2 taxonomy spells these out; match conservatively on the phrases
# rather than bare "AI"/"ML" tokens that collide with unrelated words.
AI_NATIVE_TAG_MARKERS = ("artificial intelligence", "machine learning")


def _is_ai_native(tags_v2: list[Any]) -> Optional[bool]:
    # Empty tagsV2 is absence of tag data, not evidence the company isn't AI-native.
    if not tags_v2:
        return None
    for tag in tags_v2:
        display = _safe_dict(tag).get("displayValue")
        if isinstance(display, str) and any(marker in display.lower() for marker in AI_NATIVE_TAG_MARKERS):
            return True
    return False


def transform_harmonic_company(company: Optional[dict[str, Any]]) -> Optional[EnrichmentFields]:
    """Map a Harmonic `enrichCompanyByIdentifiers.company` payload to EnrichmentFields.

    Returns None when the payload is missing or not a dict.
    """
    if not company or not isinstance(company, dict):
        return None

    funding = _safe_dict(company.get("funding"))
    traction = _safe_dict(company.get("tractionMetrics"))
    location = _safe_dict(company.get("location"))
    founding = _safe_dict(company.get("foundingDate"))

    headcount = _latest_metric(traction, "headcount")
    if headcount is None and isinstance(company.get("headcount"), (int, float)):
        headcount = int(company["headcount"])

    tags = _safe_list(company.get("tags"))
    tags_v2 = _safe_list(company.get("tagsV2"))

    return EnrichmentFields(
        company_type=company.get("companyType"),
        headcount=headcount,
        headcount_engineering=_latest_metric(traction, "headcountEngineering"),
        web_traffic=_latest_metric(traction, "webTraffic"),
        industry=_extract_primary_tag(tags, tags_v2),
        # ISO alpha-2 to match the format the icp_country group property already holds.
        country=country_name_to_iso_code(location.get("country")),
        founded_year=_founded_year(founding),
        funding_stage=_funding_stage(funding),
        total_raised=_funding_amount(funding.get("fundingTotal")),
        last_round_size=_funding_amount(funding.get("lastFundingTotal")),
        last_round_date=_funding_date(funding.get("lastFundingAt")),
        investors=_investor_names(funding.get("investors")),
        is_yc_company=_is_yc_funded(funding.get("investors")),
        is_ai_native=_is_ai_native(tags_v2),
        ownership_status=company.get("ownershipStatus"),
        customer_type=company.get("customerType"),
        # Harmonic's own known-none-vs-unknown vocabulary for the funding data itself, passed
        # through verbatim rather than re-derived, since fundingAttributeNullStatus already
        # distinguishes what funding_stage's VENTURE_UNKNOWN placeholder conflates. Lives at the
        # top level of Company in the GraphQL schema, not inside funding (API-verified).
        funding_status=company.get("fundingAttributeNullStatus"),
    )
