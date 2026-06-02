"""The main orchestrator that wires together discovery, planning, policy,
execution, and memory.
"""
import uuid
from typing import Any, Dict, List, Optional
from loguru import logger

from app.agents.discovery_agent import discovery_agent
from app.agents.reasoning_engine import llm_engine
from app.agents.policy_engine import policy_engine
from app.db.vector_store import vector_store
from app.services.service_manager import service_manager


class Orchestrator:
    async def run(
        self,
        user_query: str,
        session_id: Optional[str] = None,
        user_role: str = "Admin",
    ) -> Dict[str, Any]:
        run_id = str(uuid.uuid4())
        memory = []
        if session_id:
            memory = vector_store.search_memory(user_query, top_k=4, where={"session_id": session_id})

        services = service_manager.list_services()
        if not services:
            return {
                "run_id": run_id,
                "session_id": session_id,
                "user_query": user_query,
                "user_role": user_role,
                "error": "No OData services are registered. Register a service first.",
                "plan": None,
                "discovery": None,
                "execution": None,
                "table": None,
                "summary": "No services available.",
                "memory_used": memory,
            }

        discovery = await discovery_agent.discover(user_query)
        plan = await llm_engine.plan(user_query, services, memory_context=memory)

        tool_calls: List[Dict[str, Any]] = []
        execution_results: List[Dict[str, Any]] = []
        blocked_steps: List[Dict[str, Any]] = []
        primary_table = None
        primary_url = None
        primary_service = None
        error_message: Optional[str] = None

        for step in plan.get("steps", []):
            sid = step.get("service_id")
            ent = step.get("entity_set")
            check = policy_engine.can_execute(user_role, sid, ent, step)
            if not check["allowed"]:
                blocked_steps.append({"step": step, "reason": check["reason"]})
                continue
            try:
                role = policy_engine.get_role(user_role)
                res = await service_manager.execute_plan(
                    service_id=sid,
                    plan=step,
                    allowed_ops=role.get("allowed_ops"),
                )
                tool_calls.append({
                    "type": "odata.query",
                    "service_id": sid,
                    "entity_set": ent,
                    "url": res["url"],
                    "row_count": res["table"]["row_count"],
                })
                execution_results.append(res)
                if primary_table is None:
                    primary_table = res["table"]
                    primary_url = res["url"]
                    primary_service = sid
            except Exception as e:
                logger.exception("Step execution failed")
                error_message = f"Step failed for service '{sid}' entity '{ent}': {e}"
                tool_calls.append({
                    "type": "odata.error",
                    "service_id": sid,
                    "entity_set": ent,
                    "error": str(e),
                })

        if session_id:
            try:
                vector_store.add_memory(
                    memory_id=f"{session_id}:{run_id}",
                    text=f"Q: {user_query}\nA: {plan.get('summary','')}",
                    metadata={"session_id": session_id, "run_id": run_id, "role": "qa"},
                )
            except Exception as e:
                logger.debug(f"Memory write failed: {e}")

        if not plan.get("steps"):
            summary = "I could not determine which OData service to use. Try registering a service or rephrasing."
        elif blocked_steps and not execution_results:
            summary = "All proposed steps were blocked by policy: " + "; ".join(s["reason"] for s in blocked_steps)
        elif error_message and not execution_results:
            summary = error_message
        elif primary_table is None:
            summary = plan.get("summary", "Done.")
        else:
            summary = plan.get("summary", "Done.")

        return {
            "run_id": run_id,
            "session_id": session_id,
            "user_query": user_query,
            "user_role": user_role,
            "discovery": discovery,
            "plan": plan,
            "tool_calls": tool_calls,
            "execution": execution_results,
            "blocked_steps": blocked_steps,
            "table": primary_table,
            "primary_url": primary_url,
            "primary_service": primary_service,
            "summary": summary,
            "error": error_message,
            "memory_used": memory,
        }


orchestrator = Orchestrator()
