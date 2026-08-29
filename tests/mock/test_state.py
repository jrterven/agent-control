from __future__ import annotations

from mock_hermes.state import MockHermesState


def test_lazy_session_persists_on_first_message_and_resume_changes_runtime() -> None:
    state = MockHermesState()
    session = state.create_session("default")

    assert session.stored_session_id == "stored-0001"
    assert session.runtime_session_id == "00000001"
    assert state.list_sessions("default") == []

    state.add_message(session, "user", "Hola Newton")
    assert state.list_sessions("default") == [session]
    old_runtime = session.runtime_session_id

    resumed = state.resume_session(session.stored_session_id, "default")
    assert resumed.stored_session_id == session.stored_session_id
    assert resumed.runtime_session_id != old_runtime


def test_replay_reports_gap_when_ring_buffer_was_truncated() -> None:
    state = MockHermesState(replay_buffer_size=3)
    session = state.create_session("control-dev")
    for index in range(5):
        state.emit(session, "message.delta", {"text": str(index)})

    replay = state.events_since(session, 0)
    assert replay["truncated"] is True
    assert replay["latest_seq"] == 5
    assert [event["params"]["seq"] for event in replay["events"]] == [3, 4, 5]


def test_epoch_change_resets_sequence_watermarks() -> None:
    state = MockHermesState()
    session = state.create_session("jarvis")
    state.emit(session, "message.delta", {"text": "before"})
    original_epoch = state.replay_epoch

    new_epoch = state.bump_epoch()

    assert new_epoch != original_epoch
    assert session.latest_seq == 0
    assert list(session.events) == []
