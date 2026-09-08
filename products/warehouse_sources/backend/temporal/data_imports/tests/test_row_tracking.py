import uuid

import pytest
from posthog.test.base import BaseTest
from unittest import mock

from django.test import override_settings

from parameterized import parameterized
from redis import exceptions as redis_exceptions

from products.warehouse_sources.backend.temporal.data_imports.row_tracking import setup_row_tracking


class _CacheReadFailsClient:
    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def get(self, *args, **kwargs):
        raise redis_exceptions.ConnectionError("Error connecting to redis:6379.")


class TestRowTrackingRedisUnavailable(BaseTest):
    @parameterized.expand(
        [
            (
                "connection_error",
                redis_exceptions.ConnectionError(
                    "Error connecting to redis:6379. Temporary failure in name resolution."
                ),
            ),
            (
                "misconf_error",
                redis_exceptions.ResponseError(
                    "MISCONF Redis is configured to save RDB snapshots, but it's currently unable to persist to "
                    "disk. Commands that may modify the data set are disabled, because this instance is configured "
                    "to report errors during writes if RDB snapshotting fails (stop-writes-on-bgsave-error option). "
                    "Please check the Redis logs for details about the RDB error."
                ),
            ),
        ]
    )
    @pytest.mark.asyncio
    async def test_setup_row_tracking_does_not_raise_when_redis_is_unreachable(self, _name, exception):
        # get_async_client only builds a lazy client - the ping is the first real
        # connection attempt. If it fails, the client must not be used again, or the
        # next command (hset) raises the same connection error, this time uncaught.
        # Row tracking already fails open when redis is unavailable, so a Redis-side
        # error (unreachable, or refusing writes because RDB snapshotting failed) is a
        # transient infra blip, not a bug, and must not be reported to error tracking.
        unreachable_client = mock.AsyncMock()
        unreachable_client.ping.side_effect = exception

        with (
            mock.patch(
                "products.warehouse_sources.backend.temporal.data_imports.row_tracking.get_async_client",
                return_value=unreachable_client,
            ),
            mock.patch(
                "products.warehouse_sources.backend.temporal.data_imports.row_tracking.capture_exception"
            ) as mock_capture_exception,
            override_settings(DATA_WAREHOUSE_REDIS_HOST="localhost", DATA_WAREHOUSE_REDIS_PORT="6379"),
        ):
            await setup_row_tracking(self.team.pk, str(uuid.uuid4()))

        unreachable_client.hset.assert_not_called()
        mock_capture_exception.assert_not_called()

    @pytest.mark.asyncio
    async def test_setup_row_tracking_does_not_raise_when_redis_rejects_writes(self):
        # A successful ping doesn't guarantee the following command succeeds - e.g. Redis
        # can refuse writes (MISCONF) if it can't persist an RDB snapshot to disk. That
        # must fail open like the unreachable-at-ping case above, not crash the sync.
        read_only_client = mock.AsyncMock()
        read_only_client.ping.return_value = True
        read_only_client.hset.side_effect = redis_exceptions.ResponseError(
            "MISCONF Redis is configured to save RDB snapshots, but it's currently "
            "unable to persist to disk. Commands that may modify the data set are "
            "disabled, because this instance is configured to report errors during "
            "writes if RDB snapshotting fails (stop-writes-on-bgsave-error option). "
            "Please check the Redis logs for details about the RDB error."
        )

        with (
            mock.patch(
                "products.warehouse_sources.backend.temporal.data_imports.row_tracking.get_async_client",
                return_value=read_only_client,
            ),
            override_settings(DATA_WAREHOUSE_REDIS_HOST="localhost", DATA_WAREHOUSE_REDIS_PORT="6379"),
        ):
            await setup_row_tracking(self.team.pk, str(uuid.uuid4()))

        read_only_client.expire.assert_not_called()
