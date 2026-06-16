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
from app.mcp.mcp_server import mcp_server


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting OData Orchestration backend...")
    policy_engine.ensure_default_roles()
    await service_manager.recover_from_graph()
    yield
    logger.info("Shutting down.")


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
    return {"status": "ok"}


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
    svc = await service_manager.register_service(
        service_id=payload.id,
        name=payload.name,
        base_url=payload.base_url,
        description=payload.description,
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
    del service_manager._services[service_id]
    service_manager._clients.pop(service_id, None)
    service_manager._entity_to_set.pop(service_id, None)
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
    url = f"{base}/$metadata"
    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            resp = await client.get(url, headers={"Accept": "application/xml"})
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
]


def _llm_requirements_status() -> Dict[str, bool]:
    return {
        "openai_key": bool(settings.openai_api_key),
        "gemini_key": bool(settings.gemini_api_key),
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

    session_id = payload.session_id
    if not session_id:
        session_id = create_session(title=payload.query[:50] or "New Chat", user_role=user_role)
    else:
        touch_session(session_id)

    add_message(session_id, "user", payload.query)

    # Direct prediction detection (bypass LLM for prediction queries)
    from app.services.model_store import model_store
    query_lower = payload.query.lower()
    prediction_keywords = ["predict", "what will", "forecast", "estimate", "project"]
    is_prediction = any(kw in query_lower for kw in prediction_keywords) and ("if" in query_lower or "given" in query_lower or "when" in query_lower)
    
    if is_prediction:
        models = model_store.list_models()
        if models:
            # Find best matching model
            best_model = None
            for m in models:
                ek = m["entity_key"].lower()
                if any(word in query_lower for word in ek.split("_")):
                    best_model = m
                    break
            if not best_model:
                best_model = models[0]
            
            # Extract feature values from query (simple heuristic)
            features = {}
            import re
            for m in models:
                for feat in m.get("feature_columns", []):
                    # Match patterns like "CategoryID is 2", "CategoryID = 2", "CategoryID 2"
                    pattern = rf'{re.escape(feat)}\s*(?:is|=|equals)?\s*(\d+\.?\d*)'
                    match = re.search(pattern, payload.query, re.IGNORECASE)
                    if match:
                        features[feat] = float(match.group(1))
            
            pred_result = model_store.predict(best_model["entity_key"], features)
            if pred_result:
                tool_calls = [{
                    "type": "prediction",
                    "entity_key": best_model["entity_key"],
                    "target": pred_result["target_column"],
                    "prediction": pred_result["prediction"],
                    "confidence": pred_result["confidence_info"],
                    "features": pred_result["features_used"],
                }]
                summary = (
                    f"Predicted **{pred_result['target_column']}** = **{pred_result['prediction']:.2f}** "
                    f"based on {pred_result['features_used']}. "
                    f"{pred_result['confidence_info']}. "
                    f"*(Model: {best_model['algorithm']}, trained on {best_model['sample_count']} samples)*"
                )
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

    result = await orchestrator.run(
        user_query=payload.query,
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

    # Auto-train model on query results for prediction capability
    if result.get("table") and result["table"].get("rows") and len(result["table"]["rows"]) >= 5:
        try:
            from app.services.model_store import model_store
            from app.services.ml_supervised import train_model
            table_data = result["table"]
            cols = table_data["columns"]
            rows = table_data["rows"]
            numeric_cols = [c for c in cols if not c.startswith("@odata.") and c != "odata.etag"]
            # Find best numeric target for regression
            for col in reversed(numeric_cols):
                try:
                    vals = [float(r[col]) for r in rows if r.get(col) is not None]
                    if len(vals) >= 5 and len(set(vals)) > 1:
                        entity_key = f"{result.get('primary_service', 'unknown')}_{result.get('plan', {}).get('steps', [{}])[0].get('entity_set', 'data')}"
                        train_result = train_model(rows, cols, col, "random_forest")
                        if "_model" in train_result:
                            model_store.store(
                                entity_key=entity_key,
                                model_obj=train_result["_model"],
                                feature_columns=train_result["feature_columns"],
                                target_column=col,
                                task_type=train_result["task_type"],
                                metrics=train_result["metrics"],
                                feature_importance=train_result.get("feature_importance", []),
                                algorithm="random_forest",
                                sample_count=train_result["sample_count"],
                            )
                            logger.info(f"Auto-trained model for {entity_key} targeting {col}")
                        break
                except (ValueError, TypeError):
                    continue
        except Exception as e:
            logger.warning(f"Auto-training failed: {e}")

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
    )


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

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(settings.n8n_webhook_url, json=payload)
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
