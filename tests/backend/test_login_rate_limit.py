from collections import defaultdict, deque

from hermes_control_api.middleware import SecurityBoundaryMiddleware


def test_login_limiter_has_a_global_bucket_across_rotating_proxy_addresses():
    middleware = SecurityBoundaryMiddleware.__new__(SecurityBoundaryMiddleware)
    middleware._login_attempts = defaultdict(deque)

    for index in range(middleware._LOGIN_ATTEMPT_LIMIT):
        assert middleware._login_rate_limited(f"forged-{index}", 100.0) is False

    assert middleware._login_rate_limited("forged-next", 100.0) is True
    assert middleware._login_rate_limited("legitimate-peer", 161.0) is False
