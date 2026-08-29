from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import httpx
from hermes_client import AdminResourceSnapshot, HermesProvider
from sqlalchemy.orm import Session

from .models import User
from .services import (
    AppServices,
    ConflictError,
    GatewayService,
    NotFoundError,
    audit,
    require_capability,
)


T = TypeVar("T")


class AdminResourceService:
    """Capability-gated bridge to Hermes' optional administration modules."""

    def __init__(self, services: AppServices) -> None:
        self.services = services
        self.gateways = GatewayService(services)

    async def provider(
        self,
        db: Session,
        *,
        gateway_id: str,
        profile_name: str,
        capability: str,
    ) -> HermesProvider:
        await require_capability(
            db,
            self.services,
            gateway_id=gateway_id,
            profile_name=profile_name,
            method=capability,
        )
        connection = await self.gateways.connection(db, gateway_id, profile_name)
        return await self.services.provider_pool.get(connection)

    async def read(
        self,
        db: Session,
        *,
        gateway_id: str,
        profile_name: str,
        capability: str,
        call: Callable[[HermesProvider], Awaitable[AdminResourceSnapshot]],
    ) -> AdminResourceSnapshot:
        provider = await self.provider(
            db,
            gateway_id=gateway_id,
            profile_name=profile_name,
            capability=capability,
        )
        return await self._translate(call(provider))

    async def mutate(
        self,
        db: Session,
        actor: User,
        *,
        gateway_id: str,
        profile_name: str,
        capability: str,
        resource: str,
        action: str,
        call: Callable[[HermesProvider], Awaitable[AdminResourceSnapshot]],
        target_id: str | None = None,
    ) -> AdminResourceSnapshot:
        provider = await self.provider(
            db,
            gateway_id=gateway_id,
            profile_name=profile_name,
            capability=capability,
        )
        result = await self._translate(call(provider))
        audit(
            db,
            actor=actor,
            action=f"admin.{resource}.{action}",
            target_type=resource,
            target_id=target_id,
            details={"gatewayId": gateway_id, "profile": profile_name},
        )
        db.commit()
        return result

    @staticmethod
    async def _translate(
        operation: Awaitable[AdminResourceSnapshot],
    ) -> AdminResourceSnapshot:
        try:
            return await operation
        except KeyError as exc:
            raise NotFoundError("Hermes administration resource was not found") from exc
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise NotFoundError("Hermes administration resource was not found") from exc
            if exc.response.status_code in {400, 409, 422}:
                raise ConflictError("Hermes rejected the administration mutation") from exc
            raise
