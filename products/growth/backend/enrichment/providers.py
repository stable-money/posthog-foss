"""Enrichment provider interface.

The interface keeps the enrichment core provider-agnostic.
"""

import abc
from typing import Any, Optional

from posthog.dataclasses import frozen

from products.growth.backend.enrichment.fields import EnrichmentFields


@frozen
class ProviderLookup:
    """One provider lookup: the transformed fields plus the raw response kept for the archive.

    `fields` is None on a not-found. `raw_payload` is the provider's verbatim response for the
    company, or None when the provider returns nothing at all for a miss. `enrichment_urn` is
    the provider's tracking id for this lookup (Harmonic's enrichmentUrn), archived alongside
    the payload so a later poll can check whether enrichment has since completed.
    """

    fields: Optional[EnrichmentFields]
    raw_payload: Optional[dict[str, Any]]
    enrichment_urn: Optional[str] = None


class EnrichmentProvider(abc.ABC):
    """Looks up firmographic enrichment for a single company by domain."""

    name: str

    @abc.abstractmethod
    async def enrich_by_domain(self, domain: str) -> ProviderLookup:
        """Return the fields and raw payload for a domain; fields is None when not found.

        Raises on operational failure (network, provider outage) so the caller can retry
        and alert, rather than conflating an outage with a genuine not-found.
        """

    async def enrichment_status_for(self, urn: str) -> Optional[str]:
        """Poll the provider for the status of a previously archived tracking URN.

        No-op by default; a provider without async enrichment tracking has nothing to poll.
        """
        return None
