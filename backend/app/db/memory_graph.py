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
        self.joins: Dict[str, Dict[str, Any]] = {}

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

    def upsert_entity_relationship(self, from_service: str, from_entity: str,
                                    to_service: str, to_entity: str,
                                    join_column: str, confidence: float = 0.9):
        with self._lock:
            key = (from_service, from_entity, to_service, to_entity)
            self.relationships.append({
                "from_service": from_service,
                "from_name": from_entity,
                "to_service": to_service,
                "to_name": to_entity,
                "join_column": join_column,
                "confidence": confidence,
                "rel_type": "CAN_JOIN_TO",
            })

    def get_entity_join_relationships(self, service_id: str, entity_name: str) -> List[Dict[str, Any]]:
        with self._lock:
            out = []
            for rel in self.relationships:
                if rel.get("rel_type") == "CAN_JOIN_TO":
                    if (rel.get("from_service") == service_id and
                        rel.get("from_name") == entity_name):
                        out.append({
                            "related_service": rel.get("to_service"),
                            "related_name": rel.get("to_name"),
                            "join_column": rel.get("join_column"),
                            "confidence": rel.get("confidence", 0.9),
                        })
            return out

    def find_entity_relationships(self, entity_name: str, service_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            out = []
            for rel in self.relationships:
                if (rel.get("from_service") == service_id and
                    rel.get("from_name") == entity_name):
                    out.append({
                        "related_service": rel.get("to_service"),
                        "related_name": rel.get("to_name"),
                        "rel_type": rel.get("rel_type", "RELATED_TO"),
                        "join_column": rel.get("join_column"),
                        "confidence": rel.get("confidence"),
                        "cardinality": rel.get("cardinality"),
                    })
                elif (rel.get("to_service") == service_id and
                      rel.get("to_name") == entity_name):
                    out.append({
                        "related_service": rel.get("from_service"),
                        "related_name": rel.get("from_name"),
                        "rel_type": rel.get("rel_type", "RELATED_TO"),
                        "join_column": rel.get("join_column"),
                        "confidence": rel.get("confidence"),
                        "cardinality": rel.get("cardinality"),
                    })
            return out

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

    def get_custom_entities(self) -> List[Dict[str, Any]]:
        with self._lock:
            out = []
            for (svc_id, ent_name), ent in self.entities.items():
                if ent.get("is_custom"):
                    out.append({
                        "service_id": svc_id,
                        "name": ent_name,
                        "base_entity_set": ent.get("base_entity_set", ""),
                        "description": ent.get("description", ""),
                        "default_filter": ent.get("default_filter", ""),
                        "allowed_columns": ent.get("allowed_columns", []),
                        "created_by": ent.get("created_by", ""),
                        "created_at": ent.get("created_at", ""),
                    })
            return out

    def get_service_entities(self, service_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            out = []
            for (svc_id, ent_name), ent in self.entities.items():
                if svc_id == service_id and not ent.get("is_custom"):
                    out.append({
                        "name": ent_name,
                        "type": ent.get("type", ent_name),
                        "properties": ent.get("properties", []),
                        "label": ent.get("label", ""),
                        "property_labels": ent.get("property_labels", {}),
                    })
            return out

    def delete_entity(self, service_id: str, name: str) -> bool:
        with self._lock:
            key = (service_id, name)
            if key in self.entities:
                del self.entities[key]
                return True
            return False

    def delete_service(self, service_id: str) -> bool:
        with self._lock:
            if service_id not in self.services:
                return False
            del self.services[service_id]
            keys_to_remove = [k for k in self.entities if k[0] == service_id]
            for k in keys_to_remove:
                del self.entities[k]
            self.relationships = [
                r for r in self.relationships
                if r.get("from_service") != service_id and r.get("to_service") != service_id
            ]
            self.joins = {
                jid: j for jid, j in self.joins.items()
                if j.get("left_service") != service_id and j.get("right_service") != service_id
            }
            return True

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

    def upsert_join(self, join_def: Dict[str, Any]):
        with self._lock:
            self.joins[join_def["id"]] = join_def

    def list_joins(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self.joins.values())

    def get_join(self, join_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self.joins.get(join_id)

    def delete_join(self, join_id: str) -> bool:
        with self._lock:
            if join_id in self.joins:
                del self.joins[join_id]
                return True
            return False

    def clear(self):
        with self._lock:
            self.services.clear()
            self.entities.clear()
            self.relationships.clear()
            self.roles.clear()
            self.joins.clear()


_memory_graph: Optional[InMemoryGraph] = None


def get_memory_graph() -> InMemoryGraph:
    global _memory_graph
    if _memory_graph is None:
        _memory_graph = InMemoryGraph()
    return _memory_graph
