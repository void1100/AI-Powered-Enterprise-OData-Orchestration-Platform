"""Shared dependencies and helpers used by multiple routers.

Keeps common utilities (probe helpers, LLM catalog, label builders) in one
place so every router can import from here instead of duplicating code.
"""
import time
import base64
from typing import Any, Dict, List, Optional

import httpx
from loguru import logger

from app.config import settings
from app.services.service_manager import service_manager


# ---------------------------------------------------------------------------
# LLM catalog — list of available LLM provider/model combos shown in the UI
# ---------------------------------------------------------------------------

LLM_CATALOG: List[Dict[str, Any]] = [
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


def llm_requirements_status() -> Dict[str, bool]:
    """Return a dict of which LLM keys/requirements are available."""
    return {
        "openai_key": bool(settings.openai_api_key),
        "gemini_key": bool(settings.gemini_api_key),
        "openrouter_key": bool(settings.openrouter_api_key),
        "nvidia_key": bool(settings.nvidia_api_key),
        "groq_base_url": "groq.com" in (settings.openai_base_url or ""),
    }


# ---------------------------------------------------------------------------
# Service probe helper — used by services router
# ---------------------------------------------------------------------------

async def probe_service(svc: Dict[str, Any]) -> Dict[str, Any]:
    """Probe a single OData service's $metadata endpoint for health status."""
    base = (svc.get("base_url") or "").rstrip("/")
    url = base if "metadata=true" in base.lower() else f"{base}/$metadata"
    t0 = time.perf_counter()
    try:
        auth_type = svc.get("auth_type")
        auth_config = svc.get("auth_config")
        headers = {"Accept": "application/xml"}
        if auth_type == "basic" and auth_config:
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


# ---------------------------------------------------------------------------
# Column label builder — used by chat and entity routers
# ---------------------------------------------------------------------------

def build_column_labels(service_id: str, entity_set: str, columns: list) -> dict:
    """Build column_labels dict from entity metadata using direct O(1) lookup."""
    svc_raw = service_manager.get_service(service_id)
    if not svc_raw:
        return {}
    entity_labels: Dict[str, Any] = {}
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
