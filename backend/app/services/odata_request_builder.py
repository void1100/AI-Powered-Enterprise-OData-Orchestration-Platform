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

    def __init__(self, client: ODataClient, allowed_ops: Optional[list] = None):
        self.client = client
        self.allowed_ops = set(allowed_ops or self.ALLOWED_OPS_DEFAULT)

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

    async def execute(self, plan: Dict[str, Any]) -> Dict[str, Any]:
        cleaned = self.validate(plan)
        url = self.client._build_url(
            entity_set=cleaned["entity_set"],
            select=cleaned.get("select"),
            filter_expr=cleaned.get("filter"),
            expand=cleaned.get("expand"),
            top=cleaned.get("top"),
            skip=cleaned.get("skip"),
            orderby=cleaned.get("orderby"),
        )
        result = await self.client.query(
            entity_set=cleaned["entity_set"],
            select=cleaned.get("select"),
            filter_expr=cleaned.get("filter"),
            expand=cleaned.get("expand"),
            top=cleaned.get("top"),
            skip=cleaned.get("skip"),
            orderby=cleaned.get("orderby"),
        )
        return {"url": url, "result": result}
