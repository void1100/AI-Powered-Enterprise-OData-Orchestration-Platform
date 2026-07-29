"""Entities router — entity selector, auto-join detection, join execution, and field requirements."""
import asyncio
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException
from loguru import logger

from app.schemas.models import AutoJoinRequest, EntityJoinExecuteRequest, TableData
from app.services.service_manager import service_manager
from app.services.entity_selector import entity_selector, classify_property
from app.services.column_filter import filter_columns
from app.services.cross_service_join import match_join
from app.routers.deps import build_column_labels

router = APIRouter(tags=["entities"])


@router.get("/entities/{service_id}")
async def get_service_entities(service_id: str):
    """Get entity list with properties for a specific service."""
    services = service_manager.list_services()
    svc = next((s for s in services if s["id"] == service_id), None)
    if not svc:
        raise HTTPException(status_code=404, detail=f"Service '{service_id}' not found")
    entities = []
    entity_labels = svc.get("entity_labels", {})
    for es_name in svc.get("entity_sets", []):
        props = svc.get("entity_properties", {}).get(es_name, [])
        labels_info = entity_labels.get(es_name, {})
        entity_label = labels_info.get("entity_label", "")
        prop_labels = labels_info.get("property_labels", {})
        labeled_props = []
        for p in props:
            if isinstance(p, str):
                sap_label = prop_labels.get(p, "")
                labeled_props.append({
                    "name": p,
                    "label": classify_property(p),
                    "display_label": sap_label or p,
                })
            elif isinstance(p, dict):
                sap_label = p.get("label", "") or prop_labels.get(p.get("name", ""), "")
                name = p.get("name", "")
                labeled_props.append({
                    **p,
                    "label": classify_property(name),
                    "display_label": sap_label or name,
                })
            else:
                labeled_props.append({"name": str(p), "label": "Attribute"})
        entities.append({
            "name": es_name,
            "label": entity_label,
            "properties": labeled_props,
            "property_count": len(labeled_props),
        })
    return {"service_id": service_id, "service_name": svc["name"], "entities": entities}


@router.post("/entities/auto-join")
async def detect_auto_joins(payload: AutoJoinRequest):
    """Detect potential joins between selected entities."""
    entities = []
    services = service_manager.list_services()
    for e in payload.entities:
        svc = next((s for s in services if s["id"] == e.service_id), None)
        if svc:
            props = svc.get("entity_properties", {}).get(e.entity_name, [])
            entities.append({
                "service_id": e.service_id,
                "entity_name": e.entity_name,
                "properties": props,
            })
    if len(entities) < 2:
        return {"joins": [], "message": "Select at least 2 entities to detect joins"}
    joins = entity_selector.detect_joins(entities)
    return {"joins": joins, "entity_count": len(entities)}


@router.post("/entities/execute-join")
async def execute_entity_join(payload: EntityJoinExecuteRequest):
    """Execute a query with selected entities and auto-detected joins."""
    services = service_manager.list_services()
    all_results: List[Dict[str, Any]] = []

    for e in payload.entities:
        svc = next((s for s in services if s["id"] == e.service_id), None)
        if not svc:
            logger.warning(f"execute-join: service {e.service_id} not found")
            continue
        client = service_manager._clients.get(e.service_id)
        if not client:
            logger.warning(f"execute-join: no client for {e.service_id}")
            continue
        try:
            top = min(payload.top, 200)
            resp = await client.query(entity_set=e.entity_name, top=top)
            rows = client.flatten_odata_value(resp)
            if rows:
                cols = list(rows[0].keys())
                all_results.append({
                    "service_id": e.service_id,
                    "entity_name": e.entity_name,
                    "table": {"columns": cols, "rows": rows},
                })
                logger.info(f"execute-join: fetched {len(rows)} rows from {e.entity_name}")
            else:
                logger.warning(f"execute-join: no rows from {e.entity_name}")
        except Exception as ex:
            logger.warning(f"execute-join: failed to fetch {e.entity_name} from {e.service_id}: {ex}")

    if not all_results:
        return {
            "error": "No data retrieved from selected entities",
            "table": TableData().model_dump(),
            "entity_count": 0,
            "join_count": 0,
        }

    joins = payload.joins or []
    if len(all_results) >= 2 and joins:
        sorted_joins = sorted(
            joins,
            key=lambda j: getattr(j, "confidence", 0) if hasattr(j, "confidence") else (
                j.get("confidence", 0) if isinstance(j, dict) else 0
            ),
            reverse=True,
        )
        result_table = all_results[0]["table"]
        used_right_entities: set = set()
        for join_def in sorted_joins:
            left_key = join_def.left_key if hasattr(join_def, "left_key") else join_def.get("left_key", "") if isinstance(join_def, dict) else ""
            right_key = join_def.right_key if hasattr(join_def, "right_key") else join_def.get("right_key", "") if isinstance(join_def, dict) else ""
            right_entity = join_def.right_entity if hasattr(join_def, "right_entity") else join_def.get("right_entity", "") if isinstance(join_def, dict) else ""
            if not left_key or not right_key:
                continue
            right_result = None
            for r in all_results:
                if r["entity_name"] == right_entity and r["entity_name"] not in used_right_entities:
                    right_result = r["table"]
                    used_right_entities.add(r["entity_name"])
                    break
            if not right_result:
                continue
            result_table = match_join(
                result_table,
                right_result,
                left_key=left_key,
                right_key=right_key,
                left_service=all_results[0]["service_id"],
                right_service=right_entity,
            )
        columns = result_table.get("columns", [])
        rows = result_table.get("rows", [])
    elif len(all_results) == 1:
        columns = all_results[0]["table"]["columns"]
        rows = all_results[0]["table"]["rows"]
    else:
        all_cols: List[str] = []
        for r in all_results:
            for c in r["table"]["columns"]:
                if c not in all_cols:
                    all_cols.append(c)
        columns = all_cols
        rows = []
        for r in all_results:
            for row in r["table"]["rows"]:
                merged = {c: row.get(c) for c in all_cols}
                merged["_source_service"] = r["service_id"]
                rows.append(merged)

    entity_service_lookup = {e.entity_name: e.service_id for e in payload.entities}
    for j in joins:
        le = j.left_entity if hasattr(j, "left_entity") else j.get("left_entity", "") if isinstance(j, dict) else ""
        re_ = j.right_entity if hasattr(j, "right_entity") else j.get("right_entity", "") if isinstance(j, dict) else ""
        lk = j.left_key if hasattr(j, "left_key") else j.get("left_key", "") if isinstance(j, dict) else ""
        rk = j.right_key if hasattr(j, "right_key") else j.get("right_key", "") if isinstance(j, dict) else ""
        left_service = j.left_service if hasattr(j, "left_service") else j.get("left_service", "") if isinstance(j, dict) else ""
        right_service = j.right_service if hasattr(j, "right_service") else j.get("right_service", "") if isinstance(j, dict) else ""
        entity_selector.store_successful_join(
            left_service or entity_service_lookup.get(le, ""),
            le,
            right_service or entity_service_lookup.get(re_, ""),
            re_,
            lk,
            rk,
        )

    MAX_JOIN_ROWS = 100
    if len(rows) > MAX_JOIN_ROWS:
        rows = rows[:MAX_JOIN_ROWS]

    filtered = filter_columns({"columns": columns, "rows": rows, "row_count": len(rows)})
    columns, rows = filtered["columns"], filtered["rows"]

    table = TableData(columns=columns, rows=rows, row_count=len(rows))
    return {
        "success": True,
        "table": table.model_dump(),
        "entity_count": len(payload.entities),
        "join_count": len(joins),
    }


@router.get("/entities/{service_id}/{entity_set}/fields")
async def get_entity_fields(service_id: str, entity_set: str):
    """Get field requirements for an entity (required, optional, auto-generated)."""
    from app.services.guardrails import get_entity_field_requirements
    return get_entity_field_requirements(service_id, entity_set)
