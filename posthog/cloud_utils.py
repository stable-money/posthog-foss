import os
from typing import TYPE_CHECKING, Any, Optional

from django.conf import settings

from posthog.run_mode import RunMode, derive_run_mode

if TYPE_CHECKING:
    from ee.models.license import License

is_cloud_cached: Optional[bool] = None
is_instance_licensed_cached: Optional[bool] = None
instance_license_cached: Optional["License"] = None


def _run_mode() -> RunMode:
    """Resolve the run mode from `django.conf.settings`, so `override_settings` applies.

    `posthog.run_mode.run_mode` reads the `posthog.settings` module instead, which is what
    ClickHouse migrations and their `mock.patch`-based tests need. Both spell the mapping
    with `derive_run_mode`; only the settings object they read differs.
    """
    return derive_run_mode(settings.CLOUD_DEPLOYMENT, settings.DEBUG)


# Keep this in sync with isCloud() in nodejs/src/utils/env-utils.ts.
# "dev" refers to the hosted development environment, not local development (which is "local").
def is_cloud() -> bool:
    return _run_mode().is_cloud


def is_dev_mode() -> bool:
    return bool(settings.DEBUG)


def is_hobby() -> bool:
    """Self-hosted install: neither cloud nor local dev. Mirrors `isHobby` in preflightLogic."""
    return _run_mode().is_hobby


def is_ci() -> bool:
    return os.environ.get("GITHUB_ACTIONS") is not None


def get_cached_instance_license() -> Optional["License"]:
    """Returns the first valid license and caches the value for the lifetime of the instance, as it is not expected to change.
    If there is no valid license, it returns None.
    """
    # No licence exists in this build: the License model is part of the enterprise code
    # this tree does not contain, and no migration here creates its table.
    return None

def TEST_clear_instance_license_cache(
    is_instance_licensed: Optional[bool] = None, instance_license: Optional[Any] = None
):
    global instance_license_cached
    instance_license_cached = instance_license
    global is_instance_licensed_cached
    is_instance_licensed_cached = is_instance_licensed


def get_api_host():
    if settings.SITE_URL == "https://us.posthog.com":
        return "https://us.i.posthog.com"
    elif settings.SITE_URL == "https://eu.posthog.com":
        return "https://eu.i.posthog.com"
    return settings.SITE_URL
