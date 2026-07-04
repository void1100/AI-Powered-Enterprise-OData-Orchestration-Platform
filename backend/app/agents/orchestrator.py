"""The main orchestrator that wires together discovery, planning, policy,
execution, and memory.
"""
import uuid
from typing import Any, Dict, List, Optional, Tuple
import httpx
from loguru import logger

from app.agents.discovery_agent import discovery_agent
from app.agents.reasoning_engine import llm_engine
from app.agents.policy_engine import policy_engine
from app.db.vector_store import vector_store
from app.services.service_manager import service_manager
from app.services.column_filter import filter_columns


TOP_SAFETY_CAP = 200


def _to_list(value):
    if value is None:
        return None
    if isinstance(value, list):
        return [str(v) for v in value if v is not None]
    if isinstance(value, str):
        return [p.strip() for p in value.split(",") if p.strip()]
    return [str(value)]


def _normalize_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    if not plan:
        return plan
    normalized = dict(plan)
    intent = normalized.get("intent")
    if not isinstance(intent, str):
        normalized["intent"] = str(intent) if intent is not None else "unknown"
    summary = normalized.get("summary")
    if not isinstance(summary, str):
        normalized["summary"] = str(summary) if summary is not None else ""
    ts = normalized.get("target_services")
    if not isinstance(ts, list):
        if isinstance(ts, str):
            normalized["target_services"] = [s.strip() for s in ts.split(",") if s.strip()]
        else:
            normalized["target_services"] = []
    elif ts:
        # Extract strings from dicts: [{"id":"northwind"}] → ["northwind"]
        normalized["target_services"] = [
            s.get("id") or s.get("name") or str(s) if isinstance(s, dict) else str(s)
            for s in ts
        ]
    steps = normalized.get("steps") or []
    if not isinstance(steps, list):
        steps = []
    new_steps = []
    for step in steps:
        s = dict(step)
        s["select"] = _to_list(s.get("select"))
        s["expand"] = _to_list(s.get("expand"))
        ob = s.get("orderby")
        if isinstance(ob, list):
            s["orderby"] = ", ".join(str(x) for x in ob if x) if ob else None
        elif ob is not None and not isinstance(ob, str):
            s["orderby"] = str(ob)
        for str_field in ("service_id", "entity_set", "filter"):
            v = s.get(str_field)
            if v is not None and not isinstance(v, str):
                if isinstance(v, dict):
                    s[str_field] = v.get("id") or v.get("name") or str(v)
                elif isinstance(v, list):
                    s[str_field] = v[0] if v else ""
                else:
                    s[str_field] = str(v)
        for int_field in ("top", "skip"):
            v = s.get(int_field)
            if v == "" or v is None:
                s[int_field] = None
            elif isinstance(v, bool):
                s[int_field] = None
            elif isinstance(v, (int, float)):
                try:
                    s[int_field] = int(v)
                except (TypeError, ValueError, OverflowError):
                    s[int_field] = None
            elif isinstance(v, str):
                try:
                    s[int_field] = int(v.strip())
                except (TypeError, ValueError):
                    s[int_field] = None
            else:
                s[int_field] = None
        new_steps.append(s)
    normalized["steps"] = new_steps
    return normalized


def _apply_safety_caps(plan: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure every step has a $top cap to prevent accidental huge responses.
    SAP CPI services have strict row limits (often 2-5), so we use a lower cap."""
    if not plan:
        return plan
    for step in plan.get("steps", []):
        service_id = step.get("service_id", "")
        svc = service_manager._services.get(service_id, {})
        is_sap_cpi = "service=" in svc.get("base_url", "").lower() or "metadata=true" in svc.get("base_url", "").lower()
        cap = 2 if is_sap_cpi else TOP_SAFETY_CAP
        if step.get("top") is None:
            step["top"] = cap
        elif isinstance(step.get("top"), int) and step["top"] > cap:
            step["top"] = cap
    return plan


def _is_retryable_odata_error(exc: Exception) -> bool:
    """We only self-correct on client (4xx) and server (5xx) errors that
    look like bad planning, not on connection failures or auth issues."""
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code in (400, 404, 500, 501, 502, 503)
    return False


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
                "llm_provider": "n/a",
                "llm_latency_ms": 0,
                "llm_tokens": 0,
            }

        discovery = await discovery_agent.discover(user_query)
        plan, llm_meta = await llm_engine.plan(user_query, services, memory_context=memory)
        plan = _normalize_plan(plan)
        plan = _apply_safety_caps(plan)

        # For aggregation queries, remove $select so all columns are fetched
        from app.services.aggregator import detect_aggregation
        agg_info = detect_aggregation(user_query)
        if agg_info:
            for step in plan.get("steps", []):
                step["select"] = []
                step["filter"] = ""
                for key in list(step.keys()):
                    if key.lower() in ("groupby", "group_by", "aggregate", "aggregation"):
                        step.pop(key)
            logger.info(f"Aggregation detected: {agg_info}, cleared $select/$filter for full data fetch")
        llm_provider = llm_meta.get("provider", "unknown")
        llm_latency_ms = llm_meta.get("latency_ms", 0)
        llm_tokens = llm_meta.get("tokens", 0)

        tool_calls: List[Dict[str, Any]] = []
        execution_results: List[Dict[str, Any]] = []
        blocked_steps: List[Dict[str, Any]] = []
        primary_table = None
        primary_url = None
        primary_service = None
        error_message: Optional[str] = None
        corrected_step_indices: List[int] = []

        # Handle prediction intent
        if plan.get("intent") == "predict" and plan.get("prediction"):
            pred = plan["prediction"]
            entity_key = pred.get("entity_key", "")
            if isinstance(entity_key, list):
                entity_key = entity_key[0] if entity_key else ""
            features = pred.get("features", {})
            target = pred.get("target", "")
            from app.services.model_store import model_store
            prediction_result = model_store.predict(entity_key, features)
            if prediction_result:
                tool_calls.append({
                    "type": "prediction",
                    "entity_key": entity_key,
                    "target": target,
                    "features": features,
                    "prediction": prediction_result["prediction"],
                    "confidence": prediction_result["confidence_info"],
                })
                summary = (
                    f"Predicted **{target}** = **{prediction_result['prediction']}** "
                    f"based on {features}. "
                    f"{prediction_result['confidence_info']}"
                )
                return {
                    "run_id": run_id,
                    "session_id": session_id,
                    "user_query": user_query,
                    "user_role": user_role,
                    "summary": summary,
                    "plan": plan,
                    "discovery": discovery,
                    "tool_calls": tool_calls,
                    "blocked_steps": [],
                    "table": None,
                    "primary_url": None,
                    "primary_service": None,
                    "error": None,
                    "memory_used": memory,
                    "llm_provider": llm_provider,
                    "llm_latency_ms": llm_latency_ms,
                    "llm_tokens": llm_tokens,
                }
            else:
                error_message = f"No trained model available for '{entity_key}'. Query the data first to enable predictions."

        for idx, step in enumerate(plan.get("steps", [])):
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
                    "corrected": idx in corrected_step_indices,
                })
                execution_results.append(res)
                if primary_table is None:
                    primary_table = res["table"]
                    primary_url = res["url"]
                    primary_service = sid
            except Exception as e:
                if _is_retryable_odata_error(e) and not (idx in corrected_step_indices):
                    corrected_plan, corr_meta = await llm_engine.correct_plan(
                        original_query=user_query,
                        failed_plan=plan,
                        error_message=str(e),
                        available_services=services,
                    )
                    llm_tokens += corr_meta.get("tokens", 0)
                    llm_latency_ms += corr_meta.get("latency_ms", 0)
                    if corrected_plan and corrected_plan.get("steps"):
                        normalized_corrected = _normalize_plan(corrected_plan)
                        normalized_corrected = _apply_safety_caps(normalized_corrected)
                        replacement_step = normalized_corrected["steps"][0]
                        replacement_step = {**step, **replacement_step}
                        try:
                            role = policy_engine.get_role(user_role)
                            res2 = await service_manager.execute_plan(
                                service_id=replacement_step.get("service_id") or sid,
                                plan=replacement_step,
                                allowed_ops=role.get("allowed_ops"),
                            )
                            tool_calls.append({
                                "type": "odata.query",
                                "service_id": replacement_step.get("service_id") or sid,
                                "entity_set": replacement_step.get("entity_set") or ent,
                                "url": res2["url"],
                                "row_count": res2["table"]["row_count"],
                                "corrected": True,
                            })
                            execution_results.append(res2)
                            if primary_table is None:
                                primary_table = res2["table"]
                                primary_url = res2["url"]
                                primary_service = replacement_step.get("service_id") or sid
                            plan["steps"][idx] = replacement_step
                            corrected_step_indices.append(idx)
                            continue
                        except Exception as e2:
                            logger.warning(f"Self-correction retry failed: {e2}")
                            error_message = f"Step failed for service '{sid}' entity '{ent}': {e} (self-correction also failed: {e2})"
                            tool_calls.append({
                                "type": "odata.error",
                                "service_id": sid,
                                "entity_set": ent,
                                "error": str(e),
                                "correction_error": str(e2),
                            })
                            continue
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

        # Store successful plan in RAG for future retrieval
        if not error_message and primary_service:
            try:
                from app.services.query_rag import query_plan_rag
                rag_steps = plan.get("steps", [{}])
                entity = rag_steps[0].get("entity_set", "") if rag_steps else ""
                query_plan_rag.store_plan(
                    query=user_query,
                    plan=plan,
                    service_id=primary_service,
                    entity_set=entity,
                    success=True,
                )
                logger.info(f"RAG: Stored plan for '{user_query[:50]}' -> {primary_service}/{entity}")
            except Exception as e:
                logger.warning(f"RAG: Failed to store plan: {e}")

        # Apply column filter to remove useless columns
        if primary_table and primary_table.get("rows"):
            original_cols = len(primary_table.get("columns", []))
            primary_table = filter_columns(primary_table)
            filtered_cols = len(primary_table.get("columns", []))
            if original_cols != filtered_cols:
                logger.info(f"Column filter: {original_cols} -> {filtered_cols} columns")

        return {
            "run_id": run_id,
            "session_id": session_id,
            "user_query": user_query,
            "user_role": user_role,
            "discovery": discovery,
            "plan": _normalize_plan(plan),
            "tool_calls": tool_calls,
            "execution": execution_results,
            "blocked_steps": blocked_steps,
            "table": primary_table,
            "primary_url": primary_url,
            "primary_service": primary_service,
            "summary": summary,
            "error": error_message,
            "memory_used": memory,
            "llm_provider": llm_provider,
            "llm_latency_ms": llm_latency_ms,
            "llm_tokens": llm_tokens,
            "intent": llm_meta.get("intent"),
        }


orchestrator = Orchestrator()
