"""LLM router — provider/model configuration, roles, query cache, and suggestions."""
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

from app.auth import get_current_user
from app.agents.reasoning_engine import llm_engine
from app.agents.policy_engine import policy_engine
from app.routers.deps import LLM_CATALOG, llm_requirements_status

router = APIRouter(tags=["llm"])


@router.get("/llm/config")
async def get_llm_config():
    status = llm_requirements_status()
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


@router.post("/llm/config")
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
        status = llm_requirements_status()
        if not all(status.get(req, False) for req in opt["requires"]):
            missing = [req for req in opt["requires"] if not status.get(req, False)]
            raise HTTPException(status_code=400, detail=f"Cannot select {opt['label']}: missing {', '.join(missing)}")
        provider = opt["provider"]
        model = opt["model"]
    if not provider or not model:
        raise HTTPException(status_code=400, detail="Must provide 'provider' and 'model', or a valid 'id'")
    llm_engine.set_config(provider=provider, model=model)
    return {"ok": True, "provider": llm_engine.provider, "model": llm_engine.model}


@router.get("/roles")
async def get_roles():
    return policy_engine.list_roles()


@router.get("/suggestions")
async def get_suggestions():
    from app.services.query_enhancements import generate_suggestions
    from app.services.service_manager import service_manager
    return {"suggestions": generate_suggestions(service_manager.list_services())}


@router.get("/cache/stats")
async def get_cache_stats():
    from app.services.query_enhancements import query_cache
    from app.services.query_optimizer import query_optimizer
    query_stats = query_cache.stats()
    query_stats["optimizer"] = query_optimizer.stats
    return query_stats


@router.post("/cache/clear")
async def clear_cache():
    from app.services.query_enhancements import query_cache
    from app.services.query_optimizer import query_optimizer
    query_cache.clear()
    query_optimizer.clear_cache()
    return {"ok": True}
