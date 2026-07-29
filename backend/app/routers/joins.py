"""Joins router — cross-service join definitions, execution, and chat against joined data."""
import re as _re
import uuid as _uuid
import datetime

from typing import Any, Dict
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from loguru import logger

from app.auth import get_current_user
from app.agents.reasoning_engine import llm_engine
from app.services.service_manager import service_manager
from app.services.cross_service_join import union_join, match_join, enrichment_join

router = APIRouter(prefix="/joins", tags=["joins"])


def _require_admin(request: Request):
    user = get_current_user(request)
    if not user or user.get("role") not in ("super_admin", "admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


class JoinCreate(BaseModel):
    name: str
    strategy: str  # union, match, enrichment
    left_service: str
    left_entity: str
    left_key: str = ""
    right_service: str
    right_entity: str
    right_key: str = ""
    column_mapping: Dict[str, Dict[str, str]] = {}
    description: str = ""


async def _fetch_and_build_tables(join_def: Dict[str, Any]):
    """Shared helper: fetch both sides of a join and return (left_data, right_data, left_cols, right_cols)."""
    left_client = service_manager.get_client(join_def["left_service"])
    right_client = service_manager.get_client(join_def["right_service"])
    if not left_client or not right_client:
        raise HTTPException(status_code=400, detail="Service client not available")
    left_table = await left_client.query(entity_set=join_def["left_entity"], top=200)
    right_table = await right_client.query(entity_set=join_def["right_entity"], top=200)
    left_data = left_client.flatten_odata_value(left_table)
    right_data = right_client.flatten_odata_value(right_table)
    left_cols = list(left_data[0].keys()) if left_data else []
    right_cols = list(right_data[0].keys()) if right_data else []
    return left_data, right_data, left_cols, right_cols


def _execute_strategy(strategy: str, join_def: Dict[str, Any], left_cols, left_data, right_cols, right_data):
    """Apply the correct join strategy and return the result dict."""
    if strategy == "union":
        return union_join(
            [
                {"service_id": join_def["left_service"], "table": {"columns": left_cols, "rows": left_data}},
                {"service_id": join_def["right_service"], "table": {"columns": right_cols, "rows": right_data}},
            ],
            column_mapping=join_def.get("column_mapping"),
        )
    if strategy == "match":
        return match_join(
            {"columns": left_cols, "rows": left_data},
            {"columns": right_cols, "rows": right_data},
            left_key=join_def["left_key"],
            right_key=join_def["right_key"],
            left_service=join_def["left_service"],
            right_service=join_def["right_service"],
        )
    if strategy == "enrichment":
        return enrichment_join(
            {"columns": left_cols, "rows": left_data},
            {"columns": right_cols, "rows": right_data},
            primary_key=join_def["left_key"],
            secondary_key=join_def["right_key"],
            primary_service=join_def["left_service"],
            secondary_service=join_def["right_service"],
        )
    raise HTTPException(status_code=400, detail=f"Unknown strategy: {strategy}")


@router.get("")
async def list_joins(request: Request):
    _require_admin(request)
    g = service_manager.graph()
    return g.list_joins()


@router.post("")
async def create_join(payload: JoinCreate, request: Request):
    user = _require_admin(request)
    if payload.left_service not in service_manager._services:
        raise HTTPException(status_code=400, detail=f"Unknown left service: {payload.left_service}")
    if payload.right_service not in service_manager._services:
        raise HTTPException(status_code=400, detail=f"Unknown right service: {payload.right_service}")
    join_id = str(_uuid.uuid4())[:8]
    join_def = {
        "id": join_id,
        "name": payload.name,
        "strategy": payload.strategy,
        "left_service": payload.left_service,
        "left_entity": payload.left_entity,
        "left_key": payload.left_key,
        "right_service": payload.right_service,
        "right_entity": payload.right_entity,
        "right_key": payload.right_key,
        "column_mapping": payload.column_mapping,
        "description": payload.description,
        "created_by": user.get("username", "admin"),
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    g = service_manager.graph()
    g.upsert_join(join_def)
    return join_def


@router.patch("/{join_id}")
async def update_join(join_id: str, payload: JoinCreate, request: Request):
    _require_admin(request)
    g = service_manager.graph()
    existing = g.get_join(join_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Join not found")
    updated = {
        **existing,
        "name": payload.name,
        "strategy": payload.strategy,
        "left_service": payload.left_service,
        "left_entity": payload.left_entity,
        "left_key": payload.left_key,
        "right_service": payload.right_service,
        "right_entity": payload.right_entity,
        "right_key": payload.right_key,
        "column_mapping": payload.column_mapping,
        "description": payload.description,
    }
    g.upsert_join(updated)
    return updated


@router.delete("/{join_id}")
async def delete_join(join_id: str, request: Request):
    _require_admin(request)
    g = service_manager.graph()
    if g.delete_join(join_id):
        return {"deleted": join_id}
    raise HTTPException(status_code=404, detail="Join not found")


@router.post("/{join_id}/execute")
async def execute_join(join_id: str, request: Request):
    _require_admin(request)
    g = service_manager.graph()
    join_def = g.get_join(join_id)
    if not join_def:
        raise HTTPException(status_code=404, detail="Join not found")
    try:
        left_data, right_data, left_cols, right_cols = await _fetch_and_build_tables(join_def)
        result = _execute_strategy(join_def["strategy"], join_def, left_cols, left_data, right_cols, right_data)
        return {"join": join_def, "result": result}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Join execution failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{join_id}/chat")
async def join_chat(join_id: str, request: Request):
    _require_admin(request)
    body = await request.json()
    query = body.get("query", "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="Query is required")

    g = service_manager.graph()
    join_def = g.get_join(join_id)
    if not join_def:
        raise HTTPException(status_code=404, detail="Join not found")

    left_data, right_data, left_cols, right_cols = await _fetch_and_build_tables(join_def)
    strategy = join_def["strategy"]
    result = _execute_strategy(strategy, join_def, left_cols, left_data, right_cols, right_data)

    rows = result.get("rows", [])
    cols = result.get("columns", [])
    important_cols = [
        c for c in cols
        if not c.startswith("@odata") and c not in ("Emails", "AddressInfo", "Concurrency", "Photo", "Notes", "PhotoPath")
    ]

    filter_match = _re.search(
        r'(?:where|whose|filter|with)\s+(\w+)\s*(>|<|>=|<=|!=|=|==)\s*([\d.]+)',
        query, _re.IGNORECASE,
    )
    filtered_rows = rows
    filter_info = None
    if filter_match:
        col_name = filter_match.group(1)
        op = filter_match.group(2)
        val = float(filter_match.group(3))
        matched_col = next((c for c in important_cols if c.lower() == col_name.lower()), None)
        if matched_col:
            def _check(row):
                rv = row.get(matched_col)
                if rv is None:
                    return False
                try:
                    rv = float(rv)
                except (ValueError, TypeError):
                    return False
                if op == ">": return rv > val
                if op == "<": return rv < val
                if op == ">=": return rv >= val
                if op == "<=": return rv <= val
                if op in ("!=", "<>"): return rv != val
                return rv == val
            filtered_rows = [r for r in rows if _check(r)]
            filter_info = f"{matched_col} {op} {val}"

    agg_match = _re.search(
        r'(sum|total|average|avg|min|minimum|max|maximum|count)\s+(?:of\s+)?(\w+)',
        query, _re.IGNORECASE,
    )
    if agg_match:
        agg_func = agg_match.group(1).lower()
        agg_col_name = agg_match.group(2)
        matched_agg_col = next((c for c in important_cols if c.lower() == agg_col_name.lower()), None)
        if matched_agg_col:
            nums = []
            for r in filtered_rows:
                v = r.get(matched_agg_col)
                if v is not None:
                    try:
                        nums.append(float(v))
                    except (ValueError, TypeError):
                        pass
            if nums:
                filter_suffix = f" where {filter_info}" if filter_info else ""
                if agg_func in ("sum", "total"):
                    answer = f"Sum of {matched_agg_col}{filter_suffix}: {round(sum(nums), 2)}"
                elif agg_func in ("average", "avg"):
                    answer = f"Average of {matched_agg_col}{filter_suffix}: {round(sum(nums)/len(nums), 2)} (from {len(nums)} values)"
                elif agg_func in ("min", "minimum"):
                    answer = f"Minimum of {matched_agg_col}{filter_suffix}: {min(nums)}"
                elif agg_func in ("max", "maximum"):
                    answer = f"Maximum of {matched_agg_col}{filter_suffix}: {max(nums)}"
                elif agg_func == "count":
                    answer = f"Count of {matched_agg_col}{filter_suffix}: {len(nums)}"
                else:
                    answer = f"Could not compute {agg_func} for {matched_agg_col}"
                return {
                    "answer": answer,
                    "provider": "computed",
                    "join_name": join_def["name"],
                    "row_count": len(rows),
                }

    sample_rows = filtered_rows[:50]
    data_summary = " | ".join(important_cols) + "\n"
    data_summary += "\n".join(" | ".join(str(r.get(c, ""))[:30] for c in important_cols) for r in sample_rows)
    if len(filtered_rows) > 50:
        data_summary += f"\n... ({len(filtered_rows)} total rows)"

    system_prompt = (
        "You are a data analyst. Answer questions about this cross-service join result.\n"
        f"Join: {join_def['name']} ({strategy})\n"
        f"Left: {join_def['left_service']}.{join_def['left_entity']}\n"
        f"Right: {join_def['right_service']}.{join_def['right_entity']}\n"
        f"Columns: {', '.join(important_cols)}\n"
        f"Total rows: {len(rows)}\n"
        + (f"Filter applied: {filter_info} → {len(filtered_rows)} matching rows\n" if filter_info else "")
        + "Data sample:\n" + data_summary + "\n\nBe concise. Answer based on this data."
    )

    try:
        response = await llm_engine.generate(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": query},
            ],
            temperature=0.3,
            max_tokens=1000,
        )
        answer = response.get("content", "No response from LLM.")
        provider = response.get("provider", "unknown")
        wants_table = bool(_re.search(
            r'show|list|display|details|all rows|records|entries|table|export|csv',
            query, _re.IGNORECASE,
        ))
        is_count = bool(_re.search(
            r'^(?:how many|count|total|what is the number|number of)',
            query, _re.IGNORECASE,
        ))
        resp: Dict[str, Any] = {
            "answer": answer,
            "provider": provider,
            "join_name": join_def["name"],
            "row_count": len(rows),
        }
        if filter_info and filtered_rows and wants_table and not is_count:
            resp["table"] = {
                "columns": important_cols,
                "rows": filtered_rows[:200],
                "row_count": len(filtered_rows),
                "truncated": len(filtered_rows) > 200,
                "total_count": len(filtered_rows),
            }
            resp["summary"] = f"Filtered by {filter_info}: {len(filtered_rows)} rows matching"
        return resp
    except Exception as e:
        logger.error(f"Join chat failed: {e}")
        raise HTTPException(status_code=500, detail=f"LLM Error: {str(e)}")
