"""``GlobalRateLimiter.reset_scoped_instances`` drops a blocked scope.

A real 429 blocks its provider's process-wide limiter for 60 seconds
(``error_mapping.map_error``). The unit suite resets the scopes before
every test; without that, a later test on the same xdist worker that
streams through the same provider sleeps out the block and fails on its
first-byte deadline (the budget-retry breaker test did, intermittently).
"""

from __future__ import annotations

import time

from repoach.llm_proxy.providers.rate_limit import GlobalRateLimiter


def test_reset_scoped_instances_returns_an_unblocked_limiter() -> None:
    blocked = GlobalRateLimiter.get_scoped_instance("scope-reset-test")
    blocked.set_blocked(60)
    assert blocked._blocked_until > time.monotonic()

    GlobalRateLimiter.reset_scoped_instances()

    fresh = GlobalRateLimiter.get_scoped_instance("scope-reset-test")
    assert fresh is not blocked
    assert fresh._blocked_until <= time.monotonic()
