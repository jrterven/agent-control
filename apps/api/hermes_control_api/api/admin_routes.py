from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request
from sqlalchemy.orm import Session

from ..admin_schemas import (
    AdminResourceView,
    ChannelMutation,
    ConfigMutation,
    McpServerCreate,
    MemoryProviderMutation,
    MemoryResetMutation,
    ModelSelection,
    SecretMutation,
    SoulMutation,
    ToggleMutation,
)
from ..admin_service import AdminResourceService
from ..auth import (
    current_admin,
    get_db,
    require_csrf,
    require_idempotency,
)
from ..models import AuthSession, User


_RESOURCE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}$"
_ENV_NAME_PATTERN = r"^[A-Z][A-Z0-9_]{0,199}$"
ResourceId = Annotated[str, Path(pattern=_RESOURCE_ID_PATTERN)]
SecretName = Annotated[str, Path(pattern=_ENV_NAME_PATTERN)]

router = APIRouter(
    prefix="/api/v1/admin/gateways/{gateway_id}/profiles/{profile_name}",
    tags=["administration"],
)


def _service(request: Request) -> AdminResourceService:
    return AdminResourceService(request.app.state.services)


def _view(gateway_id: str, profile_name: str, snapshot) -> AdminResourceView:
    return AdminResourceView(
        gateway_id=gateway_id,
        profile_name=profile_name,
        resource=snapshot.resource,
        data=snapshot.data,
    )


@router.get("/models", response_model=AdminResourceView)
async def models(
    gateway_id: str,
    profile_name: ResourceId,
    request: Request,
    _: User = Depends(current_admin),
    db: Session = Depends(get_db),
) -> AdminResourceView:
    snapshot = await _service(request).read(
        db,
        gateway_id=gateway_id,
        profile_name=profile_name,
        capability="models.list",
        call=lambda provider: provider.list_models(),
    )
    return _view(gateway_id, profile_name, snapshot)


@router.patch("/models", response_model=AdminResourceView)
async def set_model(
    gateway_id: str,
    profile_name: ResourceId,
    payload: ModelSelection,
    request: Request,
    _: AuthSession = Depends(require_csrf),
    __: str = Depends(require_idempotency),
    actor: User = Depends(current_admin),
    db: Session = Depends(get_db),
) -> AdminResourceView:
    snapshot = await _service(request).mutate(
        db,
        actor,
        gateway_id=gateway_id,
        profile_name=profile_name,
        capability="models.set",
        resource="models",
        action="set",
        call=lambda provider: provider.set_model(
            payload.provider,
            payload.model,
            confirm_expensive_model=payload.confirm_expensive_model,
        ),
    )
    return _view(gateway_id, profile_name, snapshot)


@router.get("/config", response_model=AdminResourceView)
async def config(
    gateway_id: str,
    profile_name: ResourceId,
    request: Request,
    _: User = Depends(current_admin),
    db: Session = Depends(get_db),
) -> AdminResourceView:
    snapshot = await _service(request).read(
        db,
        gateway_id=gateway_id,
        profile_name=profile_name,
        capability="config.get",
        call=lambda provider: provider.get_config(),
    )
    return _view(gateway_id, profile_name, snapshot)


@router.patch("/config", response_model=AdminResourceView)
async def update_config(
    gateway_id: str,
    profile_name: ResourceId,
    payload: ConfigMutation,
    request: Request,
    _: AuthSession = Depends(require_csrf),
    __: str = Depends(require_idempotency),
    actor: User = Depends(current_admin),
    db: Session = Depends(get_db),
) -> AdminResourceView:
    snapshot = await _service(request).mutate(
        db,
        actor,
        gateway_id=gateway_id,
        profile_name=profile_name,
        capability="config.set",
        resource="config",
        action="update",
        call=lambda provider: provider.update_config(payload.config),
    )
    return _view(gateway_id, profile_name, snapshot)


@router.get("/soul", response_model=AdminResourceView)
async def soul(
    gateway_id: str,
    profile_name: ResourceId,
    request: Request,
    _: User = Depends(current_admin),
    db: Session = Depends(get_db),
) -> AdminResourceView:
    snapshot = await _service(request).read(
        db,
        gateway_id=gateway_id,
        profile_name=profile_name,
        capability="soul.get",
        call=lambda provider: provider.get_soul(),
    )
    return _view(gateway_id, profile_name, snapshot)


@router.patch("/soul", response_model=AdminResourceView)
async def update_soul(
    gateway_id: str,
    profile_name: ResourceId,
    payload: SoulMutation,
    request: Request,
    _: AuthSession = Depends(require_csrf),
    __: str = Depends(require_idempotency),
    actor: User = Depends(current_admin),
    db: Session = Depends(get_db),
) -> AdminResourceView:
    snapshot = await _service(request).mutate(
        db,
        actor,
        gateway_id=gateway_id,
        profile_name=profile_name,
        capability="soul.set",
        resource="soul",
        action="update",
        call=lambda provider: provider.update_soul(payload.content),
    )
    return _view(gateway_id, profile_name, snapshot)


@router.get("/memory", response_model=AdminResourceView)
async def memory(
    gateway_id: str,
    profile_name: ResourceId,
    request: Request,
    _: User = Depends(current_admin),
    db: Session = Depends(get_db),
) -> AdminResourceView:
    snapshot = await _service(request).read(
        db,
        gateway_id=gateway_id,
        profile_name=profile_name,
        capability="memory.get",
        call=lambda provider: provider.get_memory(),
    )
    return _view(gateway_id, profile_name, snapshot)


@router.patch("/memory/provider", response_model=AdminResourceView)
async def set_memory_provider(
    gateway_id: str,
    profile_name: ResourceId,
    payload: MemoryProviderMutation,
    request: Request,
    _: AuthSession = Depends(require_csrf),
    __: str = Depends(require_idempotency),
    actor: User = Depends(current_admin),
    db: Session = Depends(get_db),
) -> AdminResourceView:
    snapshot = await _service(request).mutate(
        db,
        actor,
        gateway_id=gateway_id,
        profile_name=profile_name,
        capability="memory.provider.set",
        resource="memory",
        action="provider_set",
        call=lambda provider: provider.set_memory_provider(payload.provider),
    )
    return _view(gateway_id, profile_name, snapshot)


@router.post("/memory/reset", response_model=AdminResourceView)
async def reset_memory(
    gateway_id: str,
    profile_name: ResourceId,
    payload: MemoryResetMutation,
    request: Request,
    _: AuthSession = Depends(require_csrf),
    __: str = Depends(require_idempotency),
    actor: User = Depends(current_admin),
    db: Session = Depends(get_db),
) -> AdminResourceView:
    snapshot = await _service(request).mutate(
        db,
        actor,
        gateway_id=gateway_id,
        profile_name=profile_name,
        capability="memory.reset",
        resource="memory",
        action="reset",
        call=lambda provider: provider.reset_memory(payload.target),
    )
    return _view(gateway_id, profile_name, snapshot)


@router.get("/skills", response_model=AdminResourceView)
async def skills(
    gateway_id: str,
    profile_name: ResourceId,
    request: Request,
    _: User = Depends(current_admin),
    db: Session = Depends(get_db),
) -> AdminResourceView:
    snapshot = await _service(request).read(
        db,
        gateway_id=gateway_id,
        profile_name=profile_name,
        capability="skills.list",
        call=lambda provider: provider.list_skills(),
    )
    return _view(gateway_id, profile_name, snapshot)


@router.patch("/skills/{skill_name}", response_model=AdminResourceView)
async def toggle_skill(
    gateway_id: str,
    profile_name: ResourceId,
    skill_name: ResourceId,
    payload: ToggleMutation,
    request: Request,
    _: AuthSession = Depends(require_csrf),
    __: str = Depends(require_idempotency),
    actor: User = Depends(current_admin),
    db: Session = Depends(get_db),
) -> AdminResourceView:
    snapshot = await _service(request).mutate(
        db,
        actor,
        gateway_id=gateway_id,
        profile_name=profile_name,
        capability="skills.toggle",
        resource="skills",
        action="toggle",
        target_id=skill_name,
        call=lambda provider: provider.toggle_skill(skill_name, payload.enabled),
    )
    return _view(gateway_id, profile_name, snapshot)


@router.get("/toolsets", response_model=AdminResourceView)
async def toolsets(
    gateway_id: str,
    profile_name: ResourceId,
    request: Request,
    _: User = Depends(current_admin),
    db: Session = Depends(get_db),
) -> AdminResourceView:
    snapshot = await _service(request).read(
        db,
        gateway_id=gateway_id,
        profile_name=profile_name,
        capability="toolsets.list",
        call=lambda provider: provider.list_toolsets(),
    )
    return _view(gateway_id, profile_name, snapshot)


@router.patch("/toolsets/{toolset_name}", response_model=AdminResourceView)
async def toggle_toolset(
    gateway_id: str,
    profile_name: ResourceId,
    toolset_name: ResourceId,
    payload: ToggleMutation,
    request: Request,
    _: AuthSession = Depends(require_csrf),
    __: str = Depends(require_idempotency),
    actor: User = Depends(current_admin),
    db: Session = Depends(get_db),
) -> AdminResourceView:
    snapshot = await _service(request).mutate(
        db,
        actor,
        gateway_id=gateway_id,
        profile_name=profile_name,
        capability="toolsets.toggle",
        resource="toolsets",
        action="toggle",
        target_id=toolset_name,
        call=lambda provider: provider.toggle_toolset(toolset_name, payload.enabled),
    )
    return _view(gateway_id, profile_name, snapshot)


@router.get("/mcp/servers", response_model=AdminResourceView)
async def mcp_servers(
    gateway_id: str,
    profile_name: ResourceId,
    request: Request,
    _: User = Depends(current_admin),
    db: Session = Depends(get_db),
) -> AdminResourceView:
    snapshot = await _service(request).read(
        db,
        gateway_id=gateway_id,
        profile_name=profile_name,
        capability="mcp.list",
        call=lambda provider: provider.list_mcp_servers(),
    )
    return _view(gateway_id, profile_name, snapshot)


@router.post("/mcp/servers", response_model=AdminResourceView, status_code=201)
async def create_mcp_server(
    gateway_id: str,
    profile_name: ResourceId,
    payload: McpServerCreate,
    request: Request,
    _: AuthSession = Depends(require_csrf),
    __: str = Depends(require_idempotency),
    actor: User = Depends(current_admin),
    db: Session = Depends(get_db),
) -> AdminResourceView:
    snapshot = await _service(request).mutate(
        db,
        actor,
        gateway_id=gateway_id,
        profile_name=profile_name,
        capability="mcp.create",
        resource="mcp",
        action="create",
        target_id=payload.name,
        call=lambda provider: provider.create_mcp_server(payload.upstream()),
    )
    return _view(gateway_id, profile_name, snapshot)


@router.patch("/mcp/servers/{server_name}", response_model=AdminResourceView)
async def toggle_mcp_server(
    gateway_id: str,
    profile_name: ResourceId,
    server_name: ResourceId,
    payload: ToggleMutation,
    request: Request,
    _: AuthSession = Depends(require_csrf),
    __: str = Depends(require_idempotency),
    actor: User = Depends(current_admin),
    db: Session = Depends(get_db),
) -> AdminResourceView:
    snapshot = await _service(request).mutate(
        db,
        actor,
        gateway_id=gateway_id,
        profile_name=profile_name,
        capability="mcp.toggle",
        resource="mcp",
        action="toggle",
        target_id=server_name,
        call=lambda provider: provider.toggle_mcp_server(server_name, payload.enabled),
    )
    return _view(gateway_id, profile_name, snapshot)


@router.delete("/mcp/servers/{server_name}", response_model=AdminResourceView)
async def delete_mcp_server(
    gateway_id: str,
    profile_name: ResourceId,
    server_name: ResourceId,
    request: Request,
    _: AuthSession = Depends(require_csrf),
    __: str = Depends(require_idempotency),
    actor: User = Depends(current_admin),
    db: Session = Depends(get_db),
) -> AdminResourceView:
    snapshot = await _service(request).mutate(
        db,
        actor,
        gateway_id=gateway_id,
        profile_name=profile_name,
        capability="mcp.delete",
        resource="mcp",
        action="delete",
        target_id=server_name,
        call=lambda provider: provider.delete_mcp_server(server_name),
    )
    return _view(gateway_id, profile_name, snapshot)


@router.post("/mcp/servers/{server_name}/test", response_model=AdminResourceView)
async def test_mcp_server(
    gateway_id: str,
    profile_name: ResourceId,
    server_name: ResourceId,
    request: Request,
    _: AuthSession = Depends(require_csrf),
    __: str = Depends(require_idempotency),
    actor: User = Depends(current_admin),
    db: Session = Depends(get_db),
) -> AdminResourceView:
    snapshot = await _service(request).mutate(
        db,
        actor,
        gateway_id=gateway_id,
        profile_name=profile_name,
        capability="mcp.test",
        resource="mcp",
        action="test",
        target_id=server_name,
        call=lambda provider: provider.test_mcp_server(server_name),
    )
    return _view(gateway_id, profile_name, snapshot)


@router.get("/channels", response_model=AdminResourceView)
async def channels(
    gateway_id: str,
    profile_name: ResourceId,
    request: Request,
    _: User = Depends(current_admin),
    db: Session = Depends(get_db),
) -> AdminResourceView:
    snapshot = await _service(request).read(
        db,
        gateway_id=gateway_id,
        profile_name=profile_name,
        capability="channels.list",
        call=lambda provider: provider.list_channels(),
    )
    return _view(gateway_id, profile_name, snapshot)


@router.patch("/channels/{channel_name}", response_model=AdminResourceView)
async def update_channel(
    gateway_id: str,
    profile_name: ResourceId,
    channel_name: ResourceId,
    payload: ChannelMutation,
    request: Request,
    _: AuthSession = Depends(require_csrf),
    __: str = Depends(require_idempotency),
    actor: User = Depends(current_admin),
    db: Session = Depends(get_db),
) -> AdminResourceView:
    snapshot = await _service(request).mutate(
        db,
        actor,
        gateway_id=gateway_id,
        profile_name=profile_name,
        capability="channels.update",
        resource="channels",
        action="update",
        target_id=channel_name,
        call=lambda provider: provider.update_channel(channel_name, payload.upstream()),
    )
    return _view(gateway_id, profile_name, snapshot)


@router.post("/channels/{channel_name}/test", response_model=AdminResourceView)
async def test_channel(
    gateway_id: str,
    profile_name: ResourceId,
    channel_name: ResourceId,
    request: Request,
    _: AuthSession = Depends(require_csrf),
    __: str = Depends(require_idempotency),
    actor: User = Depends(current_admin),
    db: Session = Depends(get_db),
) -> AdminResourceView:
    snapshot = await _service(request).mutate(
        db,
        actor,
        gateway_id=gateway_id,
        profile_name=profile_name,
        capability="channels.test",
        resource="channels",
        action="test",
        target_id=channel_name,
        call=lambda provider: provider.test_channel(channel_name),
    )
    return _view(gateway_id, profile_name, snapshot)


@router.get("/usage", response_model=AdminResourceView)
async def usage(
    gateway_id: str,
    profile_name: ResourceId,
    request: Request,
    days: int = Query(default=30, ge=1, le=365),
    _: User = Depends(current_admin),
    db: Session = Depends(get_db),
) -> AdminResourceView:
    snapshot = await _service(request).read(
        db,
        gateway_id=gateway_id,
        profile_name=profile_name,
        capability="usage.get",
        call=lambda provider: provider.get_usage(days=days),
    )
    return _view(gateway_id, profile_name, snapshot)


@router.get("/secrets", response_model=AdminResourceView)
async def secrets(
    gateway_id: str,
    profile_name: ResourceId,
    request: Request,
    _: User = Depends(current_admin),
    db: Session = Depends(get_db),
) -> AdminResourceView:
    snapshot = await _service(request).read(
        db,
        gateway_id=gateway_id,
        profile_name=profile_name,
        capability="secrets.list",
        call=lambda provider: provider.list_secrets(),
    )
    return _view(gateway_id, profile_name, snapshot)


@router.patch("/secrets/{secret_name}", response_model=AdminResourceView)
async def set_secret(
    gateway_id: str,
    profile_name: ResourceId,
    secret_name: SecretName,
    payload: SecretMutation,
    request: Request,
    _: AuthSession = Depends(require_csrf),
    __: str = Depends(require_idempotency),
    actor: User = Depends(current_admin),
    db: Session = Depends(get_db),
) -> AdminResourceView:
    snapshot = await _service(request).mutate(
        db,
        actor,
        gateway_id=gateway_id,
        profile_name=profile_name,
        capability="secrets.set",
        resource="secrets",
        action="set",
        target_id=secret_name,
        call=lambda provider: provider.set_secret(
            secret_name, payload.value.get_secret_value()
        ),
    )
    return _view(gateway_id, profile_name, snapshot)


@router.delete("/secrets/{secret_name}", response_model=AdminResourceView)
async def delete_secret(
    gateway_id: str,
    profile_name: ResourceId,
    secret_name: SecretName,
    request: Request,
    _: AuthSession = Depends(require_csrf),
    __: str = Depends(require_idempotency),
    actor: User = Depends(current_admin),
    db: Session = Depends(get_db),
) -> AdminResourceView:
    snapshot = await _service(request).mutate(
        db,
        actor,
        gateway_id=gateway_id,
        profile_name=profile_name,
        capability="secrets.delete",
        resource="secrets",
        action="delete",
        target_id=secret_name,
        call=lambda provider: provider.delete_secret(secret_name),
    )
    return _view(gateway_id, profile_name, snapshot)
