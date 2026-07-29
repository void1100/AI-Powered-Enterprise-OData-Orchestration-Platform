"""Services router — OData service registration, discovery, and health checks."""
import asyncio
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Request

from app.auth import get_current_user
from app.schemas.models import ServiceInfo, ServiceRegister
from app.services.service_manager import service_manager
from app.routers.deps import probe_service

router = APIRouter(prefix="/services", tags=["services"])


def _to_service_info(svc: Dict[str, Any]) -> ServiceInfo:
    return ServiceInfo(
        id=svc["id"],
        name=svc["name"],
        base_url=svc["base_url"],
        description=svc["description"],
        entity_sets=[es["name"] for es in svc["metadata"].get("entity_sets", [])],
        healthy_entity_sets=svc.get("healthy_entity_sets"),
        unhealthy_entity_sets=svc.get("unhealthy_entity_sets"),
    )


@router.get("", response_model=List[ServiceInfo])
async def get_services():
    if not service_manager._services:
        await service_manager.recover_from_graph()
    return service_manager.list_services()


@router.get("/health")
async def services_health():
    services = service_manager.list_services()
    results = await asyncio.gather(*[probe_service(s) for s in services])
    return {"services": results}


@router.get("/{service_id}", response_model=ServiceInfo)
async def get_service(service_id: str):
    svc = service_manager.get_service(service_id)
    if not svc:
        raise HTTPException(status_code=404, detail="Service not found")
    return _to_service_info(svc)


@router.post("", response_model=ServiceInfo)
async def register_service(payload: ServiceRegister, request: Request):
    user = get_current_user(request)
    if not user or user.get("role") not in ("super_admin", "admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    auth_type = payload.auth_type
    auth_config = {}
    if auth_type == "basic" and payload.auth_username:
        auth_config = {"username": payload.auth_username, "password": payload.auth_password or ""}
    elif auth_type == "bearer" and payload.auth_token:
        auth_config = {"token": payload.auth_token}
    elif auth_type == "api_key" and payload.auth_api_key:
        auth_config = {"api_key": payload.auth_api_key, "header_name": payload.auth_header_name or "X-API-Key"}
    svc = await service_manager.register_service(
        service_id=payload.id,
        name=payload.name,
        base_url=payload.base_url,
        description=payload.description,
        auth_type=auth_type,
        auth_config=auth_config if auth_config else None,
    )
    return _to_service_info(svc)


@router.delete("/{service_id}")
async def delete_service(service_id: str, request: Request):
    user = get_current_user(request)
    if not user or user.get("role") not in ("super_admin", "admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    if service_id not in service_manager._services:
        raise HTTPException(status_code=404, detail="Service not found")
    service_manager.delete_service(service_id)
    return {"deleted": service_id}


@router.post("/{service_id}/refresh", response_model=ServiceInfo)
async def refresh_service(service_id: str, request: Request):
    user = get_current_user(request)
    if not user or user.get("role") not in ("super_admin", "admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    svc = await service_manager.refresh_service(service_id)
    if not svc:
        raise HTTPException(status_code=404, detail="Service not found")
    return _to_service_info(svc)


@router.post("/{service_id}/healthcheck")
async def healthcheck_service(service_id: str, request: Request):
    user = get_current_user(request)
    if not user or user.get("role") not in ("super_admin", "admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    if service_id not in service_manager._services:
        raise HTTPException(status_code=404, detail="Service not found")
    await service_manager._health_check_entities(service_id)
    svc = service_manager._services[service_id]
    return {
        "service_id": service_id,
        "total": len(svc.get("metadata", {}).get("entity_sets", [])),
        "healthy": len(svc.get("healthy_entity_sets", [])),
        "unhealthy": len(svc.get("unhealthy_entity_sets", [])),
        "healthy_entity_sets": svc.get("healthy_entity_sets", []),
        "unhealthy_entity_sets": svc.get("unhealthy_entity_sets", []),
    }
