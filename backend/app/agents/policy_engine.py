"""Authorization & Policy Engine.

Simple role-based policy. Roles are stored in the graph database.
Each role has allowed services, allowed entity sets, and allowed operations.
"""
from typing import Any, Dict, List, Optional

from app.db.neo4j_client import neo4j_client
from app.db.memory_graph import get_memory_graph


ROLE_PRESETS: Dict[str, Dict[str, Any]] = {
    "Admin": {
        "id": "Admin",
        "name": "Admin",
        "allowed_ops": ["select", "filter", "expand", "orderby", "top", "skip", "create", "update", "delete"],
        "allowed_entities": [],
        "allowed_services": [],
    },
    "Sales": {
        "id": "Sales",
        "name": "Sales",
        "allowed_ops": ["select", "filter", "expand", "orderby", "top", "skip", "create", "update"],
        "allowed_entities": ["Customers", "Orders", "Products"],
        "allowed_services": [],
    },
    "Analyst": {
        "id": "Analyst",
        "name": "Analyst",
        "allowed_ops": ["select", "filter", "expand", "orderby", "top", "skip", "aggregate"],
        "allowed_entities": [],
        "allowed_services": [],
    },
    "Viewer": {
        "id": "Viewer",
        "name": "Viewer",
        "allowed_ops": ["select", "filter", "top"],
        "allowed_entities": [],
        "allowed_services": [],
    },
}


class PolicyEngine:
    def graph(self):
        return neo4j_client if neo4j_client.is_available() else get_memory_graph()

    def ensure_default_roles(self):
        for role in ROLE_PRESETS.values():
            self.graph().upsert_role_policy(role)

    def get_role(self, role_id: str) -> Dict[str, Any]:
        role = self.graph().get_role_policy(role_id)
        if role:
            return role
        return ROLE_PRESETS.get(role_id, ROLE_PRESETS["Admin"])

    def list_roles(self) -> List[Dict[str, Any]]:
        return list(ROLE_PRESETS.values())

    def can_execute(self, role_id: str, service_id: str, entity_set: str, plan: Dict[str, Any]) -> Dict[str, Any]:
        role = self.get_role(role_id)
        if role.get("allowed_services") and service_id not in role["allowed_services"]:
            return {"allowed": False, "reason": f"Role '{role_id}' is not permitted to access service '{service_id}'."}
        if role.get("allowed_entities") and entity_set not in role["allowed_entities"]:
            return {"allowed": False, "reason": f"Role '{role_id}' is not permitted to access entity set '{entity_set}'."}
        allowed_ops = set(role.get("allowed_ops") or [])
        for k in plan.keys():
            if k in {"service_id", "entity_set"}:
                continue
            if k not in allowed_ops and plan.get(k) not in (None, [], ""):
                return {"allowed": False, "reason": f"Role '{role_id}' is not permitted to use the '{k}' operation."}
        return {"allowed": True, "reason": "OK"}


policy_engine = PolicyEngine()
