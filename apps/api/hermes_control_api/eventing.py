from __future__ import annotations

import asyncio
import hashlib
import json
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Any

from hermes_client import NormalizedEvent, ReplayState


@dataclass(eq=False, slots=True)
class Subscription:
    user_id: str
    queue: asyncio.Queue[tuple[dict, int]]
    queued_bytes: int = 0
    max_queue_bytes: int = 4 * 1024 * 1024


class SubscriptionLimitError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class InteractionClaim:
    kind: str
    request_id: str
    gateway_id: str
    profile_name: str
    stored_session_id: str | None
    runtime_session_id: str | None
    runtime_generation: str | None
    question_ids: frozenset[str]


class EventHub:
    def __init__(
        self,
        *,
        queue_size: int = 512,
        max_routes: int = 2_048,
        max_subscriptions: int = 64,
        max_subscriptions_per_user: int = 4,
        max_buffer_bytes: int = 32 * 1024 * 1024,
        max_route_buffer_bytes: int = 2 * 1024 * 1024,
        max_event_bytes: int = 256 * 1024,
    ) -> None:
        self.queue_size = queue_size
        self.max_routes = max_routes
        self.max_subscriptions = max_subscriptions
        self.max_subscriptions_per_user = max_subscriptions_per_user
        self.max_buffer_bytes = max_buffer_bytes
        self.max_route_buffer_bytes = max_route_buffer_bytes
        self.max_event_bytes = max_event_bytes
        self._subscriptions: set[Subscription] = set()
        self._replay: OrderedDict[tuple[str, str, str], ReplayState] = OrderedDict()
        self._buffers: OrderedDict[str, deque[tuple[dict[str, Any], int]]] = OrderedDict()
        self._buffer_size = min(512, max(128, queue_size))
        self._route_buffer_bytes: dict[str, int] = {}
        self._total_buffer_bytes = 0
        self._correlated_runs: OrderedDict[
            tuple[str, str, str, str], deque[tuple[NormalizedEvent, int]]
        ] = OrderedDict()
        self._correlated_run_bytes = 0
        self._max_correlated_run_bytes = 4 * 1024 * 1024
        self._max_correlated_runs = 512
        self._interactions: OrderedDict[
            tuple[str, str, str, str], InteractionClaim
        ] = OrderedDict()
        self._max_interactions = 4_096
        self._lock = asyncio.Lock()

    def interaction_matches(
        self,
        *,
        kind: str,
        request_id: str,
        gateway_id: str,
        profile_name: str,
        stored_session_id: str,
        runtime_session_id: str | None,
        runtime_generation: str | None,
        question_id: str | None = None,
    ) -> bool:
        claim = self._interactions.get(
            (gateway_id, profile_name, kind, request_id)
        )
        return self._interaction_claim_matches(
            claim,
            stored_session_id=stored_session_id,
            runtime_session_id=runtime_session_id,
            runtime_generation=runtime_generation,
            question_id=question_id,
        )

    @staticmethod
    def _interaction_claim_matches(
        claim: InteractionClaim | None,
        *,
        stored_session_id: str,
        runtime_session_id: str | None,
        runtime_generation: str | None,
        question_id: str | None,
    ) -> bool:
        if claim is None:
            return False
        if claim.stored_session_id and claim.stored_session_id != stored_session_id:
            return False
        if claim.runtime_session_id:
            if claim.runtime_session_id != runtime_session_id:
                return False
            if (
                not claim.runtime_generation
                or claim.runtime_generation != runtime_generation
            ):
                return False
        if not claim.stored_session_id and not claim.runtime_session_id:
            return False
        if claim.kind == "clarification":
            if claim.question_ids:
                return question_id in claim.question_ids
            return question_id is None
        return question_id is None

    def take_interaction(
        self,
        *,
        kind: str,
        request_id: str,
        gateway_id: str,
        profile_name: str,
        stored_session_id: str,
        runtime_session_id: str | None,
        runtime_generation: str | None,
        question_id: str | None = None,
    ) -> InteractionClaim | None:
        """Atomically reserve one upstream gate before awaiting its mutation."""

        key = (gateway_id, profile_name, kind, request_id)
        claim = self._interactions.get(key)
        if not self._interaction_claim_matches(
            claim,
            stored_session_id=stored_session_id,
            runtime_session_id=runtime_session_id,
            runtime_generation=runtime_generation,
            question_id=question_id,
        ):
            return None
        assert claim is not None
        if kind == "clarification" and claim.question_ids and question_id:
            remaining = claim.question_ids - {question_id}
            if remaining:
                self._interactions[key] = InteractionClaim(
                    kind=claim.kind,
                    request_id=claim.request_id,
                    gateway_id=claim.gateway_id,
                    profile_name=claim.profile_name,
                    stored_session_id=claim.stored_session_id,
                    runtime_session_id=claim.runtime_session_id,
                    runtime_generation=claim.runtime_generation,
                    question_ids=frozenset(remaining),
                )
            else:
                self._interactions.pop(key, None)
        else:
            self._interactions.pop(key, None)
        return claim

    def restore_interaction(self, claim: InteractionClaim) -> None:
        """Restore a reservation only when dispatch was proven not to occur."""

        key = (
            claim.gateway_id,
            claim.profile_name,
            claim.kind,
            claim.request_id,
        )
        current = self._interactions.get(key)
        if current is None:
            self._interactions[key] = claim
        elif (
            current.stored_session_id == claim.stored_session_id
            and current.runtime_session_id == claim.runtime_session_id
            and current.runtime_generation == claim.runtime_generation
        ):
            self._interactions[key] = InteractionClaim(
                kind=current.kind,
                request_id=current.request_id,
                gateway_id=current.gateway_id,
                profile_name=current.profile_name,
                stored_session_id=current.stored_session_id,
                runtime_session_id=current.runtime_session_id,
                runtime_generation=current.runtime_generation,
                question_ids=current.question_ids | claim.question_ids,
            )
        self._interactions.move_to_end(key)

    def forget_interaction(
        self, *, gateway_id: str, profile_name: str, kind: str, request_id: str
    ) -> None:
        self._interactions.pop(
            (gateway_id, profile_name, kind, request_id), None
        )

    def restrict_clarification_questions(
        self,
        *,
        gateway_id: str,
        profile_name: str,
        request_id: str,
        remaining_question_ids: list[str],
    ) -> bool:
        """Narrow a batch claim to Hermes' authoritative ``remaining`` set.

        This operation can only remove qids. An unexpected added qid means the
        upstream response no longer matches the request event and the claim is
        discarded fail-closed.
        """

        key = (gateway_id, profile_name, "clarification", request_id)
        claim = self._interactions.get(key)
        if claim is None or not claim.question_ids:
            return False
        remaining = frozenset(remaining_question_ids)
        if not remaining or not remaining.issubset(claim.question_ids):
            self._interactions.pop(key, None)
            return False
        self._interactions[key] = InteractionClaim(
            kind=claim.kind,
            request_id=claim.request_id,
            gateway_id=claim.gateway_id,
            profile_name=claim.profile_name,
            stored_session_id=claim.stored_session_id,
            runtime_session_id=claim.runtime_session_id,
            runtime_generation=claim.runtime_generation,
            question_ids=remaining,
        )
        self._interactions.move_to_end(key)
        return True

    def _update_interactions(self, event: NormalizedEvent) -> None:
        data = event.data or {}
        request_id = data.get("request_id")
        if not isinstance(request_id, str) or not 0 < len(request_id) <= 200:
            return
        if event.type in {"approval.request", "clarify.request"}:
            kind = "approval" if event.type == "approval.request" else "clarification"
            question_ids = frozenset(
                str(item["qid"])
                for item in data.get("questions", [])[:50]
                if isinstance(item, dict)
                and isinstance(item.get("qid"), str)
                and 0 < len(str(item["qid"])) <= 100
            ) if isinstance(data.get("questions"), list) else frozenset()
            key = (event.gateway_id, event.profile_name, kind, request_id)
            self._interactions[key] = InteractionClaim(
                kind=kind,
                request_id=request_id,
                gateway_id=event.gateway_id,
                profile_name=event.profile_name,
                stored_session_id=event.stored_session_id,
                runtime_session_id=event.runtime_session_id,
                runtime_generation=event.runtime_generation,
                question_ids=question_ids,
            )
            self._interactions.move_to_end(key)
            while len(self._interactions) > self._max_interactions:
                self._interactions.popitem(last=False)
        elif event.type == "clarify.expire":
            self.forget_interaction(
                gateway_id=event.gateway_id,
                profile_name=event.profile_name,
                kind="clarification",
                request_id=request_id,
            )

    def remember_correlation(self, event: NormalizedEvent) -> None:
        """Keep a small pre-persistence journal for fast automation events.

        Hermes may emit run.completed before the trigger HTTP response gives
        Control the run id.  Recording first and reconciling after the local
        row commits closes that race without trusting generic prompt
        correlation ids.
        """

        if not event.type.startswith(("run.", "cron.")):
            return
        run_id = event.data.get("run_id")
        automation_id = event.data.get("job_id")
        if not all(isinstance(value, str) and 0 < len(value) <= 255 for value in (run_id, automation_id)):
            return
        size = len(
            json.dumps(event.to_dict(), separators=(",", ":"), default=str).encode("utf-8")
        )
        if size > self.max_event_bytes:
            return
        key = (event.gateway_id, event.profile_name, run_id, automation_id)
        journal = self._correlated_runs.setdefault(key, deque())
        self._correlated_runs.move_to_end(key)
        journal.append((event, size))
        self._correlated_run_bytes += size
        while len(journal) > 16:
            _, removed = journal.popleft()
            self._correlated_run_bytes -= removed
        while (
            len(self._correlated_runs) > self._max_correlated_runs
            or self._correlated_run_bytes > self._max_correlated_run_bytes
        ):
            _, removed_journal = self._correlated_runs.popitem(last=False)
            self._correlated_run_bytes -= sum(item_size for _, item_size in removed_journal)

    def correlated_run_events(
        self,
        *,
        gateway_id: str,
        profile_name: str,
        run_id: str,
        automation_id: str,
    ) -> tuple[NormalizedEvent, ...]:
        journal = self._correlated_runs.get(
            (gateway_id, profile_name, run_id, automation_id)
        )
        return tuple(event for event, _ in journal) if journal else ()

    @staticmethod
    def route_key(payload: dict[str, Any]) -> str:
        identity = payload.get("runtimeSessionId") or payload.get("storedSessionId") or "gateway"
        return "\x1f".join(
            (str(payload.get("gatewayId") or ""), str(payload.get("profileName") or ""), str(identity))
        )

    def replay_since(
        self, cursors: dict[str, dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        # Reserve one reconciliation frame per accepted cursor. Together the
        # returned lists can never exceed 2,048 frames.
        max_events = 1_984
        events: list[dict[str, Any]] = []
        reconciliations: list[dict[str, Any]] = []
        for route_key, cursor in list(cursors.items())[:64]:
            if not isinstance(route_key, str) or len(route_key) > 1_024:
                continue
            if len(events) >= max_events:
                gateway_id, profile_name, route_identity = self._split_route_key(route_key)
                reconciliations.append(
                    {
                        "id": "reconcile-limit-"
                        + hashlib.sha256(route_key.encode()).hexdigest()[:16],
                        "type": "control.reconcile",
                        "gatewayId": gateway_id,
                        "profileName": profile_name,
                        "storedSessionId": None,
                        "runtimeSessionId": None,
                        "_routeIdentity": route_identity,
                        "replayEpoch": None,
                        "reconciliationRequired": True,
                        "data": {"reason": "replay_limit", "historyRequired": True},
                    }
                )
                continue
            cursor_epoch = str(cursor.get("epoch") or "")
            try:
                last_seq = max(0, int(cursor.get("seq") or 0))
            except (TypeError, ValueError):
                last_seq = 0
            buffer = self._buffers.get(route_key)
            if not buffer:
                if last_seq or cursor_epoch:
                    gateway_id, profile_name, route_identity = self._split_route_key(route_key)
                    reconciliations.append(
                        {
                            "id": "reconcile-empty-"
                            + hashlib.sha256(route_key.encode()).hexdigest()[:16],
                            "type": "control.reconcile",
                            "gatewayId": gateway_id,
                            "profileName": profile_name,
                            "storedSessionId": None,
                            "runtimeSessionId": None,
                            # Internal only: the websocket ownership binder
                            # resolves this against stored OR runtime identity
                            # and strips it before the frame leaves Control.
                            "_routeIdentity": route_identity,
                            "replayEpoch": None,
                            "reconciliationRequired": True,
                            "data": {
                                "reason": "buffer_empty",
                                "historyRequired": True,
                            },
                        }
                    )
                continue
            buffered = [item for item, _ in buffer]
            current_epoch = next(
                (str(item["replayEpoch"]) for item in reversed(buffered) if item.get("replayEpoch")),
                "",
            )
            sequenced = [item for item in buffered if isinstance(item.get("seq"), int)]
            earliest = min((int(item["seq"]) for item in sequenced), default=0)
            epoch_changed = bool(cursor_epoch and current_epoch and cursor_epoch != current_epoch)
            truncated = bool(last_seq and earliest and earliest > last_seq + 1)
            if epoch_changed or truncated:
                sample = buffered[-1]
                reconciliations.append(
                    {
                        "id": f"reconcile-{sample.get('eventId') or sample.get('id')}",
                        "type": "control.reconcile",
                        "gatewayId": sample.get("gatewayId"),
                        "profileName": sample.get("profileName"),
                        "storedSessionId": sample.get("storedSessionId"),
                        "runtimeSessionId": sample.get("runtimeSessionId"),
                        "_runtimeGeneration": sample.get("_runtimeGeneration"),
                        "replayEpoch": current_epoch or None,
                        "reconciliationRequired": True,
                        "data": {"reason": "epoch_changed" if epoch_changed else "buffer_truncated"},
                    }
                )
            if not epoch_changed:
                candidates = [item for item in sequenced if int(item["seq"]) > last_seq]
                if len(events) + len(candidates) > max_events:
                    sample = buffered[-1]
                    reconciliations.append(
                        {
                            "id": f"reconcile-limit-{sample.get('eventId') or sample.get('id')}",
                            "type": "control.reconcile",
                            "gatewayId": sample.get("gatewayId"),
                            "profileName": sample.get("profileName"),
                            "storedSessionId": sample.get("storedSessionId"),
                            "runtimeSessionId": sample.get("runtimeSessionId"),
                            "_runtimeGeneration": sample.get("_runtimeGeneration"),
                            "replayEpoch": current_epoch or None,
                            "reconciliationRequired": True,
                            "data": {"reason": "replay_limit", "historyRequired": True},
                        }
                    )
                else:
                    events.extend(candidates)
        events.sort(key=lambda item: str(item.get("occurredAt") or item.get("timestamp") or ""))
        return events, reconciliations

    @staticmethod
    def _split_route_key(route_key: str) -> tuple[str, str, str]:
        parts = route_key.split("\x1f", 2)
        parts.extend([""] * (3 - len(parts)))
        return parts[0], parts[1], parts[2]

    async def subscribe(self, user_id: str) -> Subscription:
        subscription = Subscription(user_id, asyncio.Queue(maxsize=self.queue_size))
        async with self._lock:
            user_count = sum(
                item.user_id == user_id for item in self._subscriptions
            )
            if (
                len(self._subscriptions) >= self.max_subscriptions
                or user_count >= self.max_subscriptions_per_user
            ):
                raise SubscriptionLimitError("Realtime connection limit reached")
            self._subscriptions.add(subscription)
        return subscription

    async def unsubscribe(self, subscription: Subscription) -> None:
        async with self._lock:
            self._subscriptions.discard(subscription)

    async def next_event(self, subscription: Subscription) -> dict:
        payload, size = await subscription.queue.get()
        subscription.queued_bytes = max(0, subscription.queued_bytes - size)
        return payload

    async def publish(self, event: NormalizedEvent) -> None:
        identities = (
            event.gateway_id,
            event.profile_name,
            event.stored_session_id or "",
            event.runtime_session_id or "",
            event.event_id,
            event.type,
        )
        if any(len(str(value)) > 512 for value in identities):
            return
        route_key = (
            event.gateway_id,
            event.profile_name,
            event.runtime_session_id or event.stored_session_id or "gateway",
        )
        state = self._replay.setdefault(route_key, ReplayState(seen_limit=256))
        self._replay.move_to_end(route_key)
        decision = state.apply(event)
        if not decision.accept:
            return
        self._update_interactions(event)
        payload = event.to_dict()
        if decision.requires_history:
            payload["reconciliationRequired"] = True
        payload_size = len(
            json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8")
        )
        if payload_size > self.max_event_bytes:
            payload = {
                "id": f"oversize-{event.event_id}",
                "type": "control.stream.overflow",
                "timestamp": event.timestamp.isoformat(),
                "gatewayId": event.gateway_id,
                "profileName": event.profile_name,
                "storedSessionId": event.stored_session_id,
                "runtimeSessionId": event.runtime_session_id,
                "_runtimeGeneration": event.runtime_generation,
                "seq": event.sequence,
                "replayEpoch": event.replay_epoch,
                "reconciliationRequired": True,
                "data": {
                    "reason": "event_too_large",
                    "historyRequired": True,
                },
            }
            payload_size = len(
                json.dumps(payload, separators=(",", ":")).encode("utf-8")
            )
        route = self.route_key(payload)
        buffer = self._buffers.setdefault(route, deque())
        self._buffers.move_to_end(route)
        buffer.append((dict(payload), payload_size))
        self._route_buffer_bytes[route] = self._route_buffer_bytes.get(route, 0) + payload_size
        self._total_buffer_bytes += payload_size
        while (
            len(buffer) > self._buffer_size
            or self._route_buffer_bytes[route] > self.max_route_buffer_bytes
        ):
            _, removed_size = buffer.popleft()
            self._route_buffer_bytes[route] -= removed_size
            self._total_buffer_bytes -= removed_size
        while len(self._replay) > self.max_routes:
            old_route, _ = self._replay.popitem(last=False)
            old_key = "\x1f".join(old_route)
            self._drop_buffer(old_key)
        while len(self._buffers) > self.max_routes:
            old_key = next(iter(self._buffers))
            self._drop_buffer(old_key)
            self._replay.pop(self._split_route_key(old_key), None)
        while self._total_buffer_bytes > self.max_buffer_bytes and self._buffers:
            old_key = next(iter(self._buffers))
            self._drop_buffer(old_key)
            self._replay.pop(self._split_route_key(old_key), None)
        async with self._lock:
            subscriptions = tuple(self._subscriptions)
        for subscription in subscriptions:
            dropped = False
            while subscription.queue.full() or (
                subscription.queued_bytes + payload_size > subscription.max_queue_bytes
                and not subscription.queue.empty()
            ):
                try:
                    _, removed_size = subscription.queue.get_nowait()
                    subscription.queued_bytes = max(
                        0, subscription.queued_bytes - removed_size
                    )
                    dropped = True
                except asyncio.QueueEmpty:
                    break
            if dropped:
                overflow = {
                    "id": f"overflow-{event.event_id}",
                    "type": "control.stream.overflow",
                    "timestamp": event.timestamp.isoformat(),
                    "gatewayId": event.gateway_id,
                    "profileName": event.profile_name,
                    "storedSessionId": event.stored_session_id,
                    "runtimeSessionId": event.runtime_session_id,
                    "_runtimeGeneration": event.runtime_generation,
                    "replayEpoch": event.replay_epoch,
                    "reconciliationRequired": True,
                    "data": {"reconciliationRequired": True},
                }
                overflow_size = len(
                    json.dumps(overflow, separators=(",", ":")).encode("utf-8")
                )
                try:
                    subscription.queue.put_nowait((overflow, overflow_size))
                    subscription.queued_bytes += overflow_size
                except asyncio.QueueFull:
                    pass
            try:
                subscription.queue.put_nowait((payload, payload_size))
                subscription.queued_bytes += payload_size
            except asyncio.QueueFull:
                pass

    def _drop_buffer(self, route: str) -> None:
        buffer = self._buffers.pop(route, None)
        removed = self._route_buffer_bytes.pop(route, 0)
        if buffer is not None:
            self._total_buffer_bytes = max(0, self._total_buffer_bytes - removed)
