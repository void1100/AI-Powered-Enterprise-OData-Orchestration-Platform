import os
import sys
import asyncio
import time
import uuid
import re
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from app.config import settings
from app.auth import get_current_user
from app.schemas.models import (
    ChatRequest,
    ChatResponse,
    MessageInfo,
    MCPCallRequest,
    MCPCallResponse,
    Plan,
    ServiceInfo,
    ServiceRegister,
    SessionCreate,
    SessionInfo,
    TableData,
)
from app.services.service_manager import service_manager
from app.services.column_filter import filter_columns
from app.agents.orchestrator import orchestrator
from app.agents.policy_engine import policy_engine
from app.agents.reasoning_engine import llm_engine
from app.db.sqlite_store import (
    add_message,
    add_run,
    create_session,
    delete_session,
    get_messages,
    list_sessions,
    rename_session,
    touch_session,
)
from app.db.usage_tracker import log_usage
from app.mcp.mcp_server import mcp_server


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _recovery_complete
    logger.info("Starting OData Orchestration backend...")
    policy_engine.ensure_default_roles()
    async def _run_recovery():
        global _recovery_complete
        try:
            await service_manager.recover_from_graph()
        except Exception as e:
            logger.warning(f"Background recovery failed: {e}")
        finally:
            _recovery_complete = True
            logger.info("Service recovery complete — server fully ready.")
    asyncio.create_task(_run_recovery())
    yield
    logger.info("Shutting down.")


_recovery_complete = False


app = FastAPI(title="Advanced OData Service Orchestration", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register admin/auth routes
from app.admin.routes import router as admin_router
app.include_router(admin_router, tags=["auth", "admin"])


@app.get("/")
async def root():
    return {
        "name": "Advanced OData Service Orchestration",
        "version": "1.0.0",
        "status": "ok",
        "neo4j_connected": service_manager.graph().is_available(),
        "endpoints": [
            "/services",
            "/chat",
            "/sessions",
            "/mcp",
            "/roles",
        ],
    }


@app.get("/health")
async def health():
    from app.services.query_optimizer import query_optimizer
    from app.services.query_rag import query_plan_rag
    return {"status": "ok", "optimizer": query_optimizer.stats, "rag": query_plan_rag.get_stats()}


@app.get("/ready")
async def ready():
    if not _recovery_complete:
        raise HTTPException(status_code=503, detail="Services still loading")
    return {"status": "ready"}


@app.get("/services", response_model=List[ServiceInfo])
async def get_services():
    if not service_manager._services:
        await service_manager.recover_from_graph()
    return service_manager.list_services()


@app.post("/services", response_model=ServiceInfo)
async def register_service(payload: ServiceRegister, request: Request):
    user = get_current_user(request)
    if not user or user.get("role") not in ("super_admin", "admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    # Build auth config from payload
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
    return ServiceInfo(
        id=svc["id"],
        name=svc["name"],
        base_url=svc["base_url"],
        description=svc["description"],
        entity_sets=[es["name"] for es in svc["metadata"].get("entity_sets", [])],
    )


@app.delete("/services/{service_id}")
async def delete_service(service_id: str, request: Request):
    user = get_current_user(request)
    if not user or user.get("role") not in ("super_admin", "admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    if service_id not in service_manager._services:
        raise HTTPException(status_code=404, detail="Service not found")
    service_manager.delete_service(service_id)
    return {"deleted": service_id}


@app.post("/services/{service_id}/refresh", response_model=ServiceInfo)
async def refresh_service(service_id: str, request: Request):
    user = get_current_user(request)
    if not user or user.get("role") not in ("super_admin", "admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    svc = await service_manager.refresh_service(service_id)
    if not svc:
        raise HTTPException(status_code=404, detail="Service not found")
    return ServiceInfo(
        id=svc["id"],
        name=svc["name"],
        base_url=svc["base_url"],
        description=svc["description"],
        entity_sets=[es["name"] for es in svc["metadata"].get("entity_sets", [])],
    )


async def _probe_service(svc: Dict[str, Any]) -> Dict[str, Any]:
    base = (svc.get("base_url") or "").rstrip("/")
    # SAP CPI pattern: metadata=true in query string — use as-is
    if "metadata=true" in base.lower():
        url = base
    else:
        url = f"{base}/$metadata"
    t0 = time.perf_counter()
    try:
        # Include auth headers from service registration
        auth_type = svc.get("auth_type")
        auth_config = svc.get("auth_config")
        headers = {"Accept": "application/xml"}
        if auth_type == "basic" and auth_config:
            import base64
            user = auth_config.get("username", "")
            pwd = auth_config.get("password", "")
            if user:
                token = base64.b64encode(f"{user}:{pwd}".encode()).decode()
                headers["Authorization"] = f"Basic {token}"
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
        latency_ms = int((time.perf_counter() - t0) * 1000)
        if resp.status_code == 200:
            status = "healthy"
        elif 500 <= resp.status_code < 600:
            status = "down"
        else:
            status = "degraded"
        return {
            "id": svc["id"],
            "name": svc["name"],
            "status": status,
            "http_status": resp.status_code,
            "latency_ms": latency_ms,
        }
    except Exception as e:
        latency_ms = int((time.perf_counter() - t0) * 1000)
        return {
            "id": svc["id"],
            "name": svc["name"],
            "status": "down",
            "http_status": None,
            "latency_ms": latency_ms,
            "error": str(e)[:200],
        }


@app.get("/services/health")
async def services_health():
    services = service_manager.list_services()
    results = await asyncio.gather(*[_probe_service(s) for s in services])
    return {"services": results}


def _build_column_labels(service_id: str, entity_set: str, columns: list) -> dict:
    """Build column_labels dict from entity metadata using direct O(1) lookup."""
    svc_raw = service_manager.get_service(service_id)
    if not svc_raw:
        return {}
    entity_labels = {}
    meta = svc_raw.get("metadata", {})
    for es in meta.get("entity_sets", []):
        es_name = es["name"]
        et_name = es.get("entity_type", es_name)
        et = next((e for e in meta.get("entity_types", []) if e["name"] == et_name), None)
        if not et and "." in et_name:
            local_name = et_name.rsplit(".", 1)[-1]
            et = next((e for e in meta.get("entity_types", []) if e["name"] == local_name), None)
        if not et:
            et = next((e for e in meta.get("entity_types", []) if et_name.endswith(e["name"])), None)
        prop_labels = {p["name"]: p.get("label", "") for p in (et or {}).get("properties", [])}
        entity_labels[es_name] = prop_labels
    labels_info = entity_labels.get(entity_set, {})
    if not labels_info and entity_set:
        es_lower = entity_set.lower()
        for key, val in entity_labels.items():
            if key.lower() == es_lower or key.lower().endswith(es_lower) or es_lower.endswith(key.lower()):
                labels_info = val
                break
    return {col: labels_info[col] for col in columns if col in labels_info and labels_info[col]}


# --- Entity Selector Endpoints ---

from app.services.entity_selector import entity_selector, classify_property
from app.schemas.models import AutoJoinRequest, EntityJoinExecuteRequest

@app.get("/entities/{service_id}")
async def get_service_entities(service_id: str):
    """Get entity list with properties for a specific service."""
    services = service_manager.list_services()
    svc = next((s for s in services if s["id"] == service_id), None)
    if not svc:
        raise HTTPException(status_code=404, detail=f"Service '{service_id}' not found")
    entities = []
    entity_labels = svc.get("entity_labels", {})
    for es_name in svc.get("entity_sets", []):
        props = svc.get("entity_properties", {}).get(es_name, [])
        labels_info = entity_labels.get(es_name, {})
        entity_label = labels_info.get("entity_label", "")
        prop_labels = labels_info.get("property_labels", {})
        labeled_props = []
        for p in props:
            if isinstance(p, str):
                sap_label = prop_labels.get(p, "")
                labeled_props.append({
                    "name": p,
                    "label": classify_property(p),
                    "display_label": sap_label or p,
                })
            elif isinstance(p, dict):
                sap_label = p.get("label", "") or prop_labels.get(p.get("name", ""), "")
                name = p.get("name", "")
                labeled_props.append({
                    **p,
                    "label": classify_property(name),
                    "display_label": sap_label or name,
                })
            else:
                labeled_props.append({"name": str(p), "label": "Attribute"})
        entities.append({
            "name": es_name,
            "label": entity_label,
            "properties": labeled_props,
            "property_count": len(labeled_props),
        })
    return {"service_id": service_id, "service_name": svc["name"], "entities": entities}


@app.post("/entities/auto-join")
async def detect_auto_joins(payload: AutoJoinRequest):
    """Detect potential joins between selected entities."""
    entities = []
    services = service_manager.list_services()
    for e in payload.entities:
        svc = next((s for s in services if s["id"] == e.service_id), None)
        if svc:
            props = svc.get("entity_properties", {}).get(e.entity_name, [])
            entities.append({
                "service_id": e.service_id,
                "entity_name": e.entity_name,
                "properties": props,
            })
    if len(entities) < 2:
        return {"joins": [], "message": "Select at least 2 entities to detect joins"}
    joins = entity_selector.detect_joins(entities)
    return {"joins": joins, "entity_count": len(entities)}


@app.post("/entities/execute-join")
async def execute_entity_join(payload: EntityJoinExecuteRequest):
    """Execute a query with selected entities and auto-detected joins."""

    services = service_manager.list_services()
    all_results = []

    # Fetch data for each selected entity
    for e in payload.entities:
        svc = next((s for s in services if s["id"] == e.service_id), None)
        if not svc:
            logger.warning(f"execute-join: service {e.service_id} not found")
            continue
        client = service_manager._clients.get(e.service_id)
        if not client:
            logger.warning(f"execute-join: no client for {e.service_id}")
            continue
        try:
            top = min(payload.top, 200)
            resp = await client.query(entity_set=e.entity_name, top=top)
            rows = client.flatten_odata_value(resp)
            if rows:
                cols = list(rows[0].keys())
                all_results.append({
                    "service_id": e.service_id,
                    "entity_name": e.entity_name,
                    "table": {"columns": cols, "rows": rows},
                })
                logger.info(f"execute-join: fetched {len(rows)} rows from {e.entity_name}")
            else:
                logger.warning(f"execute-join: no rows from {e.entity_name}")
        except Exception as ex:
            logger.warning(f"execute-join: failed to fetch {e.entity_name} from {e.service_id}: {ex}")

    if not all_results:
        return {"error": "No data retrieved from selected entities", "table": TableData().model_dump(), "entity_count": 0, "join_count": 0}

    # Apply joins — chain multiple joins for multi-entity scenarios
    joins = payload.joins or []
    if len(all_results) >= 2 and joins:
        from app.services.cross_service_join import match_join
        # Sort joins by confidence descending
        sorted_joins = sorted(joins, key=lambda j: getattr(j, "confidence", 0) if hasattr(j, "confidence") else (j.get("confidence", 0) if isinstance(j, dict) else 0), reverse=True)
        result_table = all_results[0]["table"]
        used_right_entities = set()
        for join_def in sorted_joins:
            left_key = join_def.left_key if hasattr(join_def, "left_key") else join_def.get("left_key", "") if isinstance(join_def, dict) else ""
            right_key = join_def.right_key if hasattr(join_def, "right_key") else join_def.get("right_key", "") if isinstance(join_def, dict) else ""
            right_entity = join_def.right_entity if hasattr(join_def, "right_entity") else join_def.get("right_entity", "") if isinstance(join_def, dict) else ""
            if not left_key or not right_key:
                continue
            # Find the right entity table (skip if already used)
            right_result = None
            for r in all_results:
                if r["entity_name"] == right_entity and r["entity_name"] not in used_right_entities:
                    right_result = r["table"]
                    used_right_entities.add(r["entity_name"])
                    break
            if not right_result:
                continue
            result_table = match_join(
                result_table,
                right_result,
                left_key=left_key,
                right_key=right_key,
                left_service=all_results[0]["service_id"],
                right_service=right_entity,
            )
        columns = result_table.get("columns", [])
        rows = result_table.get("rows", [])
    elif len(all_results) == 1:
        columns = all_results[0]["table"]["columns"]
        rows = all_results[0]["table"]["rows"]
    else:
        all_cols = []
        for r in all_results:
            for c in r["table"]["columns"]:
                if c not in all_cols:
                    all_cols.append(c)
        columns = all_cols
        rows = []
        for r in all_results:
            for row in r["table"]["rows"]:
                merged = {c: row.get(c) for c in all_cols}
                merged["_source_service"] = r["service_id"]
                rows.append(merged)

    entity_service_lookup = {
        e.entity_name: e.service_id
        for e in payload.entities
    }

    # Store successful joins for future reference
    for j in joins:
        le = j.left_entity if hasattr(j, "left_entity") else j.get("left_entity", "") if isinstance(j, dict) else ""
        re_ = j.right_entity if hasattr(j, "right_entity") else j.get("right_entity", "") if isinstance(j, dict) else ""
        lk = j.left_key if hasattr(j, "left_key") else j.get("left_key", "") if isinstance(j, dict) else ""
        rk = j.right_key if hasattr(j, "right_key") else j.get("right_key", "") if isinstance(j, dict) else ""
        left_service = j.left_service if hasattr(j, "left_service") else j.get("left_service", "") if isinstance(j, dict) else ""
        right_service = j.right_service if hasattr(j, "right_service") else j.get("right_service", "") if isinstance(j, dict) else ""
        entity_selector.store_successful_join(
            left_service or entity_service_lookup.get(le, ""),
            le,
            right_service or entity_service_lookup.get(re_, ""),
            re_,
            lk,
            rk,
        )

    # Cap rows to prevent huge cross-products
    MAX_JOIN_ROWS = 100
    if len(rows) > MAX_JOIN_ROWS:
        rows = rows[:MAX_JOIN_ROWS]

    # Filter useless columns
    filtered = filter_columns({"columns": columns, "rows": rows, "row_count": len(rows)})
    columns, rows = filtered["columns"], filtered["rows"]

    table = TableData(columns=columns, rows=rows, row_count=len(rows))
    return {
        "success": True,
        "table": table.model_dump(),
        "entity_count": len(payload.entities),
        "join_count": len(joins),
    }


# --- Custom Entity Endpoints (Admin Only) ---

from pydantic import BaseModel as PydanticBaseModel

class CustomEntityCreate(PydanticBaseModel):
    name: str
    base_entity_set: str
    description: str = ""
    default_filter: str = ""
    allowed_columns: List[str] = []

class CustomEntityUpdate(PydanticBaseModel):
    description: Optional[str] = None
    default_filter: Optional[str] = None
    allowed_columns: Optional[List[str]] = None

@app.get("/custom_entities")
async def list_custom_entities(service_id: Optional[str] = None):
    return service_manager.list_custom_entities(service_id)

@app.post("/custom_entities")
async def create_custom_entity(payload: CustomEntityCreate, request: Request):
    from app.auth import get_current_user
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    role = user.get("role", "viewer")
    if role not in ("super_admin", "admin"):
        raise HTTPException(status_code=403, detail="Only admins can create custom entities")
    try:
        entity = service_manager.register_custom_entity(
            service_id=list(service_manager._services.keys())[0] if len(service_manager._services) == 1 else payload.name.split("_")[0],
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

@app.post("/custom_entities/{service_id}")
async def create_custom_entity_for_service(service_id: str, payload: CustomEntityCreate, request: Request):
    from app.auth import get_current_user
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    role = user.get("role", "viewer")
    if role not in ("super_admin", "admin"):
        raise HTTPException(status_code=403, detail="Only admins can create custom entities")
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

@app.patch("/custom_entities/{service_id}/{name}")
async def update_custom_entity(service_id: str, name: str, payload: CustomEntityUpdate, request: Request):
    from app.auth import get_current_user
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    role = user.get("role", "viewer")
    if role not in ("super_admin", "admin"):
        raise HTTPException(status_code=403, detail="Only admins can update custom entities")
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

@app.delete("/custom_entities/{service_id}/{name}")
async def delete_custom_entity(service_id: str, name: str, request: Request):
    from app.auth import get_current_user
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    role = user.get("role", "viewer")
    if role not in ("super_admin", "admin"):
        raise HTTPException(status_code=403, detail="Only admins can delete custom entities")
    if service_manager.delete_custom_entity(service_id, name):
        return {"deleted": name}
    raise HTTPException(status_code=404, detail="Custom entity not found")


# --- Cross-Service Join Endpoints ---

import uuid as _uuid
from pydantic import BaseModel as _PydanticBase

class JoinCreate(_PydanticBase):
    name: str
    strategy: str  # union, match, enrichment
    left_service: str
    left_entity: str
    left_key: str = ""
    right_service: str
    right_entity: str
    right_key: str = ""
    column_mapping: Dict[str, Dict[str, str]] = {}
    description: str = ""

@app.get("/joins")
async def list_joins(request: Request):
    user = get_current_user(request)
    if not user or user.get("role") not in ("super_admin", "admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    g = service_manager.graph()
    return g.list_joins()

@app.post("/joins")
async def create_join(payload: JoinCreate, request: Request):
    user = get_current_user(request)
    if not user or user.get("role") not in ("super_admin", "admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    if payload.left_service not in service_manager._services:
        raise HTTPException(status_code=400, detail=f"Unknown left service: {payload.left_service}")
    if payload.right_service not in service_manager._services:
        raise HTTPException(status_code=400, detail=f"Unknown right service: {payload.right_service}")
    join_id = str(_uuid.uuid4())[:8]
    join_def = {
        "id": join_id,
        "name": payload.name,
        "strategy": payload.strategy,
        "left_service": payload.left_service,
        "left_entity": payload.left_entity,
        "left_key": payload.left_key,
        "right_service": payload.right_service,
        "right_entity": payload.right_entity,
        "right_key": payload.right_key,
        "column_mapping": payload.column_mapping,
        "description": payload.description,
        "created_by": user.get("username", "admin"),
        "created_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
    }
    g = service_manager.graph()
    g.upsert_join(join_def)
    return join_def

@app.delete("/joins/{join_id}")
async def delete_join(join_id: str, request: Request):
    user = get_current_user(request)
    if not user or user.get("role") not in ("super_admin", "admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    g = service_manager.graph()
    if g.delete_join(join_id):
        return {"deleted": join_id}
    raise HTTPException(status_code=404, detail="Join not found")

@app.patch("/joins/{join_id}")
async def update_join(join_id: str, payload: JoinCreate, request: Request):
    user = get_current_user(request)
    if not user or user.get("role") not in ("super_admin", "admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    g = service_manager.graph()
    existing = g.get_join(join_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Join not found")
    updated = {
        **existing,
        "name": payload.name,
        "strategy": payload.strategy,
        "left_service": payload.left_service,
        "left_entity": payload.left_entity,
        "left_key": payload.left_key,
        "right_service": payload.right_service,
        "right_entity": payload.right_entity,
        "right_key": payload.right_key,
        "column_mapping": payload.column_mapping,
        "description": payload.description,
    }
    g.upsert_join(updated)
    return updated

@app.post("/joins/{join_id}/execute")
async def execute_join(join_id: str, request: Request):
    user = get_current_user(request)
    if not user or user.get("role") not in ("super_admin", "admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    g = service_manager.graph()
    join_def = g.get_join(join_id)
    if not join_def:
        raise HTTPException(status_code=404, detail="Join not found")
    from app.services.cross_service_join import union_join, match_join, enrichment_join
    try:
        left_client = service_manager.get_client(join_def["left_service"])
        right_client = service_manager.get_client(join_def["right_service"])
        if not left_client or not right_client:
            raise HTTPException(status_code=400, detail="Service client not available")
        left_table = await left_client.query(entity_set=join_def["left_entity"], top=200)
        right_table = await right_client.query(entity_set=join_def["right_entity"], top=200)
        left_data = left_client.flatten_odata_value(left_table)
        right_data = right_client.flatten_odata_value(right_table)
        left_cols = list(left_data[0].keys()) if left_data else []
        right_cols = list(right_data[0].keys()) if right_data else []
        strategy = join_def["strategy"]
        if strategy == "union":
            result = union_join(
                [
                    {"service_id": join_def["left_service"], "table": {"columns": left_cols, "rows": left_data}},
                    {"service_id": join_def["right_service"], "table": {"columns": right_cols, "rows": right_data}},
                ],
                column_mapping=join_def.get("column_mapping"),
            )
        elif strategy == "match":
            result = match_join(
                {"columns": left_cols, "rows": left_data},
                {"columns": right_cols, "rows": right_data},
                left_key=join_def["left_key"],
                right_key=join_def["right_key"],
                left_service=join_def["left_service"],
                right_service=join_def["right_service"],
            )
        elif strategy == "enrichment":
            result = enrichment_join(
                {"columns": left_cols, "rows": left_data},
                {"columns": right_cols, "rows": right_data},
                primary_key=join_def["left_key"],
                secondary_key=join_def["right_key"],
                primary_service=join_def["left_service"],
                secondary_service=join_def["right_service"],
            )
        else:
            raise HTTPException(status_code=400, detail=f"Unknown strategy: {strategy}")
        return {"join": join_def, "result": result}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Join execution failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/joins/{join_id}/chat")
async def join_chat(join_id: str, request: Request):
    user = get_current_user(request)
    if not user or user.get("role") not in ("super_admin", "admin"):
        raise HTTPException(status_code=403, detail="Admin access required")

    body = await request.json()
    query = body.get("query", "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="Query is required")

    g = service_manager.graph()
    join_def = g.get_join(join_id)
    if not join_def:
        raise HTTPException(status_code=404, detail="Join not found")

    from app.services.cross_service_join import union_join, match_join, enrichment_join
    left_client = service_manager.get_client(join_def["left_service"])
    right_client = service_manager.get_client(join_def["right_service"])
    if not left_client or not right_client:
        raise HTTPException(status_code=400, detail="Service client not available")

    left_table = await left_client.query(entity_set=join_def["left_entity"], top=200)
    right_table = await right_client.query(entity_set=join_def["right_entity"], top=200)
    left_data = left_client.flatten_odata_value(left_table)
    right_data = right_client.flatten_odata_value(right_table)
    left_cols = list(left_data[0].keys()) if left_data else []
    right_cols = list(right_data[0].keys()) if right_data else []

    strategy = join_def["strategy"]
    if strategy == "union":
        result = union_join(
            [
                {"service_id": join_def["left_service"], "table": {"columns": left_cols, "rows": left_data}},
                {"service_id": join_def["right_service"], "table": {"columns": right_cols, "rows": right_data}},
            ],
            column_mapping=join_def.get("column_mapping"),
        )
    elif strategy == "match":
        result = match_join(
            {"columns": left_cols, "rows": left_data},
            {"columns": right_cols, "rows": right_data},
            left_key=join_def["left_key"],
            right_key=join_def["right_key"],
            left_service=join_def["left_service"],
            right_service=join_def["right_service"],
        )
    elif strategy == "enrichment":
        result = enrichment_join(
            {"columns": left_cols, "rows": left_data},
            {"columns": right_cols, "rows": right_data},
            primary_key=join_def["left_key"],
            secondary_key=join_def["right_key"],
            primary_service=join_def["left_service"],
            secondary_service=join_def["right_service"],
        )
    else:
        raise HTTPException(status_code=400, detail=f"Unknown strategy: {strategy}")

    rows = result.get("rows", [])
    cols = result.get("columns", [])
    important_cols = [c for c in cols if not c.startswith("@odata") and c not in ("Emails", "AddressInfo", "Concurrency", "Photo", "Notes", "PhotoPath")]

    import re as _re
    filter_match = _re.search(r'(?:where|whose|filter|with)\s+(\w+)\s*(>|<|>=|<=|!=|=|==)\s*([\d.]+)', query, _re.IGNORECASE)
    filtered_rows = rows
    filter_info = None
    if filter_match:
        col_name = filter_match.group(1)
        op = filter_match.group(2)
        val = float(filter_match.group(3))
        matched_col = None
        for c in important_cols:
            if c.lower() == col_name.lower():
                matched_col = c
                break
        if matched_col:
            def _check(row):
                rv = row.get(matched_col)
                if rv is None:
                    return False
                try:
                    rv = float(rv)
                except (ValueError, TypeError):
                    return False
                if op == ">": return rv > val
                if op == "<": return rv < val
                if op == ">=": return rv >= val
                if op == "<=": return rv <= val
                if op in ("!=", "<>"): return rv != val
                return rv == val
            filtered_rows = [r for r in rows if _check(r)]
            filter_info = f"{matched_col} {op} {val}"

    agg_match = _re.search(r'(sum|total|average|avg|min|minimum|max|maximum|count)\s+(?:of\s+)?(\w+)', query, _re.IGNORECASE)
    if agg_match:
        agg_func = agg_match.group(1).lower()
        agg_col_name = agg_match.group(2)
        matched_agg_col = None
        for c in important_cols:
            if c.lower() == agg_col_name.lower():
                matched_agg_col = c
                break
        if matched_agg_col:
            nums = []
            for r in filtered_rows:
                v = r.get(matched_agg_col)
                if v is not None:
                    try:
                        nums.append(float(v))
                    except (ValueError, TypeError):
                        pass
            if nums:
                if agg_func in ("sum", "total"):
                    result_val = round(sum(nums), 2)
                    answer = f"Sum of {matched_agg_col}{(' where ' + filter_info) if filter_info else ''}: {result_val}"
                elif agg_func in ("average", "avg"):
                    result_val = round(sum(nums) / len(nums), 2)
                    answer = f"Average of {matched_agg_col}{(' where ' + filter_info) if filter_info else ''}: {result_val} (from {len(nums)} values)"
                elif agg_func in ("min", "minimum"):
                    result_val = min(nums)
                    answer = f"Minimum of {matched_agg_col}{(' where ' + filter_info) if filter_info else ''}: {result_val}"
                elif agg_func in ("max", "maximum"):
                    result_val = max(nums)
                    answer = f"Maximum of {matched_agg_col}{(' where ' + filter_info) if filter_info else ''}: {result_val}"
                elif agg_func == "count":
                    result_val = len(nums)
                    answer = f"Count of {matched_agg_col}{(' where ' + filter_info) if filter_info else ''}: {result_val}"
                else:
                    answer = f"Could not compute {agg_func} for {matched_agg_col}"
                return {
                    "answer": answer,
                    "provider": "computed",
                    "join_name": join_def["name"],
                    "row_count": len(rows),
                }

    sample_rows = filtered_rows[:50]
    data_summary = " | ".join(important_cols) + "\n"
    data_summary += "\n".join(" | ".join(str(r.get(c, ""))[:30] for c in important_cols) for r in sample_rows)
    if len(filtered_rows) > 50:
        data_summary += f"\n... ({len(filtered_rows)} total rows)"

    system_prompt = (
        "You are a data analyst. Answer questions about this cross-service join result.\n"
        f"Join: {join_def['name']} ({strategy})\n"
        f"Left: {join_def['left_service']}.{join_def['left_entity']}\n"
        f"Right: {join_def['right_service']}.{join_def['right_entity']}\n"
        f"Columns: {', '.join(important_cols)}\n"
        f"Total rows: {len(rows)}\n"
        + (f"Filter applied: {filter_info} → {len(filtered_rows)} matching rows\n" if filter_info else "")
        + "Data sample:\n" + data_summary + "\n\n"
        "Be concise. Answer based on this data."
    )

    try:
        from app.agents.reasoning_engine import llm_engine
        response = await llm_engine.generate(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": query},
            ],
            temperature=0.3,
            max_tokens=1000,
        )
        answer = response.get("content", "No response from LLM.")
        provider = response.get("provider", "unknown")
        wants_table = bool(_re.search(r'show|list|display|details|all rows|records|entries|table|export|csv', query, _re.IGNORECASE))
        is_count = bool(_re.search(r'^(?:how many|count|total|what is the number|number of)', query, _re.IGNORECASE))
        resp = {
            "answer": answer,
            "provider": provider,
            "join_name": join_def["name"],
            "row_count": len(rows),
        }
        if filter_info and filtered_rows and wants_table and not is_count:
            resp["table"] = {"columns": important_cols, "rows": filtered_rows[:200], "row_count": len(filtered_rows), "truncated": len(filtered_rows) > 200, "total_count": len(filtered_rows)}
            resp["summary"] = f"Filtered by {filter_info}: {len(filtered_rows)} rows matching"
        return resp
    except Exception as e:
        logger.error(f"Join chat failed: {e}")
        raise HTTPException(status_code=500, detail=f"LLM Error: {str(e)}")


@app.get("/roles")
async def get_roles():
    return policy_engine.list_roles()


LLM_CATALOG = [
    {"id": "mock", "provider": "mock", "label": "Mock (no LLM call)", "model": "mock", "requires": []},
    {"id": "openai-gpt-4o-mini", "provider": "openai", "label": "OpenAI: GPT-4o mini (fast, cheap)", "model": "gpt-4o-mini", "requires": ["openai_key"]},
    {"id": "openai-gpt-4o", "provider": "openai", "label": "OpenAI: GPT-4o (smartest)", "model": "gpt-4o", "requires": ["openai_key"]},
    {"id": "openai-gpt-3.5-turbo", "provider": "openai", "label": "OpenAI: GPT-3.5 Turbo (legacy)", "model": "gpt-3.5-turbo", "requires": ["openai_key"]},
    {"id": "groq-llama-3.3-70b", "provider": "openai", "label": "Groq: Llama 3.3 70B Versatile", "model": "llama-3.3-70b-versatile", "requires": ["openai_key", "groq_base_url"]},
    {"id": "groq-llama-3.1-8b", "provider": "openai", "label": "Groq: Llama 3.1 8B Instant (fastest)", "model": "llama-3.1-8b-instant", "requires": ["openai_key", "groq_base_url"]},
    {"id": "groq-mixtral-8x7b", "provider": "openai", "label": "Groq: Mixtral 8x7B (32k ctx)", "model": "mixtral-8x7b-32768", "requires": ["openai_key", "groq_base_url"]},
    {"id": "gemini-flash", "provider": "gemini", "label": "Gemini: Flash (latest)", "model": "gemini-flash-latest", "requires": ["gemini_key"]},
    {"id": "gemini-2.0-flash", "provider": "gemini", "label": "Gemini: 2.0 Flash", "model": "gemini-2.0-flash", "requires": ["gemini_key"]},
    {"id": "openrouter-minimax-m3", "provider": "openrouter", "label": "OpenRouter: MiniMax M3", "model": "minimax/minimax-m3", "requires": ["openrouter_key"]},
    {"id": "openrouter-deepseek-r1", "provider": "openrouter", "label": "OpenRouter: DeepSeek R1 (best reasoning)", "model": "deepseek/deepseek-r1", "requires": ["openrouter_key"]},
    {"id": "openrouter-claude-3.5-sonnet", "provider": "openrouter", "label": "OpenRouter: Claude 3.5 Sonnet", "model": "anthropic/claude-3.5-sonnet", "requires": ["openrouter_key"]},
    {"id": "openrouter-gpt-4o", "provider": "openrouter", "label": "OpenRouter: GPT-4o", "model": "openai/gpt-4o", "requires": ["openrouter_key"]},
    {"id": "openrouter-llama-3.3-70b", "provider": "openrouter", "label": "OpenRouter: Llama 3.3 70B", "model": "meta-llama/llama-3.3-70b-versatile", "requires": ["openrouter_key"]},
    {"id": "nvidia-nemotron-30b", "provider": "nvidia", "label": "NVIDIA: Nemotron 30B Reasoning (slow, high tokens)", "model": "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning", "requires": ["nvidia_key"]},
    {"id": "nvidia-llama-3.1-8b", "provider": "nvidia", "label": "NVIDIA: Llama 3.1 8B Instruct (fastest)", "model": "meta/llama-3.1-8b-instruct", "requires": ["nvidia_key"]},
    {"id": "nvidia-llama-3.3-70b", "provider": "nvidia", "label": "NVIDIA: Llama 3.3 70B Instruct (smart)", "model": "meta/llama-3.3-70b-instruct", "requires": ["nvidia_key"]},
    {"id": "nvidia-nemotron-nano-30b", "provider": "nvidia", "label": "NVIDIA: Nemotron Nano 30B (fast, no reasoning)", "model": "nvidia/nemotron-3-nano-30b-a3b", "requires": ["nvidia_key"]},
]


def _llm_requirements_status() -> Dict[str, bool]:
    return {
        "openai_key": bool(settings.openai_api_key),
        "gemini_key": bool(settings.gemini_api_key),
        "openrouter_key": bool(settings.openrouter_api_key),
        "nvidia_key": bool(settings.nvidia_api_key),
        "groq_base_url": "groq.com" in (settings.openai_base_url or ""),
    }


@app.get("/llm/config")
async def get_llm_config():
    status = _llm_requirements_status()
    options = []
    for opt in LLM_CATALOG:
        available = all(status.get(req, False) for req in opt["requires"])
        reason = None
        if not available:
            missing = [req for req in opt["requires"] if not status.get(req, False)]
            reason = "Missing: " + ", ".join(missing)
        options.append({**opt, "available": available, "reason": reason})
    current_id = None
    for opt in LLM_CATALOG:
        if opt["provider"] == llm_engine.provider and opt["model"] == llm_engine.model:
            current_id = opt["id"]
            break
    if current_id is None:
        current_id = f"custom:{llm_engine.provider}:{llm_engine.model}"
    return {
        "current": {
            "id": current_id,
            "provider": llm_engine.provider,
            "model": llm_engine.model,
        },
        "options": options,
        "requirements": status,
    }


@app.post("/llm/config")
async def set_llm_config(payload: Dict[str, Any], request: Request):
    user = get_current_user(request)
    if not user or user.get("role") not in ("super_admin", "admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    provider = payload.get("provider")
    model = payload.get("model")
    option_id = payload.get("id")
    if option_id and option_id != "custom":
        opt = next((o for o in LLM_CATALOG if o["id"] == option_id), None)
        if not opt:
            raise HTTPException(status_code=404, detail=f"Unknown LLM option: {option_id}")
        status = _llm_requirements_status()
        if not all(status.get(req, False) for req in opt["requires"]):
            missing = [req for req in opt["requires"] if not status.get(req, False)]
            raise HTTPException(status_code=400, detail=f"Cannot select {opt['label']}: missing {', '.join(missing)}")
        provider = opt["provider"]
        model = opt["model"]
    if not provider or not model:
        raise HTTPException(status_code=400, detail="Must provide 'provider' and 'model', or a valid 'id'")
    llm_engine.set_config(provider=provider, model=model)
    return {"ok": True, "provider": llm_engine.provider, "model": llm_engine.model}


@app.post("/chat", response_model=ChatResponse)
async def chat(payload: ChatRequest, request: Request):
    user = get_current_user(request)
    user_role = user.get("role", "user") if user else payload.user_role
    if not service_manager._services:
        await service_manager.recover_from_graph()

    # Debug: log selected entities
    if payload.selected_entities:
        logger.info(f"Chat received {len(payload.selected_entities)} selected entities: {payload.selected_entities}")

    def _do_auto_train(rows, cols):
        """Run auto-train on fetched data. Returns auto_train_result dict or None."""
        if len(rows) < 10:
            return None
        try:
            from app.services.data_profiler import profile_table
            from app.services.llm_insights_engine import auto_select_algorithm
            from app.services.ml_supervised import train_model, _detect_task_type, _prepare_features, _encode_target
            import numpy as np

            profile = profile_table(rows, cols)
            target_rec = profile.get("target_recommendation")
            if not target_rec:
                return None

            target_col = target_rec["column"]

            # Pre-validate: ensure data can actually be trained on
            from app.services.response_sanitizer import EXCLUDE_COLUMNS
            feature_cols = [c for c in cols if c != target_col and c not in EXCLUDE_COLUMNS]
            X, y_raw, _ = _prepare_features(rows, feature_cols, target_col)
            if len(X) < 5:
                return None
            task_type = _detect_task_type(y_raw)
            if task_type == "classification":
                if task_type == "classification":
                    y_enc, _ = _encode_target(y_raw)
                    unique, counts = np.unique(y_enc, return_counts=True)
                    if len(unique) < 2 or min(counts) < 3:
                        return None

            algo_info = auto_select_algorithm(profile)
            algorithm = algo_info["algorithm"]

            train_result = train_model(rows, cols, target_col, algorithm)
            if "_model" in train_result:
                result = {
                    "algorithm": train_result.get("algorithm", algorithm),
                    "algorithm_key": algorithm,
                    "target_column": target_col,
                    "task_type": task_type,
                    "metrics": train_result.get("metrics", {}),
                    "sample_count": train_result.get("sample_count", len(rows)),
                    "reason": algo_info.get("reason", ""),
                }
                logger.info(f"Auto-trained {algorithm} for {target_col} ({task_type}) — metrics: {train_result.get('metrics', {})}")
                del train_result["_model"]
                return result
        except Exception as e:
            logger.warning(f"Auto-training failed: {e}")
        return None

    session_id = payload.session_id
    if not session_id:
        session_id = create_session(title=payload.query[:50] or "New Chat", user_role=user_role)
    else:
        touch_session(session_id)

    add_message(session_id, "user", payload.query)

    def _log_chat_usage(provider: str, tokens: int, latency_ms: int, intent: str = "", cached: bool = False):
        try:
            log_usage(
                provider=provider,
                tokens=tokens,
                latency_ms=latency_ms,
                session_id=session_id,
                user_query=payload.query,
                intent=intent,
                cached=cached,
                user_role=user_role,
            )
        except Exception:
            pass

    # Query cache check
    from app.services.query_enhancements import query_cache, summarize_results, recommend_charts, get_drill_down_links
    cached_result = query_cache.get(payload.query, session_id)
    if cached_result:
        cached_result["cached"] = True
        _log_chat_usage("cached", 0, 0, intent="cached", cached=True)
        return ChatResponse(**cached_result)

    # Direct prediction detection (bypass LLM for prediction queries)
    from app.services.model_store import model_store
    query_lower = payload.query.lower()
    prediction_keywords = ["predict", "what will", "forecast", "estimate", "project"]
    is_prediction = any(kw in query_lower for kw in prediction_keywords)

    if is_prediction:
        models = model_store.list_models()
        if not models:
            # No trained model exists — guide user
            _log_chat_usage("model_store", 0, 0, intent="predict")
            return ChatResponse(
                run_id=str(uuid.uuid4()),
                session_id=session_id,
                user_query=payload.query,
                user_role=user_role,
                summary=(
                    "I don't have a trained model yet for predictions. "
                    "First, query the data (e.g. 'Show me products'), "
                    "then I can train a model and make predictions. "
                    "You can also explicitly train via the ML panel."
                ),
                plan={"intent": "predict", "note": "no_model"},
                discovery=None,
                tool_calls=[],
                blocked_steps=[],
                table=None,
                primary_url=None,
                primary_service=None,
                error=None,
                memory_used=[],
                llm_provider="model_store",
                llm_latency_ms=0,
                llm_tokens=0,
            )

        # Find best matching model: prefer target column in query, then entity name match
        best_model = None
        # Priority 1: target column mentioned in query (e.g. "discontinued" → Discontinued model)
        for m in models:
            target = m.get("target_column", "").lower()
            if target and target in query_lower:
                best_model = m
                break
        # Priority 2: entity name segments match query
        if not best_model:
            for m in models:
                ek = m["entity_key"].lower()
                ek_parts = [p for p in ek.split("_") if len(p) > 2]
                if any(part in query_lower for part in ek_parts):
                    best_model = m
                    break
        # Priority 3: first model
        if not best_model:
            best_model = models[0]

        # Extract feature values from query (enhanced patterns)
        features = {}
        for feat in best_model.get("feature_columns", []):
            feat_esc = re.escape(feat)
            # Pattern 1: "UnitPrice is 88" / "UnitPrice = 88" / "UnitPrice 88"
            pattern1 = rf'{feat_esc}\s*(?:is|=|equals|[:=])\s*(\d+\.?\d*)'
            match1 = re.search(pattern1, payload.query, re.IGNORECASE)
            if match1:
                features[feat] = float(match1.group(1))
                continue
            # Pattern 2: "UnitPrice: 88" or "unitprice 88"
            pattern2 = rf'{feat_esc}\s+(\d+\.?\d*)'
            match2 = re.search(pattern2, payload.query, re.IGNORECASE)
            if match2:
                features[feat] = float(match2.group(1))
                continue
            # Pattern 3: "with UnitPrice 88" or "where UnitPrice is 88"
            pattern3 = rf'(?:with|where|and)\s+{feat_esc}\s+(?:is\s+)?(\d+\.?\d*)'
            match3 = re.search(pattern3, payload.query, re.IGNORECASE)
            if match3:
                features[feat] = float(match3.group(1))

        if not features:
            # Could not extract any features — try fallback from /ml/predict style input
            # Parse "product X with unitprice Y and UnitsInStock Z"
            all_numbers = re.findall(r'(\d+\.?\d*)', payload.query)
            numeric_feats = [f for f in best_model.get("feature_columns", [])
                            if best_model.get("task_type") == "regression" or not f.lower() in ("discontinued",)]
            for i, val in enumerate(all_numbers[:len(numeric_feats)]):
                features[numeric_feats[i]] = float(val)

        logger.info(f"Prediction: model={best_model['entity_key']}, features={features}")

        pred_result = model_store.predict(best_model["entity_key"], features)
        if pred_result:
                tool_calls = [{
                    "type": "prediction",
                    "entity_key": best_model["entity_key"],
                    "target": pred_result["target_column"],
                    "prediction": pred_result["prediction"],
                    "confidence": pred_result["confidence_info"],
                    "features": pred_result["features_used"],
                    "task_type": pred_result.get("task_type", "regression"),
                }]
                pred_val = pred_result["prediction"]
                target = pred_result["target_column"]
                # Format classification results with labels
                if pred_result.get("task_type") == "classification":
                    # Threshold at 0.5 for binary classification
                    label = "Yes" if pred_val >= 0.5 else "No"
                    confidence_pct = pred_val * 100 if pred_val >= 0.5 else (1 - pred_val) * 100
                    summary = (
                        f"**{target}** predicted as **{label}** "
                        f"(confidence: {confidence_pct:.0f}%). "
                        f"Based on features: {pred_result['features_used']}. "
                        f"*(Model: {best_model['algorithm']}, trained on {best_model['sample_count']} samples)*"
                    )
                else:
                    summary = (
                        f"Predicted **{target}** = **{pred_val:.2f}** "
                        f"based on {pred_result['features_used']}. "
                        f"{pred_result['confidence_info']}. "
                        f"*(Model: {best_model['algorithm']}, trained on {best_model['sample_count']} samples)*"
                    )
                _log_chat_usage("model_store", 0, 0, intent="predict")
                return ChatResponse(
                    run_id=str(uuid.uuid4()),
                    session_id=session_id,
                    user_query=payload.query,
                    user_role=user_role,
                    summary=summary,
                    plan={"intent": "predict", "prediction": pred_result},
                    discovery=None,
                    tool_calls=tool_calls,
                    blocked_steps=[],
                    table=None,
                    primary_url=None,
                    primary_service=None,
                    error=None,
                    memory_used=[],
                    llm_provider="model_store",
                    llm_latency_ms=0,
                    llm_tokens=0,
                )

    # Multi-entity aggregation (e.g., sales by country needs Customers+Orders+Order_Details)
    # When user mentions a service name, scope to that service only
    from app.services.multi_entity_aggregator import detect_multi_entity_query, execute_multi_entity_aggregation

    # Handle selected entities: scope query to only selected entities, auto-join at runtime
    if payload.selected_entities and len(payload.selected_entities) >= 1:
        selected = payload.selected_entities
        logger.info(f"Chat: {len(selected)} entities selected: {[e.get('entity_name') for e in selected]}")

        # Group by service
        svc_entities = {}
        for e in selected:
            sid = e.get("service_id", "")
            ename = e.get("entity_name", "")
            if sid and ename:
                svc_entities.setdefault(sid, []).append(ename)

        # If query needs data from multiple entities, auto-join at runtime
        if len(selected) >= 2:
            from app.services.entity_selector import entity_selector, classify_property

            # Build entity list with properties
            entities_for_join = []
            for sid, enames in svc_entities.items():
                svc = next((s for s in service_manager.list_services() if s["id"] == sid), None)
                if not svc:
                    continue
                for ename in enames:
                    props = svc.get("entity_properties", {}).get(ename, [])
                    prop_names = []
                    for p in props:
                        if isinstance(p, str):
                            prop_names.append(p)
                        elif isinstance(p, dict):
                            prop_names.append(p.get("name", ""))
                    entities_for_join.append({
                        "service_id": sid,
                        "entity_name": ename,
                        "properties": prop_names,
                    })

            # Auto-detect joins
            detected_joins = entity_selector.detect_joins(entities_for_join)

            # Fetch all entities in parallel
            import asyncio
            async def fetch_entity(sid, ename):
                client = service_manager._clients.get(sid)
                if not client:
                    return None
                try:
                    top = 10
                    resp = await client.query(entity_set=ename, top=top)
                    rows = client.flatten_odata_value(resp)
                    if rows:
                        cols = list(rows[0].keys())
                        logger.info(f"Chat entity join: fetched {len(rows)} rows from {ename}")
                        return {"service_id": sid, "entity_name": ename, "table": {"columns": cols, "rows": rows}}
                except Exception as ex:
                    logger.warning(f"Chat entity join: failed to fetch {ename}: {ex}")
                return None

            fetch_tasks = []
            for sid, enames in svc_entities.items():
                for ename in enames:
                    fetch_tasks.append(fetch_entity(sid, ename))

            fetch_results = await asyncio.gather(*fetch_tasks)
            all_results = [r for r in fetch_results if r is not None]

            if all_results and detected_joins:
                from app.services.cross_service_join import match_join
                # Chain joins
                sorted_joins = sorted(detected_joins, key=lambda j: -j.get("confidence", 0))
                result_table = all_results[0]["table"]
                used_right = set()
                for join_def in sorted_joins:
                    lk = join_def.get("left_key", "")
                    rk = join_def.get("right_key", "")
                    right_entity_name = join_def.get("right_entity", "")
                    if not lk or not rk:
                        continue
                    right_result = None
                    for r in all_results:
                        if r["entity_name"] == right_entity_name and r["entity_name"] not in used_right:
                            right_result = r["table"]
                            used_right.add(r["entity_name"])
                            break
                    if right_result:
                        result_table = match_join(
                            result_table, right_result,
                            left_key=lk, right_key=rk,
                            left_service=all_results[0]["service_id"],
                            right_service=re,
                        )

                # Cap rows
                rows = result_table.get("rows", [])[:100]
                columns = result_table.get("columns", [])

                # Filter useless columns
                filtered = filter_columns({"columns": columns, "rows": rows, "row_count": len(rows)})
                columns, rows = filtered["columns"], filtered["rows"]

                if rows:
                    entity_names = [e.get("entity_name") for e in selected]
                    summary = f"Joined {len(entity_names)} entities ({', '.join(entity_names)}): {len(rows)} rows, {len(columns)} columns"
                    tool_calls_me = [{"type": "entity_join", "entities": entity_names, "joins": len(detected_joins), "row_count": len(rows)}]
                    all_col_labels = {}
                    for ent in selected:
                        all_col_labels.update(_build_column_labels(ent.get("service_id", ""), ent.get("entity_name", ""), columns))
                    add_message(session_id, "assistant", summary, plan=None, result={"table": {"columns": columns, "rows": rows, "row_count": len(rows), "column_labels": all_col_labels or None}, "tool_calls": tool_calls_me})
                    _log_chat_usage("entity_join", 0, 0, intent="entity_join")
                    atr = _do_auto_train(rows, columns)
                    return ChatResponse(
                        run_id=str(uuid.uuid4()),
                        session_id=session_id,
                        user_query=payload.query,
                        user_role=user_role,
                        summary=summary,
                        plan={"intent": "entity_join", "entities": entity_names},
                        discovery=None,
                        tool_calls=tool_calls_me,
                        blocked_steps=[],
                        table=TableData(columns=columns, rows=rows, row_count=len(rows), column_labels=all_col_labels or None),
                        primary_url=None,
                        primary_service=selected[0].get("service_id", ""),
                        error=None,
                        memory_used=[],
                        llm_provider="entity_join",
                        llm_latency_ms=0,
                        llm_tokens=0,
                        auto_train_result=atr,
                    )
                else:
                    # Join returned 0 rows — show individual entity results instead of falling through
                    entity_names = [e.get("entity_name") for e in selected]
                    combined_rows = []
                    combined_cols = []
                    for r in all_results:
                        t = r["table"]
                        if t.get("rows"):
                            if not combined_cols:
                                combined_cols = t["columns"]
                            combined_rows.extend(t["rows"][:5])
                    if combined_rows:
                        # Filter useless columns
                        filtered = filter_columns({"columns": combined_cols, "rows": combined_rows, "row_count": len(combined_rows)})
                        combined_cols, combined_rows = filtered["columns"], filtered["rows"]
                        summary = f"No matching rows found between {', '.join(entity_names)} — showing individual entity data ({len(combined_rows)} rows)"
                        tool_calls_me = [{"type": "entity_select", "entities": entity_names, "row_count": len(combined_rows)}]
                        all_col_labels = {}
                        for ent in selected:
                            all_col_labels.update(_build_column_labels(ent.get("service_id", ""), ent.get("entity_name", ""), combined_cols))
                        add_message(session_id, "assistant", summary, plan=None, result={"table": {"columns": combined_cols, "rows": combined_rows, "row_count": len(combined_rows), "column_labels": all_col_labels or None}, "tool_calls": tool_calls_me})
                        _log_chat_usage("entity_select", 0, 0, intent="entity_select")
                        return ChatResponse(
                            run_id=str(uuid.uuid4()),
                            session_id=session_id,
                            user_query=payload.query,
                            user_role=user_role,
                            summary=summary,
                            plan={"intent": "entity_select", "entities": entity_names},
                            discovery=None,
                            tool_calls=tool_calls_me,
                            blocked_steps=[],
                            table=TableData(columns=combined_cols, rows=combined_rows, row_count=len(combined_rows), column_labels=all_col_labels or None),
                            primary_url=None,
                            primary_service=selected[0].get("service_id", ""),
                            error=None,
                            memory_used=[],
                            llm_provider="entity_select",
                            llm_latency_ms=0,
                            llm_tokens=0,
                        )

            # No joins detected or no results — still return individual entity data
            elif all_results:
                entity_names = [e.get("entity_name") for e in selected]
                combined_rows = []
                combined_cols = []
                for r in all_results:
                    t = r["table"]
                    if t.get("rows"):
                        if not combined_cols:
                            combined_cols = t["columns"]
                        combined_rows.extend(t["rows"][:5])
                if combined_rows:
                    # Filter useless columns
                    filtered = filter_columns({"columns": combined_cols, "rows": combined_rows, "row_count": len(combined_rows)})
                    combined_cols, combined_rows = filtered["columns"], filtered["rows"]
                    summary = f"Selected {len(entity_names)} entities: {', '.join(entity_names)} — {len(combined_rows)} rows total"
                    tool_calls_me = [{"type": "entity_select", "entities": entity_names, "row_count": len(combined_rows)}]
                    all_col_labels = {}
                    for ent in selected:
                        all_col_labels.update(_build_column_labels(ent.get("service_id", ""), ent.get("entity_name", ""), combined_cols))
                    add_message(session_id, "assistant", summary, plan=None, result={"table": {"columns": combined_cols, "rows": combined_rows, "row_count": len(combined_rows), "column_labels": all_col_labels or None}, "tool_calls": tool_calls_me})
                    _log_chat_usage("entity_select", 0, 0, intent="entity_select")
                    return ChatResponse(
                        run_id=str(uuid.uuid4()),
                        session_id=session_id,
                        user_query=payload.query,
                        user_role=user_role,
                        summary=summary,
                        plan={"intent": "entity_select", "entities": entity_names},
                        discovery=None,
                        tool_calls=tool_calls_me,
                        blocked_steps=[],
                        table=TableData(columns=combined_cols, rows=combined_rows, row_count=len(combined_rows), column_labels=all_col_labels or None),
                        primary_url=None,
                        primary_service=selected[0].get("service_id", ""),
                        error=None,
                        memory_used=[],
                        llm_provider="entity_select",
                        llm_latency_ms=0,
                        llm_tokens=0,
                    )

        # Single entity selected: query only that entity
        elif len(selected) == 1:
            sid = selected[0].get("service_id", "")
            ename = selected[0].get("entity_name", "")
            client = service_manager._clients.get(sid)
            if client:
                try:
                    top = 10
                    resp = await client.query(entity_set=ename, top=top)
                    rows = client.flatten_odata_value(resp)
                    if rows:
                        cols = list(rows[0].keys())
                        # Filter useless columns
                        filtered = filter_columns({"columns": cols, "rows": rows, "row_count": len(rows)})
                        cols, rows = filtered["columns"], filtered["rows"]
                        summary = f"Showing {len(rows)} rows from {ename}"
                        _log_chat_usage("entity_select", 0, 0, intent="entity_select")
                        atr = _do_auto_train(rows, cols)
                        tool_calls_me = [{"type": "entity_select", "service_id": sid, "entity": ename, "row_count": len(rows)}]
                        col_labels = _build_column_labels(sid, ename, cols)
                        add_message(session_id, "assistant", summary, plan=None, result={"table": {"columns": cols, "rows": rows, "row_count": len(rows), "column_labels": col_labels or None}, "tool_calls": tool_calls_me})
                        return ChatResponse(
                            run_id=str(uuid.uuid4()),
                            session_id=session_id,
                            user_query=payload.query,
                            user_role=user_role,
                            summary=summary,
                            plan={"intent": "entity_select", "entity": ename},
                            discovery=None,
                            tool_calls=tool_calls_me,
                            blocked_steps=[],
                            table=TableData(columns=cols, rows=rows, row_count=len(rows), column_labels=col_labels or None),
                            primary_url=None,
                            primary_service=sid,
                            error=None,
                            memory_used=[],
                            llm_provider="entity_select",
                            llm_latency_ms=0,
                            llm_tokens=0,
                            auto_train_result=atr,
                        )
                except Exception as ex:
                    logger.warning(f"Chat entity select: failed to fetch {ename}: {ex}")

    services_list = service_manager.list_services()
    q_lower = payload.query.lower()
    q_compact = re.sub(r"[^a-z0-9]", "", q_lower)
    exact_entity_requested = any(
        (
            es.lower() in q_lower
            or es.lower().replace("_", " ") in q_lower
            or re.sub(r"[^a-z0-9]", "", es.lower()) in q_compact
        )
        for svc in services_list
        for es in svc.get("entity_sets", [])
    )
    aggregate_keywords = (" by ", " per ", "count", "total", "sum", "average", "avg", "join", "combine", "compare")
    should_try_multi_entity = (not exact_entity_requested) and any(k in f" {q_lower} " for k in aggregate_keywords)
    explicit_service = None
    for svc in services_list:
        if svc["id"].lower() in q_lower or svc["name"].lower() in q_lower:
            explicit_service = svc["id"]
            break
    # Always try multi-entity aggregation — scope to explicit service if mentioned
    services_to_check = [s for s in services_list if should_try_multi_entity and (not explicit_service or s["id"] == explicit_service)]
    for svc in services_to_check:
        svc_id = svc["id"]
        client = service_manager.get_client(svc_id)
        if not client:
            continue
        entity_cols = {}
        for es in svc.get("entity_sets", []):
            es_lower = es.lower()
            if any(vp in es_lower for vp in ("summary", "by_", "for_", "list_of", "extended", "subtotal", "quarterly", "annual")):
                continue
            try:
                raw = await asyncio.wait_for(client.query(entity_set=es, top=1), timeout=3.0)
                flat = client.flatten_odata_value(raw)
                if flat:
                    entity_cols[es] = [c for c in flat[0].keys() if not c.startswith("@odata")]
            except Exception:
                pass
        if not entity_cols:
            continue
        me_info = detect_multi_entity_query(payload.query, svc_id, entity_cols)
        if me_info:
            client = service_manager.get_client(svc_id)
            if client:
                me_result = await execute_multi_entity_aggregation(
                    payload.query, svc_id, client, me_info,
                )
                if me_result:
                    tool_calls_me = [{"type": "multi_entity", "service_id": svc_id, "chain": [s["entity"] for s in me_info["chain"]], "row_count": me_result["row_count"]}]
                    add_message(session_id, "assistant", me_result.get("summary", ""), plan=None, result={"table": me_result, "tool_calls": tool_calls_me})
                    me_chart_recs = []
                    try:
                        me_chart_recs = recommend_charts(me_result.get("rows", []), me_result.get("columns", []), payload.query)
                    except Exception:
                        pass
                    _log_chat_usage("multi_entity", 0, 0, intent="aggregate")
                    return ChatResponse(
                        run_id=str(uuid.uuid4()),
                        session_id=session_id,
                        user_query=payload.query,
                        user_role=user_role,
                        summary=me_result.get("summary", "Multi-entity aggregation complete"),
                        plan={"intent": "aggregate", "summary": me_result.get("summary", "")},
                        discovery=None,
                        tool_calls=tool_calls_me,
                        blocked_steps=[],
                        table=TableData(**me_result) if me_result else None,
                        primary_url=None,
                        primary_service=svc["id"],
                        error=None,
                        memory_used=[],
                        llm_provider="computed",
                        llm_latency_ms=0,
                        llm_tokens=0,
                        chart_recommendations=me_chart_recs,
                    )

    candidates = llm_engine.find_entity_candidates(services_list, payload.query, limit=5)
    if not exact_entity_requested and len(candidates) >= 2:
        top_score = candidates[0].get("score", 0)
        close_candidates = [
            c for c in candidates
            if top_score and c.get("score", 0) >= top_score * 0.7
        ][:3]
        if len(close_candidates) >= 2:
            candidate_lines = [
                f"- {c.get('entity_set', '')} ({c.get('service_name') or c.get('service_id', 'service')})"
                for c in close_candidates
            ]
            summary = (
                "I found multiple possible entities for your request. Choose the one that matches what you mean."
                "\n\n" + "\n".join(candidate_lines)
            )
            clarification = {
                "type": "entity_choice",
                "query": payload.query,
                "candidates": close_candidates,
            }
            tool_calls_clarify = [{"type": "entity_clarification", "candidate_count": len(close_candidates), "candidates": close_candidates}]
            add_message(
                session_id,
                "assistant",
                summary,
                plan={"intent": "clarify", "summary": summary},
                result={"tool_calls": tool_calls_clarify, "clarification": clarification},
            )
            return ChatResponse(
                run_id=str(uuid.uuid4()),
                session_id=session_id,
                user_query=payload.query,
                user_role=user_role,
                summary=summary,
                plan={"intent": "clarify", "summary": summary},
                discovery=None,
                tool_calls=tool_calls_clarify,
                blocked_steps=[],
                table=None,
                primary_url=None,
                primary_service=None,
                error=None,
                memory_used=[],
                llm_provider="entity-candidates",
                llm_latency_ms=0,
                llm_tokens=0,
                clarification=clarification,
            )

    orchestrator_query = payload.query
    if not exact_entity_requested and candidates:
        top = candidates[0]
        second_score = candidates[1].get("score", 0) if len(candidates) > 1 else 0
        if top.get("score", 0) >= 1.2 and (not second_score or top.get("score", 0) >= second_score * 1.6):
            orchestrator_query = f"show {top['entity_set']}"

    result = await orchestrator.run(
        user_query=orchestrator_query,
        session_id=session_id,
        user_role=user_role,
    )

    add_message(
        session_id,
        "assistant",
        result.get("summary", ""),
        plan=result.get("plan"),
        result={"table": result.get("table"), "tool_calls": result.get("tool_calls")},
    )
    add_run(
        session_id=session_id,
        message_id=None,
        user_query=payload.query,
        plan=result.get("plan"),
        tool_calls=result.get("tool_calls"),
        response={"summary": result.get("summary"), "table": result.get("table")},
    )

    plan_obj = None
    if result.get("plan"):
        from app.agents.orchestrator import _normalize_plan
        for _ in range(3):
            try:
                plan_obj = Plan(**result["plan"])
                break
            except Exception as e:
                logger.warning(f"Plan validation failed (attempt), re-normalizing: {e}")
                result["plan"] = _normalize_plan(result["plan"])
        if plan_obj is None:
            logger.error("Plan validation failed repeatedly, dropping plan")
            result["plan"] = None
    table_obj = None
    if result.get("table"):
        try:
            table_obj = TableData(**result["table"])
        except Exception as e:
            logger.warning(f"Table validation failed: {e}")
            table_obj = None

    # Post-fetch aggregation for queries like "Count customers per country"
    from app.services.aggregator import detect_aggregation, aggregate
    agg_info = detect_aggregation(payload.query)
    if agg_info and result.get("table") and result["table"].get("rows"):
        try:
            t = result["table"]
            agg_result = aggregate(t["rows"], t["columns"], agg_info)
            result["table"] = agg_result
            table_obj = TableData(**agg_result)
            func_label = agg_info["func"].upper()
            group_label = agg_info.get("group_by") or agg_info.get("agg_col") or ""
            result["summary"] = f"Aggregated result: {func_label} by {group_label} ({agg_result['row_count']} groups from {t.get('row_count', '?')} rows)"
        except Exception as e:
            logger.warning(f"Aggregation failed: {e}")

    # Post-aggregation computation (percentage, comparison, ratio)
    from app.services.post_processor import detect_post_processing, post_process
    pp_info = detect_post_processing(payload.query)
    if pp_info and result.get("table") and result["table"].get("rows"):
        try:
            t = result["table"]
            pp_result = post_process(t["rows"], t["columns"], pp_info, payload.query)
            result["table"] = pp_result
            table_obj = TableData(**pp_result)
            pp_type = pp_info.get("type", "")
            if pp_type == "percentage":
                min_pct = pp_info.get("min_percentage")
                if min_pct is not None:
                    result["summary"] = f"Percentage breakdown ({pp_result['row_count']} groups with > {min_pct}% contribution)"
                else:
                    result["summary"] = f"Percentage breakdown ({pp_result['row_count']} groups)"
            elif pp_type == "comparison":
                result["summary"] = f"Comparison result ({pp_result['row_count']} entries)"
            elif pp_type in ("which_extremum", "extremum"):
                extremum = pp_info.get("extremum", "min")
                result_row = next((r for r in pp_result.get("rows", []) if "result" in r), None)
                if result_row:
                    result["summary"] = result_row["result"]
                else:
                    result["summary"] = f"Found the {'least' if extremum == 'min' else 'most'} ({pp_result['row_count']} entries)"
            elif pp_type == "ratio":
                result["summary"] = f"Ratio calculation ({pp_result['row_count']} entries)"
        except Exception as e:
            logger.warning(f"Post-processing failed: {e}")

    # Auto-train model on query results using data profiler + auto algorithm selection
    auto_train_result = None
    if result.get("table") and result["table"].get("rows") and len(result["table"]["rows"]) >= 5:
        auto_train_result = _do_auto_train(result["table"]["rows"], result["table"]["columns"])

    # Generate chart recommendations and drill-down links
    chart_recs = []
    drill_links = []
    if result.get("table") and result["table"].get("rows"):
        try:
            t = result["table"]
            chart_recs = recommend_charts(t["rows"], t["columns"], payload.query)
        except Exception as e:
            logger.warning(f"Chart recommendation failed: {e}")
        try:
            if t["rows"]:
                # Extract entity_set from plan (Pydantic or dict)
                entity_set_name = ""
                plan_data = plan_obj if plan_obj else result.get("plan")
                if plan_data:
                    steps = getattr(plan_data, "steps", None) or (plan_data.get("steps") if isinstance(plan_data, dict) else None)
                    if steps and len(steps) > 0:
                        step = steps[0]
                        entity_set_name = getattr(step, "entity_set", "") or (step.get("entity_set") if isinstance(step, dict) else "")
                drill_links = get_drill_down_links(
                    entity_set_name,
                    t["rows"][0],
                    service_manager.list_services(),
                )
        except Exception as e:
            logger.warning(f"Drill-down link generation failed: {e}")

    # Cache the result (only if it has meaningful table data)
    try:
        table_data = result.get("table")
        has_table = table_data and table_data.get("rows") and len(table_data.get("rows", [])) > 0
        response_data = {
            "run_id": result["run_id"],
            "session_id": session_id,
            "user_query": result["user_query"],
            "user_role": result["user_role"],
            "summary": result["summary"],
            "plan": plan_obj.model_dump() if plan_obj else None,
            "discovery": result.get("discovery"),
            "tool_calls": result.get("tool_calls", []),
            "blocked_steps": result.get("blocked_steps", []),
            "table": table_data if has_table else None,
            "primary_url": result.get("primary_url"),
            "primary_service": result.get("primary_service"),
            "error": result.get("error"),
            "memory_used": result.get("memory_used", []),
            "llm_provider": result.get("llm_provider", "unknown"),
            "llm_latency_ms": result.get("llm_latency_ms", 0),
            "llm_tokens": result.get("llm_tokens", 0),
            "chart_recommendations": chart_recs,
            "drill_down_links": drill_links,
        }
        if has_table and not result.get("error"):
            query_cache.set(payload.query, response_data, session_id)
    except Exception:
        pass

    _log_chat_usage(
        provider=result.get("llm_provider", "unknown"),
        tokens=result.get("llm_tokens", 0),
        latency_ms=result.get("llm_latency_ms", 0),
        intent=result.get("plan", {}).get("intent", "") if isinstance(result.get("plan"), dict) else "",
    )
    return ChatResponse(
        run_id=result["run_id"],
        session_id=session_id,
        user_query=result["user_query"],
        user_role=result["user_role"],
        summary=result["summary"],
        plan=plan_obj,
        discovery=result.get("discovery"),
        tool_calls=result.get("tool_calls", []),
        blocked_steps=result.get("blocked_steps", []),
        table=table_obj,
        primary_url=result.get("primary_url"),
        primary_service=result.get("primary_service"),
        error=result.get("error"),
        memory_used=result.get("memory_used", []),
        llm_provider=result.get("llm_provider", "unknown"),
        llm_latency_ms=result.get("llm_latency_ms", 0),
        llm_tokens=result.get("llm_tokens", 0),
        chart_recommendations=chart_recs,
        drill_down_links=drill_links,
        intent=result.get("intent"),
        auto_train_result=auto_train_result,
        write_preview=result.get("write_preview"),
    )


@app.post("/chat/analyze")
async def chat_analyze(payload: ChatRequest, request: Request):
    """On-demand insights: analyze the data from a previous query using LLM."""
    from app.services.data_profiler import profile_table
    from app.services.llm_insights_engine import generate_insights

    user = get_current_user(request)
    user_role = user.get("role", "user") if user else payload.user_role

    # Get the last table data from the session
    session_id = payload.session_id
    if not session_id:
        return {"error": "session_id required for analysis"}

    messages = get_messages(session_id, limit=5)
    # Find the last assistant message with table data
    table_data = None
    for msg in reversed(messages):
        if msg.get("role") == "assistant":
            result = msg.get("result")
            if result and result.get("table"):
                table_data = result["table"]
                break

    if not table_data or not table_data.get("rows"):
        return {"error": "No data found in session to analyze", "insights": None}

    # Profile the data
    profile = profile_table(table_data["rows"], table_data["columns"])

    # Generate LLM insights
    provider = payload.llm_provider if hasattr(payload, "llm_provider") else None
    insights = await generate_insights(profile, payload.query, table_data, provider=provider or "auto")

    # Log usage
    try:
        log_usage(
            provider="llm_insights",
            tokens=0,
            latency_ms=0,
            session_id=session_id,
            user_query=payload.query,
            intent="analyze",
        )
    except Exception:
        pass

    return {
        "profile": {
            "row_count": profile.get("row_count", 0),
            "column_count": profile.get("column_count", 0),
            "numeric_columns": profile.get("numeric_columns", []),
            "categorical_columns": profile.get("categorical_columns", []),
            "quality_score": profile.get("quality_score", 0),
            "correlations": profile.get("correlations", []),
            "outlier_summary": profile.get("outlier_summary", {}),
        },
        "insights": insights.get("insights", []),
        "suggestions": insights.get("suggestions", []),
        "ml_recommendation": insights.get("ml_recommendation", {}),
        "chart_insights": insights.get("chart_insights", []),
        "summary": insights.get("summary", ""),
    }


@app.get("/suggestions")
async def get_suggestions():
    from app.services.query_enhancements import generate_suggestions
    return {"suggestions": generate_suggestions(service_manager.list_services())}


@app.get("/cache/stats")
async def get_cache_stats():
    from app.services.query_enhancements import query_cache
    from app.services.query_optimizer import query_optimizer
    query_stats = query_cache.stats()
    query_stats["optimizer"] = query_optimizer.stats
    return query_stats


@app.post("/cache/clear")
async def clear_cache():
    from app.services.query_enhancements import query_cache
    from app.services.query_optimizer import query_optimizer
    query_cache.clear()
    query_optimizer.clear_cache()
    return {"ok": True}


@app.get("/entities/{service_id}/{entity_set}/fields")
async def get_entity_fields(service_id: str, entity_set: str):
    """Get field requirements for an entity (required, optional, auto-generated)."""
    from app.services.guardrails import get_entity_field_requirements
    return get_entity_field_requirements(service_id, entity_set)


@app.get("/write/history")
async def get_write_history_endpoint(limit: int = 50, operation: str = "", entity_set: str = ""):
    """Get write operation history for audit trail."""
    from app.services.guardrails import get_write_history, get_write_history_stats
    return {
        "stats": get_write_history_stats(),
        "history": get_write_history(limit=limit, operation=operation, entity_set=entity_set),
    }


@app.post("/chat/write/preview")
async def write_preview(payload: ChatRequest, request: Request):
    """Preview a write operation and return confirmation summary before executing."""
    from app.services.guardrails import build_write_summary, run_input_guards, get_entity_field_requirements
    from app.agents.reasoning_engine import llm_engine

    user = get_current_user(request)
    user_role = user.get("role", "user") if user else payload.user_role

    # Use LLM to extract write operation details from natural language
    services = service_manager.list_services()
    plan, llm_meta = await llm_engine.plan(payload.query, services, memory_context=[])

    write_intents = {"create", "update", "delete"}
    if plan.get("intent") not in write_intents or not plan.get("write_operation"):
        return {"error": "Could not detect a write operation in your query. Try phrases like 'create a new order' or 'update customer X'."}

    write_op = plan["write_operation"]
    entity_set = write_op.get("entity_set", "")
    operation = write_op.get("operation", plan.get("intent"))
    fields = write_op.get("fields", {})
    entity_id = write_op.get("entity_id")
    service_id = write_op.get("service_id") or (plan.get("target_services", [None])[0] if plan.get("target_services") else None)
    required_fields = write_op.get("required_fields", [])

    # Get entity-specific field requirements from metadata
    field_reqs = get_entity_field_requirements(service_id, entity_set)
    entity_required = field_reqs.get("required_fields", [])
    entity_optional = field_reqs.get("optional_fields", [])
    auto_generated = field_reqs.get("auto_generated_fields", [])

    # Merge: entity metadata required fields + LLM-detected required fields
    all_required = list(set(entity_required + required_fields))

    # Run input guards
    guard_result = run_input_guards(
        user_role=user_role,
        user_id=payload.session_id or "anonymous",
        entity_set=entity_set,
        operation=operation,
        fields=fields,
        required_fields=all_required,
        confirmed=False,
    )

    # Check for missing required fields
    missing = [f for f in all_required if f not in fields or not fields.get(f)]

    # If blocked due to missing fields, return write_preview so frontend shows the modal
    if not guard_result.allow and missing:
        summary = build_write_summary(
            operation=operation,
            entity_set=entity_set,
            fields=fields,
            service_id=service_id,
            missing_fields=missing,
        )
        return {
            "preview": True,
            "operation": operation,
            "entity_set": entity_set,
            "service_id": service_id,
            "fields": fields,
            "entity_id": entity_id,
            "required_fields": all_required,
            "optional_fields": entity_optional[:10],
            "auto_generated_fields": auto_generated,
            "missing_fields": missing,
            "confirmation_summary": summary,
            "needs_user_input": True,
        }

    if not guard_result.allow:
        return {"error": f"Blocked: {guard_result.reason}", "blocked": True}

    # Build confirmation summary
    summary = build_write_summary(
        operation=operation,
        entity_set=entity_set,
        fields=fields,
        service_id=service_id,
        missing_fields=missing,
    )

    return {
        "preview": True,
        "operation": operation,
        "entity_set": entity_set,
        "service_id": service_id,
        "fields": fields,
        "entity_id": entity_id,
        "required_fields": all_required,
        "optional_fields": entity_optional[:10],
        "auto_generated_fields": auto_generated,
        "missing_fields": missing,
        "confirmation_summary": summary,
        "needs_user_input": len(missing) > 0,
    }


@app.post("/chat/write/execute")
async def write_execute(payload: ChatRequest, request: Request):
    """Execute a confirmed write operation."""
    from app.services.guardrails import run_input_guards, run_output_guards
    from app.services.odata_client import ODataClient

    user = get_current_user(request)
    user_role = user.get("role", "user") if user else payload.user_role

    # Parse write operation from query context (expects JSON in query field)
    import json
    try:
        write_op = json.loads(payload.query) if payload.query.startswith("{") else {}
    except json.JSONDecodeError:
        return {"error": "Invalid write operation format"}

    if not write_op:
        return {"error": "No write operation provided"}

    operation = write_op.get("operation", "")
    entity_set = write_op.get("entity_set", "")
    fields = write_op.get("fields", {})
    entity_id = write_op.get("entity_id")
    service_id = write_op.get("service_id", "")

    if not service_id or not entity_set:
        return {"error": "service_id and entity_set required"}

    # Run input guards with confirmed=True
    guard_result = run_input_guards(
        user_role=user_role,
        user_id=payload.session_id or "anonymous",
        entity_set=entity_set,
        operation=operation,
        fields=fields,
        required_fields=[],
        confirmed=True,
    )

    if not guard_result.allow:
        from app.services.guardrails import log_write_operation
        log_write_operation(
            operation=operation, entity_set=entity_set, service_id=service_id,
            user_role=user_role, user_id=payload.session_id or "anonymous",
            fields=fields, success=False, error=guard_result.reason,
        )
        return {"error": f"Blocked: {guard_result.reason}"}

    # Execute write
    try:
        svc = service_manager._services.get(service_id, {})
        if not svc:
            return {"error": f"Service '{service_id}' not found"}

        client = ODataClient(svc.get("base_url", ""), auth_type=svc.get("auth_type"), auth_config=svc.get("auth_config"))

        if operation == "create":
            result = await client.create(entity_set, fields)
        elif operation == "update":
            if not entity_id:
                return {"error": "entity_id required for update"}
            result = await client.update(entity_set, entity_id, fields)
        elif operation == "delete":
            if not entity_id:
                return {"error": "entity_id required for delete"}
            result = await client.delete(entity_set, entity_id)
        else:
            return {"error": f"Unknown operation: {operation}"}

        # Run output guards
        result = run_output_guards(result, operation)

        # Log successful write
        from app.services.guardrails import log_write_operation
        log_write_operation(
            operation=operation, entity_set=entity_set, service_id=service_id,
            user_role=user_role, user_id=payload.session_id or "anonymous",
            fields=fields, success=True, entity_id=entity_id or "",
        )

        return {
            "success": True,
            "operation": operation,
            "entity_set": entity_set,
            "result": result,
            "summary": f"Successfully {operation}d entity in {entity_set}",
        }
    except Exception as e:
        logger.exception(f"Write execution failed: {e}")
        from app.services.guardrails import log_write_operation
        log_write_operation(
            operation=operation, entity_set=entity_set, service_id=service_id,
            user_role=user_role, user_id=payload.session_id or "anonymous",
            fields=fields, success=False, error=str(e),
        )
        return {"error": f"Write failed: {str(e)}"}


@app.get("/sessions", response_model=List[SessionInfo])
async def get_sessions():
    return [SessionInfo(**s) for s in list_sessions()]


@app.post("/sessions", response_model=SessionInfo)
async def create_session_endpoint(payload: SessionCreate):
    sid = create_session(title=payload.title, user_role=payload.user_role)
    sessions = list_sessions()
    for s in sessions:
        if s["id"] == sid:
            return SessionInfo(**s)
    raise HTTPException(status_code=500, detail="Failed to create session")


@app.patch("/sessions/{session_id}")
async def patch_session(session_id: str, payload: Dict[str, str]):
    if "title" in payload:
        rename_session(session_id, payload["title"])
    return {"ok": True}


@app.delete("/sessions/{session_id}")
async def delete_session_endpoint(session_id: str):
    delete_session(session_id)
    return {"deleted": session_id}


@app.get("/sessions/{session_id}/messages", response_model=List[MessageInfo])
async def get_session_messages(session_id: str):
    return [MessageInfo(**m) for m in get_messages(session_id)]


@app.post("/analyze")
async def analyze_table(payload: Dict[str, Any], request: Request):
    user = get_current_user(request)
    table = payload.get("table")
    if not table or not table.get("rows"):
        raise HTTPException(status_code=400, detail="No table data to analyze")
    from app.services.ml_engine import analyze_table as ml_analyze
    try:
        result = ml_analyze(table)
        return result
    except Exception as e:
        logger.error(f"ML analysis failed: {e}")
        raise HTTPException(status_code=500, detail=f"Analysis failed: {str(e)}")


@app.post("/ml/clean")
async def ml_clean(payload: Dict[str, Any], request: Request):
    user = get_current_user(request)
    table = payload.get("table")
    options = payload.get("options", {})
    if not table or not table.get("rows"):
        raise HTTPException(status_code=400, detail="No table data to clean")
    from app.services.data_cleaner import clean_data
    try:
        result = clean_data(table["rows"], table["columns"], options)
        return result
    except Exception as e:
        logger.error(f"Data cleaning failed: {e}")
        raise HTTPException(status_code=500, detail=f"Cleaning failed: {str(e)}")


@app.post("/ml/train")
async def ml_train(payload: Dict[str, Any], request: Request):
    user = get_current_user(request)
    table = payload.get("table")
    target_col = payload.get("target_column")
    algorithm = payload.get("algorithm", "random_forest")
    options = payload.get("options", {})
    compare = payload.get("compare", False)
    if not table or not table.get("rows"):
        raise HTTPException(status_code=400, detail="No table data to train on")
    if not target_col:
        raise HTTPException(status_code=400, detail="target_column is required")

    from app.services.ml_supervised import train_model, train_and_compare
    try:
        if compare:
            algorithms = payload.get("algorithms", ["decision_tree", "random_forest", "linear_regression", "logistic_regression", "xgboost", "gradient_boosting"])
            result = train_and_compare(table["rows"], table["columns"], target_col, algorithms)
        else:
            result = train_model(table["rows"], table["columns"], target_col, algorithm, options)
        # Remove non-serializable model object before returning
        model_obj = result.pop("_model", None)
        # Store model for prediction if single algorithm
        if model_obj and not compare:
            from app.services.model_store import model_store
            entity_key = f"manual_{target_col}"
            model_store.store(
                entity_key=entity_key,
                model_obj=model_obj,
                feature_columns=result.get("feature_columns", []),
                target_column=target_col,
                task_type=result.get("task_type", "regression"),
                metrics=result.get("metrics", {}),
                feature_importance=result.get("feature_importance", []),
                algorithm=algorithm,
                sample_count=result.get("sample_count", 0),
            )
        return result
    except Exception as e:
        logger.error(f"ML training failed: {e}")
        raise HTTPException(status_code=500, detail=f"Training failed: {str(e)}")


@app.get("/ml/algorithms")
async def ml_algorithms():
    from app.services.ml_supervised import ALGORITHMS
    return {"algorithms": ALGORITHMS}


@app.get("/ml/models")
async def ml_models():
    from app.services.model_store import model_store
    return {"models": model_store.list_models()}


@app.post("/ml/predict")
async def ml_predict(payload: Dict[str, Any], request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    from app.services.model_store import model_store
    entity_key = payload.get("entity_key")
    features = payload.get("features", {})
    if not entity_key:
        raise HTTPException(status_code=400, detail="entity_key required")
    result = model_store.predict(entity_key, features)
    if result is None:
        raise HTTPException(status_code=404, detail=f"No trained model for '{entity_key}'. Query the data first to train a model.")
    return result


@app.post("/odata/paginate")
async def odata_paginate(payload: Dict[str, Any]):
    """Initialize pagination for a large dataset query."""
    from app.services.pagination import pagination_manager
    import httpx
    
    url = payload.get("url")
    session_id = payload.get("session_id")
    page_size = payload.get("page_size", 50)
    
    if not url or not session_id:
        raise HTTPException(status_code=400, detail="url and session_id required")
    
    try:
        # Fetch with $count to get total
        count_url = url + ("&" if "?" in url else "?") + "$count=true&$top=0"
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            resp = await client.get(count_url)
            resp.raise_for_status()
            data = resp.json()
            total_count = data.get("@odata.count", 0)
        
        # Create pagination session
        pagination_info = pagination_manager.create_session(
            session_id=session_id,
            base_url=url,
            total_count=total_count,
            page_size=page_size
        )
        
        # Fetch first page
        skip, top = pagination_manager.get_skip_top(session_id)
        page_url = url + ("&" if "?" in url else "?") + f"$skip={skip}&$top={top}"
        
        from app.services.response_sanitizer import sanitize
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            resp = await client.get(page_url)
            resp.raise_for_status()
            raw = resp.json()
        
        sanitized = sanitize(raw, max_rows=top)
        
        return {
            "pagination": pagination_info,
            "table": sanitized
        }
    except Exception as e:
        logger.error(f"Pagination init failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/odata/page")
async def odata_page(payload: Dict[str, Any]):
    """Get next/previous page of paginated data."""
    from app.services.pagination import pagination_manager
    import httpx
    
    session_id = payload.get("session_id")
    action = payload.get("action", "next")  # next, prev, goto
    page = payload.get("page", 1)
    
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id required")
    
    state = pagination_manager.get_session(session_id)
    if not state:
        raise HTTPException(status_code=404, detail="Pagination session not found. Query again to start pagination.")
    
    # Update pagination state
    if action == "next":
        pagination_info = pagination_manager.next_page(session_id)
    elif action == "prev":
        pagination_info = pagination_manager.prev_page(session_id)
    elif action == "goto":
        pagination_info = pagination_manager.goto_page(session_id, page)
    else:
        raise HTTPException(status_code=400, detail="action must be next, prev, or goto")
    
    if not pagination_info:
        raise HTTPException(status_code=400, detail="No more pages available")
    
    try:
        # Fetch the page data
        skip, top = pagination_manager.get_skip_top(session_id)
        page_url = state.base_url + ("&" if "?" in state.base_url else "?") + f"$skip={skip}&$top={top}"
        
        from app.services.response_sanitizer import sanitize
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            resp = await client.get(page_url)
            resp.raise_for_status()
            raw = resp.json()
        
        sanitized = sanitize(raw, max_rows=top)
        
        return {
            "pagination": pagination_info,
            "table": sanitized
        }
    except Exception as e:
        logger.error(f"Pagination page failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/mcp/tools")
async def mcp_tools():
    return {"tools": mcp_server.tools}


@app.post("/mcp/call", response_model=MCPCallResponse)
async def mcp_call(payload: MCPCallRequest, request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    result = await mcp_server.call_tool(payload.name, payload.arguments)
    return MCPCallResponse(result=result)


@app.post("/share")
async def share_chat(request: Request):
    user = get_current_user(request)

    body = await request.json()
    channel = body.get("channel", "clipboard")
    query = body.get("query", "")
    summary = body.get("summary", "")
    table = body.get("table")
    session_id = body.get("session_id", "")

    if not query and not summary:
        raise HTTPException(status_code=400, detail="No content to share")

    share_text = f"Chat Query: {query}\n\nResult: {summary}"
    if table and table.get("rows"):
        cols = table.get("columns", [])
        rows = table.get("rows", [])[:20]
        share_text += "\n\nData:\n" + " | ".join(cols) + "\n"
        share_text += "\n".join(
            " | ".join(str(r.get(c, "")) for c in cols) for r in rows
        )
        if len(table.get("rows", [])) > 20:
            share_text += f"\n... and {len(table['rows']) - 20} more rows"

    user_info = {
        "username": user.get("username", "unknown") if user else "anonymous",
        "email": user.get("email", "") if user else "",
        "role": user.get("role", "") if user else "",
    }

    payload = {
        "channel": channel,
        "query": query,
        "summary": summary,
        "share_text": share_text,
        "session_id": session_id,
        "user": user_info,
        "table_summary": {
            "columns": table.get("columns", []) if table else [],
            "row_count": len(table.get("rows", [])) if table else 0,
        },
    }

    if channel == "clipboard":
        return {
            "success": True,
            "channel": "clipboard",
            "share_text": share_text,
        }

    webhook_urls = {
        "slack": settings.n8n_webhook_url,
        "email": settings.n8n_email_webhook_url,
        "whatsapp": settings.n8n_whatsapp_webhook_url,
    }
    webhook_url = webhook_urls.get(channel, settings.n8n_webhook_url)

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(webhook_url, json=payload)
            if resp.status_code >= 400:
                logger.warning(f"n8n returned {resp.status_code}: {resp.text[:200]}")
            return {
                "success": resp.status_code < 400,
                "channel": channel,
                "n8n_status": resp.status_code,
                "share_text": share_text,
            }
    except httpx.ConnectError:
        raise HTTPException(status_code=502, detail="n8n webhook unreachable. Check n8n service is running.")
    except Exception as e:
        logger.error(f"Share failed: {e}")
        raise HTTPException(status_code=500, detail=f"Share failed: {str(e)}")
