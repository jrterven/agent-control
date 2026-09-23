"""Fail-closed, request-local cloud resource visibility.

Background workers deliberately use unscoped sessions; they must route using
stored ownership. HTTP sessions get this policy before loading product rows.
"""
from sqlalchemy import event, select, true
from sqlalchemy.orm import Session, with_loader_criteria

from . import models as m
from .connector_models import Connector
from .mail_models import MailAccount, MailAgent, MailOAuthFlow, MailSendOperation


def active_gateway_filter(gateway_column, *, cloud: bool, owner_id: str):
    """Omit revoked routes from active lists without deleting their owned data.

    This is a projection filter, not an ownership policy. Offline computers stay
    visible and canonical historical records remain independently addressable.
    """
    if not cloud:
        return true()
    revoked = select(Connector.gateway_id).where(
        Connector.owner_id == owner_id, Connector.revoked_at.is_not(None),
    )
    return gateway_column.not_in(revoked)


def scope_cloud_session(db: Session, owner_id: str) -> None:
    if db.info.get("cloud_owner_id") is not None:
        if db.info["cloud_owner_id"] != owner_id:
            raise RuntimeError("A database session cannot change owners")
        return
    db.info["cloud_owner_id"] = owner_id
    gateway_ids = select(m.Gateway.id).where(m.Gateway.owner_id == owner_id)
    automation_ids = select(m.Automation.id).where(m.Automation.owner_id == owner_id)
    policy = [
        with_loader_criteria(m.Gateway, m.Gateway.owner_id == owner_id, include_aliases=True),
        with_loader_criteria(m.ProfileRef, m.ProfileRef.gateway_id.in_(gateway_ids), include_aliases=True),
        with_loader_criteria(m.GatewayCredential, m.GatewayCredential.gateway_id.in_(gateway_ids), include_aliases=True),
        with_loader_criteria(m.AutomationRun, m.AutomationRun.automation_id.in_(automation_ids), include_aliases=True),
        with_loader_criteria(m.AuditEvent, m.AuditEvent.actor_user_id == owner_id, include_aliases=True),
    ]
    for model in (m.Workspace, m.SessionLink, m.Automation, m.LiveTranscript,
                  m.SemanticPreference, m.SemanticIndexState, m.SemanticFragment,
                  m.EmailReferenceCache, m.PushSubscription, m.Tag, m.SessionTag,
                  m.AttachmentReference, m.Draft, m.UserIntegration,
                  m.UserVoicePreference, m.OpenAIProfileVoicePreference,
                  m.VisionPreference, m.VisionObservationRecord, m.VisionRequestReceipt,
                  m.VisualMedia, m.VisualMediaRouteTombstone):
        if hasattr(model, "owner_id"):
            policy.append(with_loader_criteria(model, model.owner_id == owner_id, include_aliases=True))
    for model in (MailAccount, MailAgent, MailOAuthFlow, MailSendOperation):
        policy.append(with_loader_criteria(model, model.owner_id == owner_id, include_aliases=True))

    def restrict(statement):
        if statement.is_select or statement.is_update or statement.is_delete:
            statement.statement = statement.statement.options(*policy)

    event.listen(db, "do_orm_execute", restrict)
