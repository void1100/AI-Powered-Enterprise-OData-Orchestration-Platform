"""Advanced OData Service Orchestration — FastAPI application entry point.

This file is intentionally thin. All endpoint logic lives in the routers package:

    app/routers/
        chat.py           — /chat, /chat/analyze, /chat/write/*, /write/history, /share
        sessions.py       — /sessions, /sessions/{id}/messages
        services.py       — /services/*
        entities.py       — /entities/*
        custom_entities.py — /custom_entities/*
        joins.py          — /joins/*
        ml.py             — /analyze, /ml/*, /odata/*
        llm.py            — /llm/config, /roles, /cache/*, /suggestions
        mcp.py            — /mcp/*
"""
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from app.config import settings
from app.agents.policy_engine import policy_engine
from app.agents.reasoning_engine import llm_engine  # noqa: F401 — keeps singleton alive
from app.agents.orchestrator import orchestrator  # noqa: F401
from app.services.service_manager import service_manager

# ── Routers ─────────────────────────────────────────────────────────────────
from app.routers.chat import router as chat_router
from app.routers.sessions import router as sessions_router
from app.routers.services import router as services_router
from app.routers.entities import router as entities_router
from app.routers.custom_entities import router as custom_entities_router
from app.routers.joins import router as joins_router
from app.routers.ml import router as ml_router
from app.routers.llm import router as llm_router
from app.routers.mcp import router as mcp_router

# External integration routers (existing)
from app.services.n8n_integration import router as n8n_router
from app.services.twilio_webhook import router as twilio_router
from app.services.teams_webhook import router as teams_router
from app.admin.routes import router as admin_router


# ── Startup/shutdown ─────────────────────────────────────────────────────────

_recovery_complete = False


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


# ── Application ──────────────────────────────────────────────────────────────

app = FastAPI(
    title="Advanced OData Service Orchestration",
    description=(
        "AI-powered enterprise chatbot that converts natural-language questions "
        "into OData v4 queries across SAP and generic OData services."
    ),
    version="3.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Register routers ─────────────────────────────────────────────────────────
app.include_router(admin_router, tags=["auth", "admin"])
app.include_router(n8n_router)
app.include_router(twilio_router)
app.include_router(teams_router)

app.include_router(chat_router)
app.include_router(sessions_router)
app.include_router(services_router)
app.include_router(entities_router)
app.include_router(custom_entities_router)
app.include_router(joins_router)
app.include_router(ml_router)
app.include_router(llm_router)
app.include_router(mcp_router)


# ── Core system endpoints ────────────────────────────────────────────────────

@app.get("/", tags=["system"])
async def root():
    return {
        "name": "Advanced OData Service Orchestration",
        "version": "3.0.0",
        "status": "ok",
        "neo4j_connected": service_manager.graph().is_available(),
        "endpoints": ["/services", "/chat", "/sessions", "/mcp", "/roles"],
    }


@app.get("/health", tags=["system"])
async def health():
    from app.services.query_optimizer import query_optimizer
    from app.services.query_rag import query_plan_rag
    return {
        "status": "ok",
        "optimizer": query_optimizer.stats,
        "rag": query_plan_rag.get_stats(),
    }


@app.get("/ready", tags=["system"])
async def ready():
    """Health probe used by Docker to confirm the service is fully initialised."""
    from fastapi import HTTPException
    if not _recovery_complete:
        raise HTTPException(status_code=503, detail="Services still loading")
    return {"status": "ready"}
