"""Chat router — the core LLM chat pipeline, write operations, write history, and share.

This is the most complex router: it handles multi-entity joins, ML predictions,
multi-entity aggregation, LLM orchestration, post-processing, and result caching.
"""
import asyncio
import json
import re
import uuid
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request
from loguru import logger

from app.auth import get_current_user
from app.config import settings
from app.schemas.models import ChatRequest, ChatResponse, TableData
from app.services.service_manager import service_manager
from app.services.column_filter import filter_columns
from app.services.query_optimizer import query_optimizer
from app.agents.orchestrator import orchestrator, _normalize_plan
from app.agents.reasoning_engine import llm_engine
from app.db.sqlite_store import (
    add_message,
    add_run,
    create_session,
    get_messages,
    get_session,
    touch_session,
)
from app.db.usage_tracker import log_usage
from app.schemas.models import Plan
from app.routers.deps import build_column_labels

router = APIRouter(tags=["chat"])


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _log_chat_usage(
    session_id: str,
    user_query: str,
    user_role: str,
    provider: str,
    tokens: int,
    latency_ms: int,
    intent: str = "",
    cached: bool = False,
):
    try:
        log_usage(
            provider=provider,
            tokens=tokens,
            latency_ms=latency_ms,
            session_id=session_id,
            user_query=user_query,
            intent=intent,
            cached=cached,
            user_role=user_role,
        )
    except Exception:
        pass


def _do_auto_train(rows: list, cols: list) -> Optional[Dict[str, Any]]:
    """Run auto-train on fetched data. Returns auto_train_result dict or None."""
    if len(rows) < 10:
        return None
    try:
        from app.services.data_profiler import profile_table
        from app.services.llm_insights_engine import auto_select_algorithm
        from app.services.ml_supervised import train_model, _detect_task_type, _prepare_features, _encode_target
        from app.services.response_sanitizer import EXCLUDE_COLUMNS
        import numpy as np

        profile = profile_table(rows, cols)
        target_rec = profile.get("target_recommendation")
        if not target_rec:
            return None

        target_col = target_rec["column"]
        feature_cols = [c for c in cols if c != target_col and c not in EXCLUDE_COLUMNS]
        X, y_raw, _ = _prepare_features(rows, feature_cols, target_col)
        if len(X) < 5:
            return None
        task_type = _detect_task_type(y_raw)
        if task_type == "classification":
            y_enc, _ = _encode_target(y_raw)
            unique, counts = np.unique(y_enc, return_counts=True)
            if len(unique) < 2 or min(counts) < 3:
                return None

        algo_info = auto_select_algorithm(profile)
        algorithm = algo_info["algorithm"]
        train_result = train_model(rows, cols, target_col, algorithm)
        if "_model" in train_result:
            result = {
                "algorithm": train_result.get("algorithm", algorithm),
                "algorithm_key": algorithm,
                "target_column": target_col,
                "task_type": task_type,
                "metrics": train_result.get("metrics", {}),
                "sample_count": train_result.get("sample_count", len(rows)),
                "reason": algo_info.get("reason", ""),
            }
            logger.info(f"Auto-trained {algorithm} for {target_col} ({task_type}) — metrics: {train_result.get('metrics', {})}")
            del train_result["_model"]
            return result
    except Exception as e:
        logger.warning(f"Auto-training failed: {e}")
    return None


def _build_chat_response(session_id: str, user_query: str, user_role: str, **kwargs) -> ChatResponse:
    """Convenience factory for ChatResponse with required fields pre-filled."""
    defaults: Dict[str, Any] = {
        "run_id": str(uuid.uuid4()),
        "session_id": session_id,
        "user_query": user_query,
        "user_role": user_role,
        "discovery": None,
        "tool_calls": [],
        "blocked_steps": [],
        "table": None,
        "primary_url": None,
        "primary_service": None,
        "error": None,
        "memory_used": [],
        "llm_provider": "unknown",
        "llm_latency_ms": 0,
        "llm_tokens": 0,
    }
    defaults.update(kwargs)
    return ChatResponse(**defaults)


# ---------------------------------------------------------------------------
# /chat
# ---------------------------------------------------------------------------

@router.post("/chat", response_model=ChatResponse)
async def chat(payload: ChatRequest, request: Request):
    user = get_current_user(request)
    user_role = user.get("role", "user") if user else payload.user_role
    if not service_manager._services:
        await service_manager.recover_from_graph()

    if payload.selected_entities:
        logger.info(f"Chat received {len(payload.selected_entities)} selected entities: {payload.selected_entities}")

    session_id = payload.session_id
    if not session_id:
        user_id = user.get("sub") if user else None
        session_id = create_session(
            title=payload.query[:50] or "New Chat",
            user_role=user_role,
            user_id=user_id,
        )
    else:
        touch_session(session_id)

    add_message(session_id, "user", payload.query)

    def _log(provider: str, tokens: int, latency_ms: int, intent: str = "", cached: bool = False):
        _log_chat_usage(session_id, payload.query, user_role, provider, tokens, latency_ms, intent, cached)

    # ── Query cache ─────────────────────────────────────────────────────────
    from app.services.query_enhancements import query_cache, summarize_results, recommend_charts, get_drill_down_links
    cached_result = query_cache.get(payload.query, session_id)
    if cached_result:
        cached_result["cached"] = True
        _log("cached", 0, 0, intent="cached", cached=True)
        return ChatResponse(**cached_result)

    # ── Prediction fast-path ─────────────────────────────────────────────────
    from app.services.model_store import model_store
    query_lower = payload.query.lower()
    prediction_keywords = ["predict", "what will", "forecast", "estimate", "project"]
    is_prediction = any(kw in query_lower for kw in prediction_keywords)

    if is_prediction:
        models = model_store.list_models()
        if not models:
            _log("model_store", 0, 0, intent="predict")
            return _build_chat_response(
                session_id, payload.query, user_role,
                summary=(
                    "I don't have a trained model yet for predictions. "
                    "First, query the data (e.g. 'Show me products'), "
                    "then I can train a model and make predictions. "
                    "You can also explicitly train via the ML panel."
                ),
                plan={"intent": "predict", "note": "no_model"},
                llm_provider="model_store",
            )

        best_model = None
        for m in models:
            target = m.get("target_column", "").lower()
            if target and target in query_lower:
                best_model = m
                break
        if not best_model:
            for m in models:
                ek = m["entity_key"].lower()
                ek_parts = [p for p in ek.split("_") if len(p) > 2]
                if any(part in query_lower for part in ek_parts):
                    best_model = m
                    break
        if not best_model:
            best_model = models[0]

        features: Dict[str, Any] = {}
        for feat in best_model.get("feature_columns", []):
            feat_esc = re.escape(feat)
            for pattern in [
                rf'{feat_esc}\s*(?:is|=|equals|[:=])\s*(\d+\.?\d*)',
                rf'{feat_esc}\s+(\d+\.?\d*)',
                rf'(?:with|where|and)\s+{feat_esc}\s+(?:is\s+)?(\d+\.?\d*)',
            ]:
                m_obj = re.search(pattern, payload.query, re.IGNORECASE)
                if m_obj:
                    features[feat] = float(m_obj.group(1))
                    break

        if not features:
            all_numbers = re.findall(r'(\d+\.?\d*)', payload.query)
            numeric_feats = [f for f in best_model.get("feature_columns", [])
                             if best_model.get("task_type") == "regression" or f.lower() not in ("discontinued",)]
            for i, val in enumerate(all_numbers[:len(numeric_feats)]):
                features[numeric_feats[i]] = float(val)

        logger.info(f"Prediction: model={best_model['entity_key']}, features={features}")
        pred_result = model_store.predict(best_model["entity_key"], features)
        if pred_result:
            tool_calls = [{
                "type": "prediction",
                "entity_key": best_model["entity_key"],
                "target": pred_result["target_column"],
                "prediction": pred_result["prediction"],
                "confidence": pred_result["confidence_info"],
                "features": pred_result["features_used"],
                "task_type": pred_result.get("task_type", "regression"),
            }]
            pred_val = pred_result["prediction"]
            target = pred_result["target_column"]
            if pred_result.get("task_type") == "classification":
                label = "Yes" if pred_val >= 0.5 else "No"
                confidence_pct = pred_val * 100 if pred_val >= 0.5 else (1 - pred_val) * 100
                summary = (
                    f"**{target}** predicted as **{label}** "
                    f"(confidence: {confidence_pct:.0f}%). "
                    f"Based on features: {pred_result['features_used']}. "
                    f"*(Model: {best_model['algorithm']}, trained on {best_model['sample_count']} samples)*"
                )
            else:
                summary = (
                    f"Predicted **{target}** = **{pred_val:.2f}** "
                    f"based on {pred_result['features_used']}. "
                    f"{pred_result['confidence_info']}. "
                    f"*(Model: {best_model['algorithm']}, trained on {best_model['sample_count']} samples)*"
                )
            _log("model_store", 0, 0, intent="predict")
            return _build_chat_response(
                session_id, payload.query, user_role,
                summary=summary,
                plan={"intent": "predict", "prediction": pred_result},
                tool_calls=tool_calls,
                llm_provider="model_store",
            )

    # ── Selected entities fast-path ─────────────────────────────────────────
    from app.services.multi_entity_aggregator import detect_multi_entity_query, execute_multi_entity_aggregation

    if payload.selected_entities and len(payload.selected_entities) >= 1:
        selected = payload.selected_entities
        logger.info(f"Chat: {len(selected)} entities selected: {[e.get('entity_name') for e in selected]}")

        svc_entities: Dict[str, List[str]] = {}
        for e in selected:
            sid = e.get("service_id", "")
            ename = e.get("entity_name", "")
            if sid and ename:
                svc_entities.setdefault(sid, []).append(ename)

        if len(selected) >= 2:
            from app.services.entity_selector import entity_selector
            from app.services.cross_service_join import match_join

            entities_for_join = []
            for sid, enames in svc_entities.items():
                svc = next((s for s in service_manager.list_services() if s["id"] == sid), None)
                if not svc:
                    continue
                for ename in enames:
                    props = svc.get("entity_properties", {}).get(ename, [])
                    prop_names = [p if isinstance(p, str) else p.get("name", "") for p in props]
                    entities_for_join.append({"service_id": sid, "entity_name": ename, "properties": prop_names})

            detected_joins = entity_selector.detect_joins(entities_for_join)

            async def _fetch_entity(sid: str, ename: str):
                client = service_manager._clients.get(sid)
                if not client:
                    return None
                try:
                    resp = await client.query(entity_set=ename, top=10)
                    rows = client.flatten_odata_value(resp)
                    if rows:
                        cols = list(rows[0].keys())
                        logger.info(f"Chat entity join: fetched {len(rows)} rows from {ename}")
                        return {"service_id": sid, "entity_name": ename, "table": {"columns": cols, "rows": rows}}
                except Exception as ex:
                    logger.warning(f"Chat entity join: failed to fetch {ename}: {ex}")
                return None

            fetch_tasks = [
                _fetch_entity(sid, ename)
                for sid, enames in svc_entities.items()
                for ename in enames
            ]
            fetch_results = await asyncio.gather(*fetch_tasks)
            all_results = [r for r in fetch_results if r is not None]

            if all_results and detected_joins:
                sorted_joins = sorted(detected_joins, key=lambda j: -j.get("confidence", 0))
                result_table = all_results[0]["table"]
                used_right: set = set()
                for join_def in sorted_joins:
                    lk = join_def.get("left_key", "")
                    rk = join_def.get("right_key", "")
                    right_entity_name = join_def.get("right_entity", "")
                    if not lk or not rk:
                        continue
                    right_result = None
                    for r in all_results:
                        if r["entity_name"] == right_entity_name and r["entity_name"] not in used_right:
                            right_result = r["table"]
                            used_right.add(r["entity_name"])
                            break
                    if right_result:
                        result_table = match_join(
                            result_table, right_result,
                            left_key=lk, right_key=rk,
                            left_service=all_results[0]["service_id"],
                            right_service=right_entity_name,
                        )

                rows = result_table.get("rows", [])[:100]
                columns = result_table.get("columns", [])
                filtered = filter_columns({"columns": columns, "rows": rows, "row_count": len(rows)})
                columns, rows = filtered["columns"], filtered["rows"]

                if rows:
                    entity_names = [e.get("entity_name") for e in selected]
                    summary = f"Joined {len(entity_names)} entities ({', '.join(entity_names)}): {len(rows)} rows, {len(columns)} columns"
                    tool_calls_me = [{"type": "entity_join", "entities": entity_names, "joins": len(detected_joins), "row_count": len(rows)}]
                    all_col_labels: Dict[str, str] = {}
                    for ent in selected:
                        all_col_labels.update(build_column_labels(ent.get("service_id", ""), ent.get("entity_name", ""), columns))
                    add_message(session_id, "assistant", summary, plan=None, result={"table": {"columns": columns, "rows": rows, "row_count": len(rows), "column_labels": all_col_labels or None}, "tool_calls": tool_calls_me})
                    _log("entity_join", 0, 0, intent="entity_join")
                    atr = _do_auto_train(rows, columns)
                    return _build_chat_response(
                        session_id, payload.query, user_role,
                        summary=summary,
                        plan={"intent": "entity_join", "entities": entity_names},
                        tool_calls=tool_calls_me,
                        table=TableData(columns=columns, rows=rows, row_count=len(rows), column_labels=all_col_labels or None),
                        primary_service=selected[0].get("service_id", ""),
                        llm_provider="entity_join",
                        auto_train_result=atr,
                    )
                else:
                    # Join returned 0 rows — show individual entity results
                    entity_names = [e.get("entity_name") for e in selected]
                    combined_rows: list = []
                    combined_cols: list = []
                    for r in all_results:
                        t = r["table"]
                        if t.get("rows"):
                            if not combined_cols:
                                combined_cols = t["columns"]
                            combined_rows.extend(t["rows"][:5])
                    if combined_rows:
                        filtered = filter_columns({"columns": combined_cols, "rows": combined_rows, "row_count": len(combined_rows)})
                        combined_cols, combined_rows = filtered["columns"], filtered["rows"]
                        summary = f"No matching rows found between {', '.join(entity_names)} — showing individual entity data ({len(combined_rows)} rows)"
                        tool_calls_me = [{"type": "entity_select", "entities": entity_names, "row_count": len(combined_rows)}]
                        all_col_labels = {}
                        for ent in selected:
                            all_col_labels.update(build_column_labels(ent.get("service_id", ""), ent.get("entity_name", ""), combined_cols))
                        add_message(session_id, "assistant", summary, plan=None, result={"table": {"columns": combined_cols, "rows": combined_rows, "row_count": len(combined_rows), "column_labels": all_col_labels or None}, "tool_calls": tool_calls_me})
                        _log("entity_select", 0, 0, intent="entity_select")
                        return _build_chat_response(
                            session_id, payload.query, user_role,
                            summary=summary,
                            plan={"intent": "entity_select", "entities": entity_names},
                            tool_calls=tool_calls_me,
                            table=TableData(columns=combined_cols, rows=combined_rows, row_count=len(combined_rows), column_labels=all_col_labels or None),
                            primary_service=selected[0].get("service_id", ""),
                            llm_provider="entity_select",
                        )

            elif all_results:
                entity_names = [e.get("entity_name") for e in selected]
                combined_rows = []
                combined_cols = []
                for r in all_results:
                    t = r["table"]
                    if t.get("rows"):
                        if not combined_cols:
                            combined_cols = t["columns"]
                        combined_rows.extend(t["rows"][:5])
                if combined_rows:
                    filtered = filter_columns({"columns": combined_cols, "rows": combined_rows, "row_count": len(combined_rows)})
                    combined_cols, combined_rows = filtered["columns"], filtered["rows"]
                    summary = f"Selected {len(entity_names)} entities: {', '.join(entity_names)} — {len(combined_rows)} rows total"
                    tool_calls_me = [{"type": "entity_select", "entities": entity_names, "row_count": len(combined_rows)}]
                    all_col_labels = {}
                    for ent in selected:
                        all_col_labels.update(build_column_labels(ent.get("service_id", ""), ent.get("entity_name", ""), combined_cols))
                    add_message(session_id, "assistant", summary, plan=None, result={"table": {"columns": combined_cols, "rows": combined_rows, "row_count": len(combined_rows), "column_labels": all_col_labels or None}, "tool_calls": tool_calls_me})
                    _log("entity_select", 0, 0, intent="entity_select")
                    return _build_chat_response(
                        session_id, payload.query, user_role,
                        summary=summary,
                        plan={"intent": "entity_select", "entities": entity_names},
                        tool_calls=tool_calls_me,
                        table=TableData(columns=combined_cols, rows=combined_rows, row_count=len(combined_rows), column_labels=all_col_labels or None),
                        primary_service=selected[0].get("service_id", ""),
                        llm_provider="entity_select",
                    )

        elif len(selected) == 1:
            sid = selected[0].get("service_id", "")
            ename = selected[0].get("entity_name", "")
            client = service_manager._clients.get(sid)
            if client:
                try:
                    resp = await client.query(entity_set=ename, top=10)
                    rows = client.flatten_odata_value(resp)
                    if rows:
                        cols = list(rows[0].keys())
                        filtered = filter_columns({"columns": cols, "rows": rows, "row_count": len(rows)})
                        cols, rows = filtered["columns"], filtered["rows"]
                        summary = f"Showing {len(rows)} rows from {ename}"
                        _log("entity_select", 0, 0, intent="entity_select")
                        atr = _do_auto_train(rows, cols)
                        tool_calls_me = [{"type": "entity_select", "service_id": sid, "entity": ename, "row_count": len(rows)}]
                        col_labels = build_column_labels(sid, ename, cols)
                        add_message(session_id, "assistant", summary, plan=None, result={"table": {"columns": cols, "rows": rows, "row_count": len(rows), "column_labels": col_labels or None}, "tool_calls": tool_calls_me})
                        return _build_chat_response(
                            session_id, payload.query, user_role,
                            summary=summary,
                            plan={"intent": "entity_select", "entity": ename},
                            tool_calls=tool_calls_me,
                            table=TableData(columns=cols, rows=rows, row_count=len(rows), column_labels=col_labels or None),
                            primary_service=sid,
                            llm_provider="entity_select",
                            auto_train_result=atr,
                        )
                except Exception as ex:
                    logger.warning(f"Chat entity select: failed to fetch {ename}: {ex}")

    # ── Multi-entity aggregation ─────────────────────────────────────────────
    services_list = service_manager.list_services()
    q_lower = payload.query.lower()
    q_compact = re.sub(r"[^a-z0-9]", "", q_lower)
    exact_entity_requested = any(
        (
            es.lower() in q_lower
            or es.lower().replace("_", " ") in q_lower
            or re.sub(r"[^a-z0-9]", "", es.lower()) in q_compact
        )
        for svc in services_list
        for es in svc.get("entity_sets", [])
    )
    aggregate_keywords = (" by ", " per ", "count", "total", "sum", "average", "avg", "join", "combine", "compare")
    should_try_multi_entity = (not exact_entity_requested) and any(k in f" {q_lower} " for k in aggregate_keywords)
    explicit_service = None
    for svc in services_list:
        if svc["id"].lower() in q_lower or svc["name"].lower() in q_lower:
            explicit_service = svc["id"]
            break
    services_to_check = [s for s in services_list if should_try_multi_entity and (not explicit_service or s["id"] == explicit_service)]
    for svc in services_to_check:
        svc_id = svc["id"]
        client = service_manager.get_client(svc_id)
        if not client:
            continue
        entity_cols: Dict[str, List[str]] = {}
        for es in svc.get("entity_sets", []):
            es_lower = es.lower()
            if any(vp in es_lower for vp in ("summary", "by_", "for_", "list_of", "extended", "subtotal", "quarterly", "annual")):
                continue
            try:
                raw = await asyncio.wait_for(client.query(entity_set=es, top=1), timeout=3.0)
                flat = client.flatten_odata_value(raw)
                if flat:
                    entity_cols[es] = [c for c in flat[0].keys() if not c.startswith("@odata")]
            except Exception:
                pass
        if not entity_cols:
            continue
        me_info = detect_multi_entity_query(payload.query, svc_id, entity_cols)
        if me_info:
            me_result = await execute_multi_entity_aggregation(payload.query, svc_id, client, me_info)
            if me_result:
                tool_calls_me = [{"type": "multi_entity", "service_id": svc_id, "chain": [s["entity"] for s in me_info["chain"]], "row_count": me_result["row_count"]}]
                add_message(session_id, "assistant", me_result.get("summary", ""), plan=None, result={"table": me_result, "tool_calls": tool_calls_me})
                me_chart_recs = []
                try:
                    me_chart_recs = recommend_charts(me_result.get("rows", []), me_result.get("columns", []), payload.query)
                except Exception:
                    pass
                _log("multi_entity", 0, 0, intent="aggregate")
                return _build_chat_response(
                    session_id, payload.query, user_role,
                    summary=me_result.get("summary", "Multi-entity aggregation complete"),
                    plan={"intent": "aggregate", "summary": me_result.get("summary", "")},
                    tool_calls=tool_calls_me,
                    table=TableData(**me_result) if me_result else None,
                    primary_service=svc["id"],
                    llm_provider="computed",
                    chart_recommendations=me_chart_recs,
                )

    # ── Entity candidate clarification ──────────────────────────────────────
    candidates = llm_engine.find_entity_candidates(services_list, payload.query, limit=5)
    if not exact_entity_requested and len(candidates) >= 2:
        top_score = candidates[0].get("score", 0)
        close_candidates = [c for c in candidates if top_score and c.get("score", 0) >= top_score * 0.7][:3]
        if len(close_candidates) >= 2:
            candidate_lines = [
                f"- {c.get('entity_set', '')} ({c.get('service_name') or c.get('service_id', 'service')})"
                for c in close_candidates
            ]
            summary = (
                "I found multiple possible entities for your request. Choose the one that matches what you mean."
                "\n\n" + "\n".join(candidate_lines)
            )
            clarification = {"type": "entity_choice", "query": payload.query, "candidates": close_candidates}
            tool_calls_clarify = [{"type": "entity_clarification", "candidate_count": len(close_candidates), "candidates": close_candidates}]
            add_message(session_id, "assistant", summary, plan={"intent": "clarify", "summary": summary}, result={"tool_calls": tool_calls_clarify, "clarification": clarification})
            return _build_chat_response(
                session_id, payload.query, user_role,
                summary=summary,
                plan={"intent": "clarify", "summary": summary},
                tool_calls=tool_calls_clarify,
                llm_provider="entity-candidates",
                clarification=clarification,
            )

    # ── Main LLM orchestration pipeline ─────────────────────────────────────
    orchestrator_query = payload.query
    if not exact_entity_requested and candidates:
        top = candidates[0]
        second_score = candidates[1].get("score", 0) if len(candidates) > 1 else 0
        is_complex = query_optimizer._is_complex_query(payload.query.lower())
        if not is_complex and top.get("score", 0) >= 1.2 and (not second_score or top.get("score", 0) >= second_score * 1.6):
            # Pass original query — entity-match path in reasoning_engine will detect entity
            orchestrator_query = payload.query

    result = await orchestrator.run(user_query=orchestrator_query, session_id=session_id, user_role=user_role)

    add_message(session_id, "assistant", result.get("summary", ""), plan=result.get("plan"), result={"table": result.get("table"), "tool_calls": result.get("tool_calls")})
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
        for _ in range(3):
            try:
                plan_obj = Plan(**result["plan"])
                break
            except Exception as e:
                logger.warning(f"Plan validation failed, re-normalizing: {e}")
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

    # Post-fetch aggregation
    from app.services.aggregator import detect_aggregation, aggregate
    agg_info = detect_aggregation(payload.query)
    # Skip re-aggregation if orchestrator already handled count_total
    _precise_intent = result.get("precise_intent")
    if agg_info and result.get("table") and result["table"].get("rows") and _precise_intent != "count_total":
        try:
            t = result["table"]
            agg_result = aggregate(t["rows"], t["columns"], agg_info)
            result["table"] = agg_result
            table_obj = TableData(**agg_result)
            func_label = agg_info["func"].upper()
            group_label = agg_info.get("group_by") or agg_info.get("agg_col") or ""
            if agg_result.get("is_simple_count"):
                # Single total count — give precise human-friendly summary
                count_val = agg_result["rows"][0].get("total_count", len(agg_result["rows"]))
                entity_hint = (result.get("plan", {}) or {}).get("steps", [{}])[0].get("entity_set", "records")
                result["summary"] = f"There are **{count_val:,}** {entity_hint}."
            else:
                result["summary"] = f"Aggregated result: {func_label} by {group_label} ({agg_result['row_count']} groups from {t.get('row_count', '?')} rows)"
        except Exception as e:
            logger.warning(f"Aggregation failed: {e}")

    # Post-aggregation computation
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
                min_pct = pp_info.get("min_percentage")
                result["summary"] = f"Percentage breakdown ({pp_result['row_count']} groups{' with > ' + str(min_pct) + '%' if min_pct is not None else ''})"
            elif pp_type == "comparison":
                result["summary"] = f"Comparison result ({pp_result['row_count']} entries)"
            elif pp_type in ("which_extremum", "extremum"):
                result_row = next((r for r in pp_result.get("rows", []) if "result" in r), None)
                result["summary"] = result_row["result"] if result_row else f"Found the extremum ({pp_result['row_count']} entries)"
            elif pp_type == "ratio":
                result["summary"] = f"Ratio calculation ({pp_result['row_count']} entries)"
        except Exception as e:
            logger.warning(f"Post-processing failed: {e}")

    # ── Precise intent finalization (count_total / column_select) ────────────
    from app.services.query_intent_detector import detect_query_intent
    _table_cols = result.get("table", {}).get("columns", []) if result.get("table") else []
    if not _table_cols and session_id:
        try:
            from app.services.context_resolver import extract_session_context
            _ctx = extract_session_context(get_messages(session_id, limit=10))
            _table_cols = _ctx.get("last_columns") or []
        except Exception:
            pass
    _precise = detect_query_intent(payload.query, _table_cols)

    if _precise["type"] == "count_total":
        t = result.get("table") or {}
        # Check if the table IS already a count result
        if t.get("is_simple_count") or (t.get("columns") == ["total_count"] and t.get("rows")):
            count_val = t["rows"][0].get("total_count", t.get("total_count", "?"))
            entity_hint = ""
            plan_data = result.get("plan") or {}
            steps = plan_data.get("steps", []) if isinstance(plan_data, dict) else []
            if steps:
                entity_hint = steps[0].get("entity_set", "")
            result["summary"] = f"There are **{count_val:,}** {entity_hint}." if entity_hint else f"Total count: **{count_val:,}**"
            # Keep only the count row — no extra rows
            result["table"] = {
                "columns": ["total_count"],
                "rows": [{"total_count": count_val}],
                "row_count": 1,
                "truncated": False,
                "total_count": count_val,
                "is_simple_count": True,
            }
            table_obj = TableData(**result["table"])

    elif _precise["type"] == "column_select" and _precise.get("columns") and result.get("table"):
        t = result["table"]
        requested_cols = _precise["columns"]
        col_map = {c.lower(): c for c in t.get("columns", [])}
        resolved_cols = [col_map.get(rc.lower(), rc) for rc in requested_cols if col_map.get(rc.lower())]
        if resolved_cols:
            new_rows = [{c: r.get(c, "") for c in resolved_cols} for r in t.get("rows", [])]
            result["table"] = {**t, "columns": resolved_cols, "rows": new_rows}
            table_obj = TableData(**result["table"])
            logger.info(f"chat.py: column_select finalized to {resolved_cols}")


    auto_train_result = None
    if result.get("table") and result["table"].get("rows") and len(result["table"]["rows"]) >= 5:
        auto_train_result = _do_auto_train(result["table"]["rows"], result["table"]["columns"])

    chart_recs: list = []
    drill_links: list = []
    if result.get("table") and result["table"].get("rows"):
        t = result["table"]
        try:
            chart_recs = recommend_charts(t["rows"], t["columns"], payload.query)
        except Exception as e:
            logger.warning(f"Chart recommendation failed: {e}")
        try:
            entity_set_name = ""
            plan_data = plan_obj if plan_obj else result.get("plan")
            if plan_data:
                steps = getattr(plan_data, "steps", None) or (plan_data.get("steps") if isinstance(plan_data, dict) else None)
                if steps and len(steps) > 0:
                    step = steps[0]
                    entity_set_name = getattr(step, "entity_set", "") or (step.get("entity_set") if isinstance(step, dict) else "")
            drill_links = get_drill_down_links(entity_set_name, t["rows"][0], service_manager.list_services())
        except Exception as e:
            logger.warning(f"Drill-down link generation failed: {e}")

    # Cache result
    try:
        table_data = result.get("table")
        has_table = table_data and table_data.get("rows") and len(table_data.get("rows", [])) > 0
        response_data = {
            "run_id": result["run_id"],
            "session_id": session_id,
            "user_query": result["user_query"],
            "user_role": result["user_role"],
            "summary": result["summary"],
            "plan": plan_obj.model_dump() if plan_obj else None,
            "discovery": result.get("discovery"),
            "tool_calls": result.get("tool_calls", []),
            "blocked_steps": result.get("blocked_steps", []),
            "table": table_data if has_table else None,
            "primary_url": result.get("primary_url"),
            "primary_service": result.get("primary_service"),
            "error": result.get("error"),
            "memory_used": result.get("memory_used", []),
            "llm_provider": result.get("llm_provider", "unknown"),
            "llm_latency_ms": result.get("llm_latency_ms", 0),
            "llm_tokens": result.get("llm_tokens", 0),
            "chart_recommendations": chart_recs,
            "drill_down_links": drill_links,
        }
        if has_table and not result.get("error"):
            query_cache.set(payload.query, response_data, session_id)
    except Exception:
        pass

    _log(
        result.get("llm_provider", "unknown"),
        result.get("llm_tokens", 0),
        result.get("llm_latency_ms", 0),
        intent=result.get("plan", {}).get("intent", "") if isinstance(result.get("plan"), dict) else "",
    )
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
        chart_recommendations=chart_recs,
        drill_down_links=drill_links,
        intent=result.get("intent"),
        auto_train_result=auto_train_result,
        write_preview=result.get("write_preview"),
    )


# ---------------------------------------------------------------------------
# /chat/analyze
# ---------------------------------------------------------------------------

@router.post("/chat/analyze")
async def chat_analyze(payload: ChatRequest, request: Request):
    """On-demand insights: analyze the data from a previous query using LLM."""
    from app.services.data_profiler import profile_table
    from app.services.llm_insights_engine import generate_insights

    user = get_current_user(request)
    user_role = user.get("role", "user") if user else payload.user_role
    session_id = payload.session_id
    if not session_id:
        return {"error": "session_id required for analysis"}

    messages = get_messages(session_id, limit=5)
    table_data = None
    for msg in reversed(messages):
        if msg.get("role") == "assistant":
            result = msg.get("result")
            if result and result.get("table"):
                table_data = result["table"]
                break

    if not table_data or not table_data.get("rows"):
        return {"error": "No data found in session to analyze", "insights": None}

    profile = profile_table(table_data["rows"], table_data["columns"])
    provider = payload.llm_provider if hasattr(payload, "llm_provider") else None
    insights = await generate_insights(profile, payload.query, table_data, provider=provider or "auto")

    try:
        log_usage(provider="llm_insights", tokens=0, latency_ms=0, session_id=session_id, user_query=payload.query, intent="analyze")
    except Exception:
        pass

    return {
        "profile": {
            "row_count": profile.get("row_count", 0),
            "column_count": profile.get("column_count", 0),
            "numeric_columns": profile.get("numeric_columns", []),
            "categorical_columns": profile.get("categorical_columns", []),
            "quality_score": profile.get("quality_score", 0),
            "correlations": profile.get("correlations", []),
            "outlier_summary": profile.get("outlier_summary", {}),
        },
        "insights": insights.get("insights", []),
        "suggestions": insights.get("suggestions", []),
        "ml_recommendation": insights.get("ml_recommendation", {}),
        "chart_insights": insights.get("chart_insights", []),
        "summary": insights.get("summary", ""),
    }


# ---------------------------------------------------------------------------
# /chat/write/* + /write/history
# ---------------------------------------------------------------------------

@router.get("/write/history")
async def get_write_history_endpoint(limit: int = 50, operation: str = "", entity_set: str = ""):
    """Get write operation history for audit trail."""
    from app.services.guardrails import get_write_history, get_write_history_stats
    return {
        "stats": get_write_history_stats(),
        "history": get_write_history(limit=limit, operation=operation, entity_set=entity_set),
    }


@router.post("/chat/write/preview")
async def write_preview(payload: ChatRequest, request: Request):
    """Preview a write operation and return confirmation summary before executing."""
    from app.services.guardrails import build_write_summary, run_input_guards, get_entity_field_requirements

    user = get_current_user(request)
    user_role = user.get("role", "user") if user else payload.user_role

    services = service_manager.list_services()
    plan, llm_meta = await llm_engine.plan(payload.query, services, memory_context=[])

    write_intents = {"create", "update", "delete"}
    q_lower = payload.query.lower()
    inferred_intent = ""
    if re.search(r"\b(create|add|new|insert|submit)\b", q_lower):
        inferred_intent = "create"
    elif re.search(r"\b(update|modify|change|edit|set|replace)\b", q_lower):
        inferred_intent = "update"
    elif re.search(r"\b(delete|remove|destroy|drop)\b", q_lower):
        inferred_intent = "delete"

    intent = inferred_intent or plan.get("intent", "")
    if intent not in write_intents:
        return {"error": "Could not detect a write operation in your query. Try phrases like 'create a new order' or 'update customer X'."}

    write_op = plan.get("write_operation")
    if not write_op:
        first_step = plan.get("steps", [{}])[0] if plan.get("steps") else {}
        write_op = {
            "operation": intent,
            "entity_set": first_step.get("entity_set", ""),
            "service_id": first_step.get("service_id", ""),
            "fields": {},
            "entity_id": None,
            "confirmed": False,
        }

    entity_set = write_op.get("entity_set", "")
    operation = write_op.get("operation", intent)
    fields = write_op.get("fields", {})
    entity_id = write_op.get("entity_id")
    service_id = write_op.get("service_id") or (plan.get("target_services", [None])[0] if plan.get("target_services") else None)
    required_fields = write_op.get("required_fields", [])

    if entity_set and not service_id:
        for svc in services:
            if entity_set in svc.get("entity_sets", []):
                service_id = svc.get("id")
                break

    if not entity_set:
        q_words = set(re.findall(r"[a-z]+", q_lower)) - write_intents - {"a", "an", "the", "new"}
        best_match = None
        for svc in services:
            for candidate in svc.get("entity_sets", []):
                candidate_words = set(re.findall(r"[a-z]+", candidate.lower()))
                overlap = q_words & candidate_words
                if overlap and (not best_match or len(overlap) > best_match[0]):
                    best_match = (len(overlap), svc.get("id"), candidate)
        if best_match:
            _, service_id, entity_set = best_match

    field_reqs = get_entity_field_requirements(service_id, entity_set)
    entity_required = field_reqs.get("required_fields", [])
    entity_optional = field_reqs.get("optional_fields", [])
    auto_generated = field_reqs.get("auto_generated_fields", [])
    all_required = list(set(entity_required + required_fields))

    guard_result = run_input_guards(
        user_role=user_role,
        user_id=payload.session_id or "anonymous",
        entity_set=entity_set,
        operation=operation,
        fields=fields,
        required_fields=all_required,
        confirmed=False,
    )
    missing = [f for f in all_required if f not in fields or not fields.get(f)]

    failed_guard = guard_result.metadata.get("guard") if guard_result.metadata else ""

    if not guard_result.allow and missing:
        summary = build_write_summary(operation=operation, entity_set=entity_set, fields=fields, service_id=service_id, missing_fields=missing)
        return {
            "preview": True,
            "operation": operation,
            "entity_set": entity_set,
            "service_id": service_id,
            "fields": fields,
            "entity_id": entity_id,
            "required_fields": all_required,
            "optional_fields": entity_optional[:10],
            "auto_generated_fields": auto_generated,
            "missing_fields": missing,
            "confirmation_summary": summary,
            "needs_user_input": True,
        }
    if not guard_result.allow and failed_guard == "confirmation":
        summary = build_write_summary(operation=operation, entity_set=entity_set, fields=fields, service_id=service_id, missing_fields=missing)
        return {
            "preview": True,
            "operation": operation,
            "entity_set": entity_set,
            "service_id": service_id,
            "fields": fields,
            "entity_id": entity_id,
            "required_fields": all_required,
            "optional_fields": entity_optional[:10],
            "auto_generated_fields": auto_generated,
            "missing_fields": missing,
            "confirmation_summary": summary,
            "needs_user_input": len(missing) > 0,
        }
    if not guard_result.allow:
        return {"error": f"Blocked: {guard_result.reason}", "blocked": True}

    summary = build_write_summary(operation=operation, entity_set=entity_set, fields=fields, service_id=service_id, missing_fields=missing)
    return {
        "preview": True,
        "operation": operation,
        "entity_set": entity_set,
        "service_id": service_id,
        "fields": fields,
        "entity_id": entity_id,
        "required_fields": all_required,
        "optional_fields": entity_optional[:10],
        "auto_generated_fields": auto_generated,
        "missing_fields": missing,
        "confirmation_summary": summary,
        "needs_user_input": len(missing) > 0,
    }


@router.post("/chat/write/execute")
async def write_execute(payload: ChatRequest, request: Request):
    """Execute a confirmed write operation."""
    from app.services.guardrails import run_input_guards, run_output_guards, log_write_operation
    from app.services.odata_client import ODataClient

    user = get_current_user(request)
    user_role = user.get("role", "user") if user else payload.user_role

    try:
        write_op = json.loads(payload.query) if payload.query.startswith("{") else {}
    except json.JSONDecodeError:
        return {"error": "Invalid write operation format"}

    if not write_op:
        return {"error": "No write operation provided"}

    operation = write_op.get("operation", "")
    entity_set = write_op.get("entity_set", "")
    fields = write_op.get("fields", {})
    entity_id = write_op.get("entity_id")
    service_id = write_op.get("service_id", "")

    if not service_id or not entity_set:
        return {"error": "service_id and entity_set required"}

    guard_result = run_input_guards(
        user_role=user_role,
        user_id=payload.session_id or "anonymous",
        entity_set=entity_set,
        operation=operation,
        fields=fields,
        required_fields=[],
        confirmed=True,
    )

    if not guard_result.allow:
        log_write_operation(operation=operation, entity_set=entity_set, service_id=service_id, user_role=user_role, user_id=payload.session_id or "anonymous", fields=fields, success=False, error=guard_result.reason)
        return {"error": f"Blocked: {guard_result.reason}"}

    def _unwrap_created_record(value):
        if isinstance(value, dict):
            if isinstance(value.get("d"), dict): return _unwrap_created_record(value["d"])
            if isinstance(value.get("value"), dict): return _unwrap_created_record(value["value"])
            if isinstance(value.get("value"), list) and value["value"]: return _unwrap_created_record(value["value"][0])
            if isinstance(value.get("results"), list) and value["results"]: return _unwrap_created_record(value["results"][0])
            return value
        if isinstance(value, list) and value: return _unwrap_created_record(value[0])
        return {}

    def _extract_record_id(record):
        if not isinstance(record, dict): return ""
        preferred_keys = [entity_set.replace("A_", "").replace("I_", ""), "PurchaseOrder", "OrderID", "ID", "Id", "id", "ObjectID", "UUID"]
        for key in preferred_keys:
            value = record.get(key)
            if value not in (None, ""): return str(value)
        for key, value in record.items():
            key_lower = key.lower()
            if value not in (None, "") and (key_lower.endswith("id") or key_lower.endswith("uuid")): return str(value)
        return ""

    def _build_write_table(record):
        if not isinstance(record, dict) or not record: return None
        flat_record = {key: value for key, value in record.items() if not key.startswith("__") and not isinstance(value, (dict, list))}
        if not flat_record: return None
        return {"columns": list(flat_record.keys()), "rows": [flat_record], "row_count": 1}

    try:
        svc = service_manager._services.get(service_id, {})
        if not svc:
            return {"error": f"Service '{service_id}' not found"}

        client = ODataClient(svc.get("base_url", ""), auth_type=svc.get("auth_type"), auth_config=svc.get("auth_config"))
        if operation == "create":
            result = await client.create(entity_set, fields)
        elif operation == "update":
            if not entity_id: return {"error": "entity_id required for update"}
            result = await client.update(entity_set, entity_id, fields)
        elif operation == "delete":
            if not entity_id: return {"error": "entity_id required for delete"}
            result = await client.delete(entity_set, entity_id)
        else:
            return {"error": f"Unknown operation: {operation}"}

        result = run_output_guards(result, operation)
        created_record = _unwrap_created_record(result)
        if operation == "create" and isinstance(created_record, dict):
            created_record = {**fields, **created_record}
        created_id = _extract_record_id(created_record)
        result_table = _build_write_table(created_record)
        summary = f"Successfully created {entity_set}. New ID: {created_id}" if (operation == "create" and created_id) else f"Successfully {operation}d entity in {entity_set}"

        log_write_operation(operation=operation, entity_set=entity_set, service_id=service_id, user_role=user_role, user_id=payload.session_id or "anonymous", fields=fields, success=True, entity_id=entity_id or created_id or "")
        return {"success": True, "operation": operation, "entity_set": entity_set, "service_id": service_id, "result": result, "created_id": created_id, "created_record": created_record, "table": result_table, "summary": summary}
    except Exception as e:
        logger.exception(f"Write execution failed: {e}")
        log_write_operation(operation=operation, entity_set=entity_set, service_id=service_id, user_role=user_role, user_id=payload.session_id or "anonymous", fields=fields, success=False, error=str(e))
        return {"error": f"Write failed: {str(e)}"}


# ---------------------------------------------------------------------------
# /share
# ---------------------------------------------------------------------------

@router.post("/share")
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
        share_text += "\n".join(" | ".join(str(r.get(c, "")) for c in cols) for r in rows)
        if len(table.get("rows", [])) > 20:
            share_text += f"\n... and {len(table['rows']) - 20} more rows"

    user_info = {
        "username": user.get("username", "unknown") if user else "anonymous",
        "email": user.get("email", "") if user else "",
        "role": user.get("role", "") if user else "",
    }
    share_payload = {
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
        return {"success": True, "channel": "clipboard", "share_text": share_text}

    webhook_urls = {
        "slack": settings.n8n_webhook_url,
        "email": settings.n8n_email_webhook_url,
        "whatsapp": settings.n8n_whatsapp_webhook_url,
        "teams": settings.teams_webhook_url,
    }
    webhook_url = webhook_urls.get(channel, settings.n8n_webhook_url)

    if channel == "teams" and settings.teams_webhook_url:
        from app.services.teams_webhook import send_to_teams_webhook
        sent = await send_to_teams_webhook(settings.teams_webhook_url, share_text, f"OData Share - {query[:50]}")
        return {"success": sent, "channel": "teams", "share_text": share_text}

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(webhook_url, json=share_payload)
            if resp.status_code >= 400:
                logger.warning(f"n8n returned {resp.status_code}: {resp.text[:200]}")
            return {"success": resp.status_code < 400, "channel": channel, "n8n_status": resp.status_code, "share_text": share_text}
    except httpx.ConnectError:
        raise HTTPException(status_code=502, detail="n8n webhook unreachable. Check n8n service is running.")
    except Exception as e:
        logger.error(f"Share failed: {e}")
        raise HTTPException(status_code=500, detail=f"Share failed: {str(e)}")
