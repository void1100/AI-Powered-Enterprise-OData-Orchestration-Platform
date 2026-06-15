"""Builds an OData URL from a structured plan.

The plan format is produced by the LLM Reasoning Engine:
{
  "entity_set": "Customers",
  "select": ["CustomerID", "Name"],
  "filter": "Country eq 'USA'",
  "expand": ["Orders"],
  "orderby": "Name asc",
  "top": 10,
  "skip": 0
}
"""
from typing import Any, Dict, Optional
import re
from loguru import logger

from app.services.odata_client import ODataClient


class ODataRequestBuilder:
    ALLOWED_OPS_DEFAULT = {"select", "filter", "expand", "orderby", "top", "skip", "count"}

    def __init__(self, client: ODataClient, allowed_ops: Optional[list] = None, custom_entities: Optional[Dict[str, Any]] = None):
        self.client = client
        self.allowed_ops = set(allowed_ops or self.ALLOWED_OPS_DEFAULT)
        self.custom_entities = custom_entities or {}

    def validate(self, plan: Dict[str, Any]) -> Dict[str, Any]:
        cleaned: Dict[str, Any] = {}
        entity_set = plan.get("entity_set")
        if not entity_set or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", entity_set):
            raise ValueError(f"Invalid entity_set: {entity_set}")
        cleaned["entity_set"] = entity_set

        if "select" in plan and "select" in self.allowed_ops:
            cleaned["select"] = [s for s in (plan["select"] or []) if re.match(r"^[A-Za-z_][A-Za-z0-9_/]*$", s)]
        if "filter" in plan and "filter" in self.allowed_ops:
            f = plan["filter"]
            if f and isinstance(f, str) and len(f) < 1000:
                cleaned["filter"] = f
        if "expand" in plan and "expand" in self.allowed_ops:
            cleaned["expand"] = [e for e in (plan["expand"] or []) if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", e)]
        if "orderby" in plan and "orderby" in self.allowed_ops:
            ob = plan["orderby"]
            if ob and re.match(r"^[A-Za-z_][A-Za-z0-9_, ]*(asc|desc)?$", str(ob), re.IGNORECASE):
                cleaned["orderby"] = ob
        if "top" in plan and "top" in self.allowed_ops:
            try:
                t = int(plan["top"])
                cleaned["top"] = max(0, min(t, 1000))
            except (TypeError, ValueError):
                pass
        if "skip" in plan and "skip" in self.allowed_ops:
            try:
                s = int(plan["skip"])
                cleaned["skip"] = max(0, s)
            except (TypeError, ValueError):
                pass
        return cleaned

    def _resolve_custom_entity(self, plan: Dict[str, Any]) -> Dict[str, Any]:
        entity_set = plan.get("entity_set", "")
        custom = self.custom_entities.get(entity_set)
        if not custom:
            return plan
        resolved = dict(plan)
        resolved["entity_set"] = custom["base_entity_set"]
        resolved["_custom_entity"] = entity_set
        merged_filter = custom.get("default_filter", "")
        user_filter = plan.get("filter", "")
        if merged_filter and user_filter:
            resolved["filter"] = f"({merged_filter}) and ({user_filter})"
        elif merged_filter:
            resolved["filter"] = merged_filter
        elif user_filter:
            resolved["filter"] = user_filter
        allowed = custom.get("allowed_columns", [])
        if allowed and plan.get("select"):
            resolved["select"] = [s for s in plan["select"] if s in allowed]
        elif allowed:
            resolved["select"] = allowed
        return resolved

    async def execute(self, plan: Dict[str, Any]) -> Dict[str, Any]:
        cleaned = self.validate(plan)
        resolved = self._resolve_custom_entity(cleaned)
        url = self.client._build_url(
            entity_set=resolved["entity_set"],
            select=resolved.get("select"),
            filter_expr=resolved.get("filter"),
            expand=resolved.get("expand"),
            top=resolved.get("top"),
            skip=resolved.get("skip"),
            orderby=resolved.get("orderby"),
        )
        result = await self.client.query(
            entity_set=resolved["entity_set"],
            select=resolved.get("select"),
            filter_expr=resolved.get("filter"),
            expand=resolved.get("expand"),
            top=resolved.get("top"),
            skip=resolved.get("skip"),
            orderby=resolved.get("orderby"),
        )
        return {"url": url, "result": result}
