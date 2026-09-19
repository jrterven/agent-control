"""Unmodified dispatch body from audited Hermes 939e45c, session_notifications.py.

Rebound onto fake server globals by tests, as Hermes' method_ctx.bind_module does.
Keep the native claim/admission/ack order: the shim must not replace that protocol.
"""


def _notif_dispatch_event(sid: str, session: dict, evt: dict, text: str) -> None:
    """Run the claimed (running=True) agent turn for one notification event."""
    from tools.async_delegation import claim_event_delivery, complete_event_delivery, release_event_delivery
    if (claim := claim_event_delivery(evt, "tui-poller")) is None:
        return
    kwargs = ({"display_kind": "async_delegation_complete", "display_metadata": _async_delegation_display_metadata(evt)}
              if evt.get("type") == "async_delegation" else {})
    try:
        _notif_submit(f"__notif__{int(time.time() * 1000)}", sid, session, text, "notification poller dispatch failed", **kwargs)
    except Exception:
        release_event_delivery(evt, claim)
        return
    complete_event_delivery(evt, claim)
