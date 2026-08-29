from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from .types import NormalizedEvent


@dataclass(frozen=True, slots=True)
class ReplayDecision:
    accept: bool
    duplicate: bool = False
    epoch_changed: bool = False
    gap_detected: bool = False
    requires_history: bool = False


@dataclass(slots=True)
class ReplayState:
    """Tracks Hermes' bounded replay stream without crossing session routes."""

    epoch: str | None = None
    last_sequence: int = 0
    seen_limit: int = 1024
    _seen: set[str] = field(default_factory=set, init=False)
    _seen_order: deque[str] = field(default_factory=deque, init=False)

    def reset(self, *, epoch: str | None = None) -> None:
        self.epoch = epoch
        self.last_sequence = 0
        self._seen.clear()
        self._seen_order.clear()

    def apply(self, event: NormalizedEvent, *, replay_truncated: bool = False) -> ReplayDecision:
        if event.event_id in self._seen:
            return ReplayDecision(accept=False, duplicate=True)

        epoch_changed = bool(
            self.epoch is not None
            and event.replay_epoch is not None
            and event.replay_epoch != self.epoch
        )
        if epoch_changed:
            self.reset(epoch=event.replay_epoch)
        elif self.epoch is None and event.replay_epoch is not None:
            self.epoch = event.replay_epoch

        seq = event.sequence
        if seq is not None and seq <= self.last_sequence and not epoch_changed:
            self._remember(event.event_id)
            return ReplayDecision(accept=False, duplicate=True)

        gap = bool(seq is not None and self.last_sequence and seq > self.last_sequence + 1)
        if seq is not None:
            self.last_sequence = max(self.last_sequence, seq)
        self._remember(event.event_id)
        return ReplayDecision(
            accept=True,
            epoch_changed=epoch_changed,
            gap_detected=gap,
            requires_history=epoch_changed or gap or replay_truncated,
        )

    def _remember(self, event_id: str) -> None:
        if event_id in self._seen:
            return
        self._seen.add(event_id)
        self._seen_order.append(event_id)
        while len(self._seen_order) > self.seen_limit:
            self._seen.discard(self._seen_order.popleft())
