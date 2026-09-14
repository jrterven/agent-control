from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import TimeoutError as PoolTimeout

from hermes_control_api.cloud_concurrency import CloudConcurrencyMiddleware
from hermes_control_api import database


def http_scope(path="/api/v1/bootstrap"):
    return {"type": "http", "method": "GET", "path": path}


async def invoke(middleware, scope=None):
    messages = []
    async def send(message):
        messages.append(message)
    await middleware(scope or http_scope(), None, send)
    return messages


@pytest.mark.asyncio
async def test_bounded_queue_keeps_connector_replies_and_health_runnable():
    active = peak = 0
    all_entered, release = asyncio.Event(), asyncio.Event()
    async def app(scope, receive, send):
        nonlocal active, peak
        if scope["type"] == "websocket":
            await send({"type": "websocket.accept"})
            return
        if scope["path"] == "/api/v1/health":
            await send({"type": "http.response.start", "status": 200})
            return
        active += 1
        peak = max(peak, active)
        if active == 2:
            all_entered.set()
        await release.wait()
        await send({"type": "http.response.start", "status": 200})
        active -= 1
    middleware = CloudConcurrencyMiddleware(app, active_limit=2, queued_limit=3)
    tasks = [asyncio.create_task(invoke(middleware)) for _ in range(12)]
    await asyncio.wait_for(all_entered.wait(), 1)
    assert middleware.admitted == 5
    assert sum(task.done() for task in tasks) == 7
    probe = await asyncio.wait_for(invoke(middleware, http_scope("/api/v1/health")), .5)
    socket = await asyncio.wait_for(invoke(middleware, {"type": "websocket", "path": "/api/v1/connectors/ws"}), .5)
    assert probe[0]["status"] == 200
    assert socket[0]["type"] == "websocket.accept"
    release.set()
    results = await asyncio.gather(*tasks)
    assert sorted(result[0]["status"] for result in results) == [200] * 5 + [503] * 7
    assert peak == 2 and middleware.admitted == 0


@pytest.mark.asyncio
async def test_slot_is_held_until_dependency_cleanup_after_response():
    body_sent, cleanup = asyncio.Event(), asyncio.Event()
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200})
        await send({"type": "http.response.body", "body": b"{}"})
        body_sent.set()
        await cleanup.wait()
    middleware = CloudConcurrencyMiddleware(app, active_limit=1, queued_limit=0)
    first = asyncio.create_task(invoke(middleware))
    await body_sent.wait()
    assert (await invoke(middleware))[0]["status"] == 503
    cleanup.set()
    await first
    assert (await invoke(middleware))[0]["status"] == 200


@pytest.mark.asyncio
async def test_canceled_and_expired_waiters_release_admission_capacity():
    entered, release = asyncio.Event(), asyncio.Event()
    async def app(scope, receive, send):
        entered.set()
        await release.wait()
        await send({"type": "http.response.start", "status": 200})
    middleware = CloudConcurrencyMiddleware(app, active_limit=1, queued_limit=1, wait_seconds=.02)
    first = asyncio.create_task(invoke(middleware))
    await entered.wait()
    waiter = asyncio.create_task(invoke(middleware))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert middleware.admitted == 1
    expired = await invoke(middleware)
    assert expired[0]["status"] == 503
    assert middleware.admitted == 1
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert middleware.admitted == 0
    release.set()
    assert (await invoke(middleware))[0]["status"] == 200


@pytest.mark.asyncio
async def test_disconnect_when_waiter_is_awakened_does_not_lose_a_slot():
    entered, release = asyncio.Event(), asyncio.Event()
    waiter = None
    calls = 0
    async def app(scope, receive, send):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
            # Client cancellation will run immediately after the departing
            # request releases its permit and wakes the queued request.
            asyncio.get_running_loop().call_soon(waiter.cancel)
        await send({"type": "http.response.start", "status": 200})
    middleware = CloudConcurrencyMiddleware(app, active_limit=1, queued_limit=1)
    first = asyncio.create_task(invoke(middleware))
    await entered.wait()
    waiter = asyncio.create_task(invoke(middleware))
    await asyncio.sleep(0)
    assert middleware.admitted == 2
    release.set()
    await first
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert middleware.admitted == 0
    assert (await asyncio.wait_for(invoke(middleware), .5))[0]["status"] == 200


@pytest.mark.asyncio
async def test_probe_traffic_has_separate_finite_capacity():
    entered, release = asyncio.Event(), asyncio.Event()
    async def app(scope, receive, send):
        entered.set()
        await release.wait()
        await send({"type": "http.response.start", "status": 200})
    middleware = CloudConcurrencyMiddleware(app, probe_limit=1)
    probe = asyncio.create_task(invoke(middleware, http_scope("/api/v1/ready")))
    await entered.wait()
    assert (await invoke(middleware, http_scope("/api/v1/health")))[0]["status"] == 503
    release.set()
    await probe


@pytest.mark.asyncio
async def test_pool_exhaustion_returns_sanitized_retry_without_waiting():
    async def app(scope, receive, send):
        raise PoolTimeout("private DSN and query must never appear")
    middleware = CloudConcurrencyMiddleware(app)
    response = await invoke(middleware)
    assert response[0]["status"] == 503
    assert b"private" not in response[1]["body"]
    assert (b"retry-after", b"2") in response[0]["headers"]
    assert await invoke(middleware, {"type": "websocket", "path": "/api/v1/connectors/ws"}) == [
        {"type": "websocket.close", "code": 1013}]
    mutation = await invoke(middleware, {"type": "http", "method": "POST", "path": "/api/v1/sessions"})
    assert mutation[0]["status"] == 409
    assert b'"retryable":false' in mutation[1]["body"]
    assert b"MUTATION_DELIVERY_UNKNOWN" in mutation[1]["body"]
    assert middleware.admitted == 0


def test_cloud_pool_is_bounded_and_checkout_never_blocks_event_loop(monkeypatch, tmp_path):
    # Exercise the actual QueuePool policy with a local DBAPI. No external
    # PostgreSQL is required to prove a 65th checkout fails immediately.
    def local_engine(url, **kwargs):
        assert url == "postgresql+psycopg://unused.invalid/cloud"
        return create_engine(f"sqlite:///{tmp_path / 'pool.db'}", **kwargs)
    monkeypatch.setattr(database, "create_engine", local_engine)
    engine = database.build_engine(SimpleNamespace(deployment_mode="cloud", database_url="postgresql://unused.invalid/cloud"))
    held = [engine.connect() for _ in range(64)]
    try:
        started = time.perf_counter()
        with pytest.raises(PoolTimeout):
            engine.connect()
        assert time.perf_counter() - started < .5
        assert engine.pool.size() == 64
    finally:
        for connection in held:
            connection.close()
        engine.dispose()


def test_private_postgres_retains_existing_pool_policy():
    engine = database.build_engine(SimpleNamespace(deployment_mode="private", database_url="postgresql://unused.invalid/private"))
    assert engine.pool.size() == 5
    assert engine.pool.timeout() == 30
    engine.dispose()


def test_cloud_admission_precedes_database_backed_security_middleware():
    from hermes_control_api.main import create_app
    from hermes_control_api.config import Settings
    from hermes_control_api.middleware import SecurityBoundaryMiddleware, IdempotencyMiddleware
    app = create_app(Settings(environment="test", deployment_mode="cloud", public_base_url="https://control.test", database_url="sqlite://"))
    order = [middleware.cls for middleware in app.user_middleware]
    assert order.index(CloudConcurrencyMiddleware) < order.index(SecurityBoundaryMiddleware) < order.index(IdempotencyMiddleware)
