"""The main orchestrator that wires together discovery, planning, policy,
execution, and memory.
"""
import uuid
import re
import asyncio
from typing import Any, Dict, List, Optional, Tuple
import httpx
from loguru import logger

from app.agents.discovery_agent import discovery_agent
from app.agents.reasoning_engine import llm_engine
from app.agents.policy_engine import policy_engine
from app.db.vector_store import vector_store
from app.services.service_manager import service_manager
from app.services.column_filter import filter_columns
from app.services.guardrails import run_input_guards, run_output_guards


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
    SAP CPI services have strict row limits (often 2-5), so we use a lower cap.
    'show all' queries bypass the cap (top=100 signals all-rows intent)."""
    if not plan:
        return plan
    for step in plan.get("steps", []):
        service_id = step.get("service_id", "")
        svc = service_manager._services.get(service_id, {})
        is_sap_cpi = "service=" in svc.get("base_url", "").lower() or "metadata=true" in svc.get("base_url", "").lower()
        cap = 10 if is_sap_cpi else TOP_SAFETY_CAP
        if step.get("top") is None:
            step["top"] = cap
        elif isinstance(step.get("top"), int) and step["top"] > cap:
            # top=100 is the "show all" signal — don't cap it
            if step["top"] != 100:
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
        session_context = {}
        if session_id:
            memory = vector_store.search_memory(user_query, top_k=4, where={"session_id": session_id})
            try:
                from app.db.sqlite_store import get_messages
                from app.services.context_resolver import extract_session_context
                from app.services.langchain_memory import conversation_summary_memory
                session_msgs = get_messages(session_id, limit=20)
                session_context = extract_session_context(session_msgs)
                session_context = conversation_summary_memory.build_context(
                    session_msgs, session_context
                )
            except Exception as _ex:
                logger.warning(f"Failed to fetch session context: {_ex}")

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
        plan, llm_meta = await llm_engine.plan(
            user_query,
            services,
            memory_context=memory,
            chat_history=session_context.get("recent_turns"),
            session_context=session_context,
        )
        plan = _normalize_plan(plan)
        plan = _apply_safety_caps(plan)

        # ── Detect precise intent (count-only / column-select) ───────────────
        from app.services.query_intent_detector import detect_query_intent
        # Collect available columns from first plan step's entity or session context
        _step0 = plan.get("steps", [{}])[0] if plan.get("steps") else {}
        _svc0 = _step0.get("service_id", "") or session_context.get("last_service_id", "")
        _ent0 = _step0.get("entity_set", "") or session_context.get("last_entity_set", "")
        _avail_cols: List[str] = session_context.get("last_columns") or []
        if _svc0 and _ent0:
            _svc_match = next((s for s in services if s["id"] == _svc0), None)
            if _svc_match:
                _prop_cols = list(_svc_match.get("entity_properties", {}).get(_ent0, []))
                if _prop_cols:
                    _avail_cols = _prop_cols
        precise_intent = detect_query_intent(user_query, _avail_cols)

        # Context fallback for step entity set if missing
        if plan.get("steps"):
            s0 = plan["steps"][0]
            if not s0.get("entity_set") and session_context.get("last_entity_set"):
                s0["entity_set"] = session_context["last_entity_set"]
                s0["service_id"] = s0.get("service_id") or session_context.get("last_service_id", "")
                logger.info(f"Context fallback applied to plan step: {s0['entity_set']}")

        # ── Handle count_total: use server-side $count for accuracy ──────────
        if precise_intent["type"] == "count_total" and plan.get("steps"):
            step0 = plan["steps"][0]
            sid = step0.get("service_id")
            ent = step0.get("entity_set")
            flt = step0.get("filter") or None
            client = service_manager.get_client(sid) if sid else None
            if client and ent:
                try:
                    count_val = await client.get_count(entity_set=ent, filter_expr=flt)
                    if count_val is not None:
                        logger.info(f"count_total intent: {ent} count={count_val}")
                        count_table = {
                            "columns": ["total_count"],
                            "rows": [{"total_count": count_val}],
                            "row_count": 1,
                            "truncated": False,
                            "total_count": count_val,
                            "is_simple_count": True,
                        }
                        return {
                            "run_id": run_id,
                            "session_id": session_id,
                            "user_query": user_query,
                            "user_role": user_role,
                            "discovery": discovery,
                            "plan": _normalize_plan(plan),
                            "tool_calls": [{"type": "odata.count", "service_id": sid, "entity_set": ent, "count": count_val}],
                            "execution": [],
                            "blocked_steps": [],
                            "table": count_table,
                            "primary_url": client._build_url(ent) if hasattr(client, "_build_url") else None,
                            "primary_service": sid,
                            "summary": f"There are **{count_val:,}** {ent}.",
                            "error": None,
                            "memory_used": memory,
                            "llm_provider": llm_meta.get("provider", "unknown"),
                            "llm_latency_ms": llm_meta.get("latency_ms", 0),
                            "llm_tokens": llm_meta.get("tokens", 0),
                            "intent": "count_total",
                            "precise_intent": "count_total",
                        }
                except Exception as _ce:
                    logger.warning(f"count_total fast-path failed: {_ce}; falling through to normal flow")

        # ── Handle column_select: force $select to user-requested columns ────
        # Skip if plan already has explicit column selection (including select=[] for all columns)
        _has_explicit_select = any(step.get("select") is not None for step in plan.get("steps", []))
        if precise_intent["type"] == "column_select" and precise_intent.get("columns") and not _has_explicit_select:
            requested_cols = precise_intent["columns"]
            for step in plan.get("steps", []):
                step["select"] = requested_cols
                step["_user_column_select"] = True
            logger.info(f"column_select intent: forcing $select={requested_cols}")
        # Store precise intent on plan for downstream use
        plan["_precise_intent"] = precise_intent

        # For aggregation queries, remove $select so all columns are fetched
        from app.services.aggregator import detect_aggregation
        agg_info = detect_aggregation(user_query)
        if agg_info:
            for step in plan.get("steps", []):
                # Don't override a user-explicit column selection
                if not step.get("_user_column_select"):
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

        # Handle write intents (create/update/delete)
        write_intents = {"create", "update", "delete"}
        if plan.get("intent") in write_intents:
            write_op = plan.get("write_operation", {})
            entity_set = write_op.get("entity_set", "")
            operation = write_op.get("operation", plan.get("intent"))
            fields = write_op.get("fields", {})
            entity_id = write_op.get("entity_id")
            service_id = write_op.get("service_id") or (plan.get("target_services", [None])[0] if plan.get("target_services") else None)
            required_fields = write_op.get("required_fields", [])

            # If LLM didn't provide write_operation, try to detect entity from discovery
            if not write_op or not entity_set:
                # Try to detect entity from the query
                from app.services.guardrails import get_entity_field_requirements
                discovery_data = discovery or {}
                entities_found = discovery_data.get("entities", [])
                if entities_found:
                    # Pick the first entity found
                    entity_set = entities_found[0].get("entity_set", "")
                    service_id = entities_found[0].get("service_id", service_id)

            # Get entity field requirements
            from app.services.guardrails import get_entity_field_requirements
            field_reqs = get_entity_field_requirements(service_id, entity_set) if service_id and entity_set else {}
            entity_required = list(set(field_reqs.get("required_fields", []) + required_fields))

            # Run input guards
            guard_result = run_input_guards(
                user_role=user_role,
                user_id=session_id or "anonymous",
                entity_set=entity_set,
                operation=operation,
                fields=fields,
                required_fields=entity_required,
                confirmed=write_op.get("confirmed", False),
            )
            missing_fields = [f for f in entity_required if f not in fields or not fields.get(f)]
            guard_reason = guard_result.reason if not guard_result.allow else ""
            summary = f"**{operation.title()}** requires review before execution."
            if missing_fields:
                summary = f"**{operation.title()}** requires additional required fields before execution."

            return {
                "run_id": run_id,
                "session_id": session_id,
                "user_query": user_query,
                "user_role": user_role,
                "summary": summary,
                "plan": plan,
                "discovery": discovery,
                "tool_calls": ([{"type": "guardrail_block", "reason": guard_reason}] if guard_reason else []),
                "blocked_steps": [],
                "table": None,
                "primary_url": None,
                "primary_service": service_id,
                "error": None,
                "memory_used": memory,
                "llm_provider": llm_provider,
                "llm_latency_ms": llm_latency_ms,
                "llm_tokens": llm_tokens,
                "write_preview": {
                    "preview": True,
                    "operation": operation,
                    "entity_set": entity_set,
                    "service_id": service_id,
                    "fields": fields,
                    "entity_id": entity_id,
                    "required_fields": entity_required,
                    "optional_fields": field_reqs.get("optional_fields", [])[:10],
                    "auto_generated_fields": field_reqs.get("auto_generated_fields", []),
                    "missing_fields": missing_fields,
                    "confirmation_summary": (
                        f"**{operation.title()}** record in `{entity_set}` ({service_id}).\n\n"
                        + ("Required fields missing. Please provide the required values." if missing_fields else "Review the values below and confirm to execute this change.")
                    ),
                    "needs_user_input": True,
                },
            }

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
        elif primary_table is not None and primary_table.get("row_count", 0) == 0:
            entity_name = plan.get("steps", [{}])[0].get("entity_set", "") if plan.get("steps") else ""
            if re.search(r'(VH|StdVH|ValueHelp|Value_Help)$', entity_name):
                summary = f"Entity **{entity_name}** is a Value Help (dropdown metadata) and contains no data. Try querying a main data entity instead (e.g., production orders, confirmations, materials)."
            else:
                summary = f"No data found in **{entity_name}**. The entity may be empty or the SAP endpoint may have timed out. Try a different entity or add a filter."
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
        _pi = plan.get("_precise_intent", {})
        _user_selected_cols = _pi.get("type") == "column_select" and _pi.get("columns")
        if primary_table and primary_table.get("rows"):
            original_cols = len(primary_table.get("columns", []))

            if _user_selected_cols:
                # User asked for specific columns — enforce them exactly, skip smart/filter
                requested_cols = _pi["columns"]
                # Build case-insensitive resolved rows
                resolved_rows = []
                col_map = {c.lower(): c for c in primary_table.get("columns", [])}
                resolved_cols = [col_map.get(rc.lower(), rc) for rc in requested_cols]
                for r in primary_table.get("rows", []):
                    resolved_rows.append({col_map.get(rc.lower(), rc): r.get(col_map.get(rc.lower(), rc), "") for rc in requested_cols})
                primary_table["columns"] = resolved_cols
                primary_table["rows"] = resolved_rows
                logger.info(f"column_select: enforced columns={resolved_cols}")
            else:
                # Build smart column view using priority map
                try:
                    from app.services.column_priority import get_top_columns
                    entity_name = plan.get("steps", [{}])[0].get("entity_set", "") if plan.get("steps") else ""
                    all_cols = primary_table.get("all_columns", primary_table.get("columns", []))
                    svc_id = primary_service or ""
                    # Get metadata XML from service data or fetch from client
                    svc_data = service_manager._services.get(svc_id, {})
                    metadata_xml = svc_data.get("metadata_xml", "")
                    if not metadata_xml and svc_id in service_manager._clients:
                        client = service_manager._clients[svc_id]
                        metadata_xml = getattr(client, "metadata_xml", "")
                    smart_cols = await asyncio.wait_for(
                        asyncio.to_thread(
                            get_top_columns,
                            entity_set_name=entity_name,
                            service_id=svc_id,
                            all_fields=all_cols,
                            metadata_xml=metadata_xml,
                            max_columns=20,
                        ),
                        timeout=2.0,
                    )
                    if smart_cols and len(smart_cols) < len(all_cols):
                        smart_rows = [{k: row.get(k, "") for k in smart_cols} for row in (primary_table.get("all_rows") or primary_table.get("rows", []))]
                        primary_table["smart_columns"] = smart_cols
                        primary_table["smart_rows"] = smart_rows
                    else:
                        primary_table["smart_columns"] = all_cols
                        primary_table["smart_rows"] = primary_table.get("all_rows") or primary_table.get("rows", [])
                except asyncio.TimeoutError:
                    logger.warning("Smart column generation timed out; returning unprioritized table")
                    primary_table["smart_columns"] = primary_table.get("columns", [])
                    primary_table["smart_rows"] = primary_table.get("rows", [])
                except Exception as e:
                    logger.debug(f"Smart column generation failed: {e}")

                _show_all_query = bool(re.match(r"^(?:show|list|get|display|fetch)\s+(?:me\s+)?(?:all\s+)", user_query, re.IGNORECASE))
                _select_empty = not plan.get("steps", [{}])[0].get("select") if plan.get("steps") else False
                _show_all = _show_all_query or _select_empty
                primary_table = filter_columns(primary_table, show_all=_show_all)
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
            "precise_intent": _pi.get("type"),
        }


orchestrator = Orchestrator()
