import os
import sys
from contextlib import asynccontextmanager
from typing import Any, Dict, List

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


@app.get("/roles")
async def get_roles():
    return policy_engine.list_roles()


@app.post("/chat", response_model=ChatResponse)
async def chat(payload: ChatRequest):
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
        plan_obj = Plan(**result["plan"])
    table_obj = None
    if result.get("table"):
        table_obj = TableData(**result["table"])

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


@app.get("/mcp/tools")
async def mcp_tools():
    return {"tools": mcp_server.tools}


@app.post("/mcp/call", response_model=MCPCallResponse)
async def mcp_call(payload: MCPCallRequest):
    result = await mcp_server.call_tool(payload.name, payload.arguments)
    return MCPCallResponse(result=result)
