"""In-memory fallback graph store used when Neo4j is not available.
This keeps the same API surface as Neo4jClient for the parts the orchestration needs.
"""
from typing import Optional, List, Dict, Any
from threading import RLock
from loguru import logger


class InMemoryGraph:
    def __init__(self):
        self._lock = RLock()
        self.services: Dict[str, Dict[str, Any]] = {}
        self.entities: Dict[tuple, Dict[str, Any]] = {}
        self.relationships: List[Dict[str, Any]] = []
        self.roles: Dict[str, Dict[str, Any]] = {}

    def is_available(self) -> bool:
        return True

    def upsert_service(self, service: Dict[str, Any]):
        with self._lock:
            self.services[service["id"]] = service

    def upsert_entity(self, entity: Dict[str, Any]):
        with self._lock:
            key = (entity["service_id"], entity["name"])
            self.entities[key] = entity

    def upsert_relationship(self, rel: Dict[str, Any]):
        with self._lock:
            self.relationships.append(rel)

    def upsert_role_policy(self, role: Dict[str, Any]):
        with self._lock:
            self.roles[role["id"]] = role

    def find_services_for_entities(self, entity_names: List[str]) -> List[Dict[str, Any]]:
        with self._lock:
            out = []
            for (svc_id, ent_name), ent in self.entities.items():
                for n in entity_names:
                    if n.lower() in ent_name.lower() or ent_name.lower() in n.lower():
                        svc = self.services.get(svc_id, {})
                        out.append({
                            "service_id": svc_id,
                            "name": svc.get("name", svc_id),
                            "base_url": svc.get("base_url", ""),
                            "description": svc.get("description", ""),
                            "entities": [ent_name],
                        })
                        break
            seen = set()
            uniq = []
            for r in out:
                key = r["service_id"]
                if key not in seen:
                    seen.add(key)
                    uniq.append(r)
            return uniq

    def find_related_entities(self, service_id: str, entity_name: str) -> List[Dict[str, Any]]:
        with self._lock:
            out = []
            for rel in self.relationships:
                if rel["from_service"] == service_id and rel["from_name"] == entity_name:
                    out.append({
                        "to_service": rel["to_service"],
                        "to_name": rel["to_name"],
                        "rel_type": rel.get("rel_type", "ASSOCIATED_WITH"),
                        "cardinality": rel.get("cardinality", "many_to_one"),
                        "description": rel.get("description", ""),
                    })
                elif rel["to_service"] == service_id and rel["to_name"] == entity_name:
                    out.append({
                        "to_service": rel["from_service"],
                        "to_name": rel["from_name"],
                        "rel_type": rel.get("rel_type", "ASSOCIATED_WITH"),
                        "cardinality": rel.get("cardinality", "many_to_one"),
                        "description": rel.get("description", ""),
                    })
            return out

    def get_role_policy(self, role_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self.roles.get(role_id)

    def get_entity_metadata(self, service_id: str, entity_name: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self.entities.get((service_id, entity_name))

    def list_all_services(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self.services.values())

    def list_all_entities(self) -> List[Dict[str, Any]]:
        with self._lock:
            out = []
            for (svc_id, ent_name), ent in self.entities.items():
                svc = self.services.get(svc_id, {})
                out.append({
                    "service_id": svc_id,
                    "service_name": svc.get("name", svc_id),
                    "entity_name": ent_name,
                    "type": ent.get("type", ""),
                    "description": ent.get("description", ""),
                    "allowed_ops": ent.get("allowed_ops", []),
                    "properties": ent.get("properties", []),
                })
            return out

    def clear(self):
        with self._lock:
            self.services.clear()
            self.entities.clear()
            self.relationships.clear()
            self.roles.clear()


_memory_graph: Optional[InMemoryGraph] = None


def get_memory_graph() -> InMemoryGraph:
    global _memory_graph
    if _memory_graph is None:
        _memory_graph = InMemoryGraph()
    return _memory_graph
