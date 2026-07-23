"""Service Discovery Agent.

Uses the vector store for semantic search across entity set descriptions,
and the graph database for relationship traversal. Returns a list of
candidate tools (entity sets) ranked by relevance.
"""
from typing import Any, Dict, List, Optional

from app.db.vector_store import vector_store
from app.db.neo4j_client import neo4j_client
from app.db.memory_graph import get_memory_graph
from app.services.service_manager import service_manager


class ServiceDiscoveryAgent:
    def __init__(self, top_k: int = 8):
        self.top_k = top_k

    def graph(self):
        return neo4j_client if neo4j_client.is_available() else get_memory_graph()

    async def discover(self, query: str) -> Dict[str, Any]:
        semantic_hits = vector_store.search_tools(query, top_k=self.top_k)
        candidates: List[Dict[str, Any]] = []
        seen = set()
        for hit in semantic_hits:
            meta = hit.get("metadata", {}) or {}
            sid = meta.get("service_id")
            es = meta.get("entity_set")
            key = (sid, es)
            if not sid or not es or key in seen:
                continue
            seen.add(key)
            rels = self.graph().find_related_entities(sid, es) if sid and es else []
            candidates.append({
                "service_id": sid,
                "service_name": meta.get("service_name", sid),
                "entity_set": es,
                "entity_type": meta.get("entity_type"),
                "properties": meta.get("properties", []),
                "score": float(hit.get("score", 0.0)),
                "relationships": rels,
            })
        if not candidates:
            for svc in service_manager.list_services():
                healthy = svc.get("healthy_entity_sets")
                for es in svc.get("entity_sets", []):
                    if healthy is not None and es not in healthy:
                        continue
                    rels = self.graph().find_related_entities(svc["id"], es)
                    candidates.append({
                        "service_id": svc["id"],
                        "service_name": svc["name"],
                        "entity_set": es,
                        "entity_type": es,
                        "properties": [],
                        "score": 0.5,
                        "relationships": rels,
                    })
        return {"query": query, "candidates": candidates}


discovery_agent = ServiceDiscoveryAgent()
