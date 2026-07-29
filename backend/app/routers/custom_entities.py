"""Custom entities router — admin-gated CRUD for virtual (custom) OData entities."""
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.auth import get_current_user
from app.services.service_manager import service_manager

router = APIRouter(prefix="/custom_entities", tags=["custom_entities"])


class CustomEntityCreate(BaseModel):
    name: str
    base_entity_set: str
    description: str = ""
    default_filter: str = ""
    allowed_columns: List[str] = []


class CustomEntityUpdate(BaseModel):
    description: Optional[str] = None
    default_filter: Optional[str] = None
    allowed_columns: Optional[List[str]] = None


def _require_admin(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    if user.get("role", "viewer") not in ("super_admin", "admin"):
        raise HTTPException(status_code=403, detail="Only admins can manage custom entities")
    return user


@router.get("")
async def list_custom_entities(service_id: Optional[str] = None):
    return service_manager.list_custom_entities(service_id)


@router.post("")
async def create_custom_entity(payload: CustomEntityCreate, request: Request):
    user = _require_admin(request)
    try:
        # If exactly one service is registered, default to it; otherwise derive from name prefix
        services = list(service_manager._services.keys())
        svc_id = services[0] if len(services) == 1 else payload.name.split("_")[0]
        entity = service_manager.register_custom_entity(
            service_id=svc_id,
            name=payload.name,
            base_entity_set=payload.base_entity_set,
            description=payload.description,
            default_filter=payload.default_filter,
            allowed_columns=payload.allowed_columns,
            created_by=user.get("username", "admin"),
        )
        return entity
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{service_id}")
async def create_custom_entity_for_service(service_id: str, payload: CustomEntityCreate, request: Request):
    user = _require_admin(request)
    try:
        entity = service_manager.register_custom_entity(
            service_id=service_id,
            name=payload.name,
            base_entity_set=payload.base_entity_set,
            description=payload.description,
            default_filter=payload.default_filter,
            allowed_columns=payload.allowed_columns,
            created_by=user.get("username", "admin"),
        )
        return entity
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch("/{service_id}/{name}")
async def update_custom_entity(service_id: str, name: str, payload: CustomEntityUpdate, request: Request):
    _require_admin(request)
    entity = service_manager.get_custom_entity(service_id, name)
    if not entity:
        raise HTTPException(status_code=404, detail="Custom entity not found")
    if payload.description is not None:
        entity["description"] = payload.description
    if payload.default_filter is not None:
        entity["default_filter"] = payload.default_filter
    if payload.allowed_columns is not None:
        entity["allowed_columns"] = payload.allowed_columns
    return entity


@router.delete("/{service_id}/{name}")
async def delete_custom_entity(service_id: str, name: str, request: Request):
    _require_admin(request)
    if service_manager.delete_custom_entity(service_id, name):
        return {"deleted": name}
    raise HTTPException(status_code=404, detail="Custom entity not found")
