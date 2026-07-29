"""
Context Resolver — Extracts active conversation context from recent chat messages.

Provides contextual fallback for follow-up queries like:
  - "How many are there?" -> inherits last_entity_set ("Products")
  - "Show only ProductName" -> inherits last_entity_set ("Products") & last_columns
  - "Filter them by USA" -> inherits last_entity_set ("Customers")
"""
from typing import Any, Dict, List, Optional
from loguru import logger


def extract_session_context(messages: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Analyzes recent messages from get_messages(session_id) and returns context info:

    {
        "last_service_id": str or None,
        "last_entity_set": str or None,
        "last_filter": str or None,
        "last_select": list or None,
        "last_columns": list or None,
        "recent_turns": list of {"role": str, "content": str},
    }
    """
    context = {
        "last_service_id": None,
        "last_entity_set": None,
        "last_filter": None,
        "last_select": None,
        "last_columns": None,
        "recent_turns": [],
    }

    if not messages:
        return context

    # 1. Format recent turns for LLM prompt
    recent = messages[-6:]  # last 6 messages (3 turns)
    turns = []
    for msg in recent:
        role = msg.get("role")
        content = msg.get("content")
        if role in ("user", "assistant") and content:
            turns.append({"role": role, "content": content})
    context["recent_turns"] = turns

    # 2. Iterate backwards to find the last assistant message with plan/table result
    for msg in reversed(messages):
        if msg.get("role") != "assistant":
            continue

        plan = msg.get("plan")
        result = msg.get("result") or {}
        table = result.get("table") or {}

        # Extract columns from last table result if available
        if not context["last_columns"] and isinstance(table, dict) and table.get("columns"):
            context["last_columns"] = list(table["columns"])

        # Extract step info from plan
        if isinstance(plan, dict) and plan.get("steps"):
            step = plan["steps"][0]
            if isinstance(step, dict):
                if not context["last_service_id"]:
                    context["last_service_id"] = step.get("service_id")
                if not context["last_entity_set"]:
                    context["last_entity_set"] = step.get("entity_set")
                if not context["last_filter"]:
                    context["last_filter"] = step.get("filter")
                if not context["last_select"]:
                    context["last_select"] = step.get("select")

        # Also check target_services in plan
        if not context["last_service_id"] and isinstance(plan, dict) and plan.get("target_services"):
            target_svcs = plan["target_services"]
            if target_svcs and isinstance(target_svcs, list):
                context["last_service_id"] = target_svcs[0]

        # Stop once we have found last entity set & service
        if context["last_entity_set"] and context["last_service_id"]:
            break

    logger.debug(f"ContextResolver: extracted context = {context}")
    return context
