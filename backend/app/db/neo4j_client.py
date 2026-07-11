from neo4j import GraphDatabase, Driver
from typing import Optional, List, Dict, Any
from loguru import logger

from app.config import settings


class Neo4jClient:
    def __init__(self):
        self._driver: Optional[Driver] = None
        self._connect()

    def _connect(self, retries=3, delay=5):
        for attempt in range(retries):
            try:
                self._driver = GraphDatabase.driver(
                    settings.neo4j_uri,
                    auth=(settings.neo4j_user, settings.neo4j_password),
                )
                self._driver.verify_connectivity()
                logger.info(f"Connected to Neo4j at {settings.neo4j_uri}")
                self._init_schema()
                return
            except Exception as e:
                if attempt < retries - 1:
                    logger.warning(f"Neo4j attempt {attempt+1}/{retries} failed: {e}. Retrying in {delay}s...")
                    import time; time.sleep(delay)
                else:
                    logger.warning(f"Neo4j unavailable after {retries} attempts: {e}. Falling back to in-memory graph.")
                    self._driver = None

    def _init_schema(self):
        if not self._driver:
            return
        constraints = [
            "CREATE CONSTRAINT service_id IF NOT EXISTS FOR (s:Service) REQUIRE s.id IS UNIQUE",
            "CREATE CONSTRAINT role_id IF NOT EXISTS FOR (r:Role) REQUIRE r.id IS UNIQUE",
        ]
        indexes = [
            "CREATE INDEX service_name IF NOT EXISTS FOR (s:Service) ON (s.name)",
            "CREATE INDEX entity_name_idx IF NOT EXISTS FOR (e:Entity) ON (e.name)",
            "CREATE INDEX entity_service_idx IF NOT EXISTS FOR (e:Entity) ON (e.service)",
        ]
        with self._driver.session() as session:
            for stmt in constraints + indexes:
                try:
                    session.run(stmt)
                except Exception as e:
                    logger.debug(f"Schema stmt skipped: {e}")

    @property
    def driver(self) -> Optional[Driver]:
        return self._driver

    def is_available(self) -> bool:
        return self._driver is not None

    def close(self):
        if self._driver:
            self._driver.close()

    def upsert_service(self, service: Dict[str, Any]):
        if not self._driver:
            return
        with self._driver.session() as session:
            session.run(
                """
                MERGE (s:Service {id: $id})
                SET s.name = $name,
                    s.base_url = $base_url,
                    s.description = $description,
                    s.metadata = $metadata,
                    s.auth_type = $auth_type,
                    s.auth_config = $auth_config
                """,
                id=service["id"],
                name=service["name"],
                base_url=service["base_url"],
                description=service.get("description", ""),
                metadata=str(service.get("metadata", {})),
                auth_type=service.get("auth_type"),
                auth_config=str(service.get("auth_config")) if service.get("auth_config") else None,
            )

    def upsert_entity(self, entity: Dict[str, Any]):
        if not self._driver:
            return
        with self._driver.session() as session:
            session.run(
                """
                MATCH (s:Service {id: $service_id})
                MERGE (e:Entity {service: $service_id, name: $name})
                SET e.type = $type,
                    e.description = $description,
                    e.allowed_ops = $allowed_ops,
                    e.properties = $properties,
                    e.label = $label,
                    e.property_labels = $property_labels,
                    e.is_custom = $is_custom,
                    e.base_entity_set = $base_entity_set,
                    e.default_filter = $default_filter,
                    e.allowed_columns = $allowed_columns,
                    e.created_by = $created_by,
                    e.created_at = $created_at
                MERGE (s)-[:HAS_ENTITY]->(e)
                """,
                service_id=entity["service_id"],
                name=entity["name"],
                type=entity.get("type", ""),
                description=entity.get("description", ""),
                allowed_ops=entity.get("allowed_ops", []),
                properties=entity.get("properties", []),
                label=entity.get("label", ""),
                property_labels=__import__("json").dumps(entity.get("property_labels", {})),
                is_custom=entity.get("is_custom", False),
                base_entity_set=entity.get("base_entity_set", ""),
                default_filter=entity.get("default_filter", ""),
                allowed_columns=entity.get("allowed_columns", []),
                created_by=entity.get("created_by", ""),
                created_at=entity.get("created_at", ""),
            )

    def upsert_relationship(self, rel: Dict[str, Any]):
        if not self._driver:
            return
        with self._driver.session() as session:
            session.run(
                """
                MATCH (a:Entity {service: $from_service, name: $from_name})
                MATCH (b:Entity {service: $to_service, name: $to_name})
                MERGE (a)-[r:RELATED_TO {type: $rel_type}]->(b)
                SET r.cardinality = $cardinality,
                    r.description = $description
                """,
                from_service=rel["from_service"],
                from_name=rel["from_name"],
                to_service=rel["to_service"],
                to_name=rel["to_name"],
                rel_type=rel.get("rel_type", "ASSOCIATED_WITH"),
                cardinality=rel.get("cardinality", "many_to_one"),
                description=rel.get("description", ""),
            )

    def upsert_entity_relationship(self, from_service: str, from_entity: str,
                                    to_service: str, to_entity: str,
                                    join_column: str, confidence: float = 0.9):
        """Store a detected join relationship between two entities."""
        if not self._driver:
            return
        with self._driver.session() as session:
            session.run(
                """
                MATCH (a:Entity {service: $from_service, name: $from_entity})
                MATCH (b:Entity {service: $to_service, name: $to_entity})
                MERGE (a)-[r:CAN_JOIN_TO]->(b)
                SET r.join_column = $join_column,
                    r.confidence = $confidence
                """,
                from_service=from_service,
                from_entity=from_entity,
                to_service=to_service,
                to_entity=to_entity,
                join_column=join_column,
                confidence=confidence,
            )

    def get_entity_join_relationships(self, service_id: str, entity_name: str) -> List[Dict[str, Any]]:
        """Get all CAN_JOIN_TO relationships for an entity."""
        if not self._driver:
            return []
        with self._driver.session() as session:
            result = session.run(
                """
                MATCH (e:Entity {service: $service_id, name: $entity_name})-[r:CAN_JOIN_TO]->(other:Entity)
                RETURN other.service AS related_service,
                       other.name AS related_name,
                       r.join_column AS join_column,
                       r.confidence AS confidence
                """,
                service_id=service_id,
                entity_name=entity_name,
            )
            rows = []
            for r in result:
                d = dict(r)
                pl = d.get("property_labels")
                if isinstance(pl, str):
                    try:
                        import json as _json
                        d["property_labels"] = _json.loads(pl)
                    except Exception:
                        d["property_labels"] = {}
                rows.append(d)
            return rows

    def find_entity_relationships(self, entity_name: str, service_id: str) -> List[Dict[str, Any]]:
        """Find all relationships for an entity (both directions)."""
        if not self._driver:
            return []
        with self._driver.session() as session:
            result = session.run(
                """
                MATCH (e:Entity {service: $service_id, name: $entity_name})-[r:RELATED_TO|CAN_JOIN_TO]-(other:Entity)
                RETURN other.service AS related_service,
                       other.name AS related_name,
                       type(r) AS rel_type,
                       r.join_column AS join_column,
                       r.confidence AS confidence,
                       r.cardinality AS cardinality
                """,
                service_id=service_id,
                entity_name=entity_name,
            )
            rows = []
            for r in result:
                d = dict(r)
                pl = d.get("property_labels")
                if isinstance(pl, str):
                    try:
                        import json as _json
                        d["property_labels"] = _json.loads(pl)
                    except Exception:
                        d["property_labels"] = {}
                rows.append(d)
            return rows

    def upsert_role_policy(self, role: Dict[str, Any]):
        if not self._driver:
            return
        with self._driver.session() as session:
            session.run(
                """
                MERGE (r:Role {id: $id})
                SET r.name = $name,
                    r.allowed_ops = $allowed_ops,
                    r.allowed_entities = $allowed_entities,
                    r.allowed_services = $allowed_services
                """,
                id=role["id"],
                name=role["name"],
                allowed_ops=role.get("allowed_ops", []),
                allowed_entities=role.get("allowed_entities", []),
                allowed_services=role.get("allowed_services", []),
            )

    def get_custom_entities(self) -> List[Dict[str, Any]]:
        if not self._driver:
            return []
        with self._driver.session() as session:
            result = session.run(
                """
                MATCH (s:Service)-[:HAS_ENTITY]->(e:Entity)
                WHERE e.is_custom = true
                RETURN s.id AS service_id, e.name AS name, e.base_entity_set AS base_entity_set,
                       e.description AS description, e.default_filter AS default_filter,
                       e.allowed_columns AS allowed_columns, e.created_by AS created_by,
                       e.created_at AS created_at
                """
            )
            rows = []
            for r in result:
                d = dict(r)
                pl = d.get("property_labels")
                if isinstance(pl, str):
                    try:
                        import json as _json
                        d["property_labels"] = _json.loads(pl)
                    except Exception:
                        d["property_labels"] = {}
                rows.append(d)
            return rows

    def get_service_entities(self, service_id: str) -> List[Dict[str, Any]]:
        """Get all entities for a service from Neo4j (used when metadata fetch fails)."""
        if not self._driver:
            return []
        with self._driver.session() as session:
            result = session.run(
                """
                MATCH (s:Service)-[:HAS_ENTITY]->(e:Entity)
                WHERE s.id = $service_id AND (e.is_custom = false OR e.is_custom = 'false')
                RETURN e.name AS name, e.type AS type, e.properties AS properties, e.label AS label, e.property_labels AS property_labels
                """,
                service_id=service_id,
            )
            rows = []
            for r in result:
                d = dict(r)
                pl = d.get("property_labels")
                if isinstance(pl, str):
                    try:
                        import json as _json
                        d["property_labels"] = _json.loads(pl)
                    except Exception:
                        d["property_labels"] = {}
                rows.append(d)
            return rows

    # --- Cross-Service Join ---

    def upsert_join(self, join_def: Dict[str, Any]):
        if not self._driver:
            return
        import json as _json
        with self._driver.session() as session:
            session.run(
                """
                MERGE (j:CrossServiceJoin {id: $id})
                SET j.name = $name,
                    j.strategy = $strategy,
                    j.left_service = $left_service,
                    j.left_entity = $left_entity,
                    j.left_key = $left_key,
                    j.right_service = $right_service,
                    j.right_entity = $right_entity,
                    j.right_key = $right_key,
                    j.column_mapping = $column_mapping,
                    j.description = $description,
                    j.created_by = $created_by,
                    j.created_at = $created_at
                WITH j
                MATCH (ls:Service {id: $left_service})
                MATCH (rs:Service {id: $right_service})
                MERGE (ls)-[:HAS_JOIN]->(j)
                MERGE (rs)-[:HAS_JOIN]->(j)
                WITH j, ls, rs
                MATCH (le:Entity {service: $left_service, name: $left_entity})
                MATCH (re:Entity {service: $right_service, name: $right_entity})
                MERGE (le)-[:JOINS_IN]->(j)
                MERGE (re)-[:JOINS_IN]->(j)
                """,
                id=join_def["id"],
                name=join_def["name"],
                strategy=join_def["strategy"],
                left_service=join_def["left_service"],
                left_entity=join_def["left_entity"],
                left_key=join_def.get("left_key", ""),
                right_service=join_def["right_service"],
                right_entity=join_def["right_entity"],
                right_key=join_def.get("right_key", ""),
                column_mapping=_json.dumps(join_def.get("column_mapping", {})),
                description=join_def.get("description", ""),
                created_by=join_def.get("created_by", ""),
                created_at=join_def.get("created_at", ""),
            )

    def list_joins(self) -> List[Dict[str, Any]]:
        if not self._driver:
            return []
        import json as _json
        with self._driver.session() as session:
            result = session.run(
                """
                MATCH (j:CrossServiceJoin)
                RETURN j.id AS id, j.name AS name, j.strategy AS strategy,
                       j.left_service AS left_service, j.left_entity AS left_entity,
                       j.left_key AS left_key, j.right_service AS right_service,
                       j.right_entity AS right_entity, j.right_key AS right_key,
                       j.column_mapping AS column_mapping, j.description AS description,
                       j.created_by AS created_by, j.created_at AS created_at
                ORDER BY j.created_at DESC
                """
            )
            out = []
            for r in result:
                d = dict(r)
                try:
                    d["column_mapping"] = _json.loads(d.get("column_mapping") or "{}")
                except Exception:
                    d["column_mapping"] = {}
                out.append(d)
            return out

    def get_join(self, join_id: str) -> Optional[Dict[str, Any]]:
        if not self._driver:
            return None
        import json as _json
        with self._driver.session() as session:
            result = session.run(
                """
                MATCH (j:CrossServiceJoin {id: $id})
                RETURN j.id AS id, j.name AS name, j.strategy AS strategy,
                       j.left_service AS left_service, j.left_entity AS left_entity,
                       j.left_key AS left_key, j.right_service AS right_service,
                       j.right_entity AS right_entity, j.right_key AS right_key,
                       j.column_mapping AS column_mapping, j.description AS description,
                       j.created_by AS created_by, j.created_at AS created_at
                """,
                id=join_id,
            )
            record = result.single()
            if not record:
                return None
            d = dict(record)
            try:
                d["column_mapping"] = _json.loads(d.get("column_mapping") or "{}")
            except Exception:
                d["column_mapping"] = {}
            return d

    def delete_join(self, join_id: str) -> bool:
        if not self._driver:
            return False
        with self._driver.session() as session:
            result = session.run(
                """
                MATCH (j:CrossServiceJoin {id: $id})
                DETACH DELETE j
                RETURN count(j) AS deleted
                """,
                id=join_id,
            )
            record = result.single()
            return record["deleted"] > 0 if record else False

    def delete_entity(self, service_id: str, name: str) -> bool:
        if not self._driver:
            return False
        with self._driver.session() as session:
            result = session.run(
                """
                MATCH (e:Entity {service: $service_id, name: $name})
                DETACH DELETE e
                RETURN count(e) AS deleted
                """,
                service_id=service_id,
                name=name,
            )
            record = result.single()
            return record["deleted"] > 0 if record else False

    def delete_service(self, service_id: str) -> bool:
        if not self._driver:
            return False
        with self._driver.session() as session:
            result = session.run(
                """
                MATCH (s:Service {id: $service_id})
                OPTIONAL MATCH (s)-[:HAS_ENTITY]->(e:Entity)
                DETACH DELETE s, e
                RETURN count(s) + count(e) AS deleted
                """,
                service_id=service_id,
            )
            record = result.single()
            return record["deleted"] > 0 if record else False

    def find_services_for_entities(self, entity_names: List[str]) -> List[Dict[str, Any]]:
        if not self._driver:
            return []
        with self._driver.session() as session:
            result = session.run(
                """
                MATCH (e:Entity)
                WHERE toLower(e.name) IN $names OR any(n IN $names WHERE toLower(e.name) CONTAINS toLower(n))
                MATCH (s:Service)-[:HAS_ENTITY]->(e)
                RETURN DISTINCT s.id AS service_id, s.name AS name, s.base_url AS base_url, s.description AS description,
                       collect(DISTINCT e.name) AS entities
                """,
                names=[n.lower() for n in entity_names],
            )
            rows = []
            for r in result:
                d = dict(r)
                pl = d.get("property_labels")
                if isinstance(pl, str):
                    try:
                        import json as _json
                        d["property_labels"] = _json.loads(pl)
                    except Exception:
                        d["property_labels"] = {}
                rows.append(d)
            return rows

    def find_related_entities(self, service_id: str, entity_name: str) -> List[Dict[str, Any]]:
        if not self._driver:
            return []
        with self._driver.session() as session:
            result = session.run(
                """
                MATCH (a:Entity {service: $service_id, name: $entity_name})-[r:RELATED_TO]->(b:Entity)
                RETURN b.service AS to_service, b.name AS to_name, r.type AS rel_type,
                       r.cardinality AS cardinality, r.description AS description
                UNION
                MATCH (b:Entity)-[r:RELATED_TO]->(a:Entity {service: $service_id, name: $entity_name})
                MATCH (a)-[:HAS_ENTITY]-(s:Service {id: $service_id})
                RETURN b.service AS to_service, b.name AS to_name, r.type AS rel_type,
                       r.cardinality AS cardinality, r.description AS description
                """,
                service_id=service_id,
                entity_name=entity_name,
            )
            rows = []
            for r in result:
                d = dict(r)
                pl = d.get("property_labels")
                if isinstance(pl, str):
                    try:
                        import json as _json
                        d["property_labels"] = _json.loads(pl)
                    except Exception:
                        d["property_labels"] = {}
                rows.append(d)
            return rows

    def get_role_policy(self, role_id: str) -> Optional[Dict[str, Any]]:
        if not self._driver:
            return None
        with self._driver.session() as session:
            result = session.run("MATCH (r:Role {id: $id}) RETURN r", id=role_id)
            record = result.single()
            if not record:
                return None
            node = record["r"]
            return dict(node)

    def get_entity_metadata(self, service_id: str, entity_name: str) -> Optional[Dict[str, Any]]:
        if not self._driver:
            return None
        with self._driver.session() as session:
            result = session.run(
                """
                MATCH (s:Service {id: $service_id})-[:HAS_ENTITY]->(e:Entity {name: $entity_name})
                RETURN e
                """,
                service_id=service_id,
                entity_name=entity_name,
            )
            record = result.single()
            if not record:
                return None
            return dict(record["e"])

    def list_all_services(self) -> List[Dict[str, Any]]:
        if not self._driver:
            return []
        with self._driver.session() as session:
            result = session.run("MATCH (s:Service) RETURN s")
            return [dict(r["s"]) for r in result]

    def list_all_entities(self) -> List[Dict[str, Any]]:
        if not self._driver:
            return []
        with self._driver.session() as session:
            result = session.run(
                """
                MATCH (s:Service)-[:HAS_ENTITY]->(e:Entity)
                RETURN s.id AS service_id, s.name AS service_name,
                       e.name AS entity_name, e.type AS type,
                       e.description AS description, e.allowed_ops AS allowed_ops,
                       e.properties AS properties
                """
            )
            rows = []
            for r in result:
                d = dict(r)
                pl = d.get("property_labels")
                if isinstance(pl, str):
                    try:
                        import json as _json
                        d["property_labels"] = _json.loads(pl)
                    except Exception:
                        d["property_labels"] = {}
                rows.append(d)
            return rows

    def clear(self):
        if not self._driver:
            return
        with self._driver.session() as session:
            session.run("MATCH (n) DETACH DELETE n")


neo4j_client = Neo4jClient()
