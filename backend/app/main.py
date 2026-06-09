import os
import sys
import asyncio
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from app.config import settings
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
async def register_service(payload: ServiceRegister):
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
async def delete_service(service_id: str):
    if service_id not in service_manager._services:
        raise HTTPException(status_code=404, detail="Service not found")
    del service_manager._services[service_id]
    service_manager._clients.pop(service_id, None)
    service_manager._entity_to_set.pop(service_id, None)
    return {"deleted": service_id}


@app.post("/services/{service_id}/refresh", response_model=ServiceInfo)
async def refresh_service(service_id: str):
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
        async with httpx.AsyncClient(timeout=4.0, follow_redirects=True) as client:
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
async def set_llm_config(payload: Dict[str, Any]):
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
async def chat(payload: ChatRequest):
    if not service_manager._services:
        await service_manager.recover_from_graph()

    session_id = payload.session_id
    if not session_id:
        session_id = create_session(title=payload.query[:50] or "New Chat", user_role=payload.user_role)
    else:
        touch_session(session_id)

    add_message(session_id, "user", payload.query)
    result = await orchestrator.run(
        user_query=payload.query,
        session_id=session_id,
        user_role=payload.user_role,
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
async def analyze_table(payload: Dict[str, Any]):
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
async def ml_clean(payload: Dict[str, Any]):
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
async def ml_train(payload: Dict[str, Any]):
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
            algorithms = payload.get("algorithms", ["decision_tree", "random_forest", "logistic_regression", "xgboost", "gradient_boosting"])
            result = train_and_compare(table["rows"], table["columns"], target_col, algorithms)
        else:
            result = train_model(table["rows"], table["columns"], target_col, algorithm, options)
        return result
    except Exception as e:
        logger.error(f"ML training failed: {e}")
        raise HTTPException(status_code=500, detail=f"Training failed: {str(e)}")


@app.get("/ml/algorithms")
async def ml_algorithms():
    from app.services.ml_supervised import ALGORITHMS
    return {"algorithms": ALGORITHMS}


@app.get("/mcp/tools")
async def mcp_tools():
    return {"tools": mcp_server.tools}


@app.post("/mcp/call", response_model=MCPCallResponse)
async def mcp_call(payload: MCPCallRequest):
    result = await mcp_server.call_tool(payload.name, payload.arguments)
    return MCPCallResponse(result=result)
