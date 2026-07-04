"""Entity Selector with Auto-Join Detection.

Detects potential joins between selected entities based on:
1. Common column names (exact match)
2. Similar column names (fuzzy match)
3. Neo4j stored relationships

Each property and join gets a priority label for display.
"""
import re
from typing import List, Dict, Tuple, Any, Optional
from difflib import SequenceMatcher


# Property label patterns
_KEY_PATTERNS = [
    (r".*id$", "Key"),
    (r".*key$", "Key"),
    (r".*code$", "Key"),
    (r".*number$", "Key"),
    (r"purchaseorder$", "Key"),
    (r"manufacturingorder$", "Key"),
    (r"^material$", "Key"),
    (r"^plant$", "Key"),
    (r"^customer$", "Key"),
    (r"^supplier$", "Key"),
]

_FK_PATTERNS = [
    (r".*id$", "ForeignKey"),
    (r".*key$", "ForeignKey"),
    (r".*code$", "ForeignKey"),
    (r"purchaseorder", "ForeignKey"),
    (r"manufacturingorder", "ForeignKey"),
    (r"material", "ForeignKey"),
    (r"plant", "ForeignKey"),
    (r"customer", "ForeignKey"),
    (r"supplier", "ForeignKey"),
]

_DATE_PATTERNS = [
    (r".*date$", "Date"),
    (r".*time$", "Date"),
    (r".*datetime$", "Date"),
    (r"^validity", "Date"),
    (r"^creation", "Date"),
    (r"^lastchange", "Date"),
]

_DESCRIPTION_PATTERNS = [
    (r".*text$", "Description"),
    (r".*name$", "Description"),
    (r".*description$", "Description"),
    (r".*longtext$", "Description"),
    (r".*shorttext$", "Description"),
    (r"^plain", "Description"),
]

_MEASURE_PATTERNS = [
    (r".*amount$", "Measure"),
    (r".*price$", "Measure"),
    (r".*quantity$", "Measure"),
    (r".*weight$", "Measure"),
    (r".*volume$", "Measure"),
    (r".*rate$", "Measure"),
    (r".*percent$", "Measure"),
    (r".*value$", "Measure"),
    (r"^netpayment", "Measure"),
    (r"^cashdiscount", "Measure"),
    (r"^exchange", "Measure"),
]

_STATUS_PATTERNS = [
    (r".*status$", "Status"),
    (r".*blocked$", "Status"),
    (r".*complete$", "Status"),
    (r"deletion", "Status"),
]


def classify_property(prop_name: str) -> str:
    """Classify a property into a priority label."""
    lower = prop_name.lower().strip()

    # Check Key patterns first (highest priority)
    for pattern, label in _KEY_PATTERNS:
        if re.match(pattern, lower):
            return label

    # Check Date
    for pattern, label in _DATE_PATTERNS:
        if re.match(pattern, lower):
            return label

    # Check Status
    for pattern, label in _STATUS_PATTERNS:
        if re.match(pattern, lower):
            return label

    # Check Measure
    for pattern, label in _MEASURE_PATTERNS:
        if re.match(pattern, lower):
            return label

    # Check Description
    for pattern, label in _DESCRIPTION_PATTERNS:
        if re.match(pattern, lower):
            return label

    # Check ForeignKey (lower priority than Key)
    for pattern, label in _FK_PATTERNS:
        if re.match(pattern, lower):
            return label

    return "Attribute"


# Join label constants
JOIN_LABELS = {
    "primary_key": "Primary Key",
    "foreign_key": "Foreign Key",
    "attribute": "Attribute Match",
    "fuzzy": "Fuzzy Match",
    "neo4j": "Confirmed Join",
}


def classify_join(join_def: Dict[str, Any]) -> str:
    """Classify a detected join into a priority label."""
    left_key = join_def.get("left_key", "").lower()
    right_key = join_def.get("right_key", "").lower()
    confidence = join_def.get("confidence", 0)
    match_type = join_def.get("match_type", "")

    # If keys are the same and high confidence → Primary Key
    if left_key == right_key and confidence >= 0.90:
        return "primary_key"

    # If Neo4j confirmed → Confirmed Join
    if match_type == "neo4j":
        return "neo4j"

    # If fuzzy → Fuzzy Match
    if match_type == "fuzzy":
        return "fuzzy"

    # If keys differ but both look like keys → Foreign Key
    key_pattern = r".*(id|key|code|number)$"
    if re.match(key_pattern, left_key) and re.match(key_pattern, right_key):
        return "foreign_key"

    return "attribute"


class EntitySelector:
    """Detect joins between selected entities."""

    def __init__(self):
        self._neo4j = None

    def _get_neo4j(self):
        if self._neo4j is None:
            try:
                from app.db.neo4j_client import neo4j_client
                self._neo4j = neo4j_client
            except Exception:
                self._neo4j = None
        return self._neo4j

    def _extract_prop_names(self, properties: List) -> List[str]:
        """Extract property names from either string or dict format."""
        names = []
        for p in properties:
            if isinstance(p, str):
                names.append(p)
            elif isinstance(p, dict):
                names.append(p.get("name", ""))
            else:
                names.append(str(p))
        return [n for n in names if n]

    def detect_joins(self, entities: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Detect potential joins between a list of selected entities.

        Args:
            entities: [{"service_id": "sopra-po", "entity_name": "A_PurchaseOrder", "properties": [...]}]

        Returns:
            [{"left_service": ..., "left_entity": ..., "right_service": ..., "right_entity": ...,
              "left_key": ..., "right_key": ..., "confidence": 0.0-1.0, "match_type": "exact|fuzzy|neo4j"}]
        """
        if len(entities) < 2:
            return []

        joins = []
        for i in range(len(entities)):
            for j in range(i + 1, len(entities)):
                e1 = entities[i]
                e2 = entities[j]
                detected = self._detect_pair_joins(e1, e2)
                joins.extend(detected)

        # Sort by confidence descending
        joins.sort(key=lambda x: -x.get("confidence", 0))

        # Deduplicate: keep best join per entity pair
        deduped = self._deduplicate_joins(joins)

        # Add priority labels
        for j in deduped:
            j["label"] = classify_join(j)

        return deduped

    def _detect_pair_joins(self, e1: Dict, e2: Dict) -> List[Dict]:
        """Detect joins between two entities."""
        joins = []
        props1 = self._extract_prop_names(e1.get("properties", []))
        props2 = self._extract_prop_names(e2.get("properties", []))

        if not props1 or not props2:
            return []

        # 1. Exact column name matches
        exact_matches = self.find_common_columns(props1, props2)
        for col in exact_matches:
            confidence = self._score_exact_match(col, props1, props2)
            joins.append({
                "left_service": e1.get("service_id", ""),
                "left_entity": e1.get("entity_name", ""),
                "right_service": e2.get("service_id", ""),
                "right_entity": e2.get("entity_name", ""),
                "left_key": col,
                "right_key": col,
                "confidence": confidence,
                "match_type": "exact",
            })

        # 2. Fuzzy column name matches
        fuzzy_matches = self.find_fuzzy_matches(props1, props2)
        for prop1, prop2, score in fuzzy_matches:
            # Skip if already found as exact match
            if prop1 in exact_matches or prop2 in exact_matches:
                continue
            confidence = score * 0.8  # Fuzzy matches are less confident
            joins.append({
                "left_service": e1.get("service_id", ""),
                "left_entity": e1.get("entity_name", ""),
                "right_service": e2.get("service_id", ""),
                "right_entity": e2.get("entity_name", ""),
                "left_key": prop1,
                "right_key": prop2,
                "confidence": round(confidence, 3),
                "match_type": "fuzzy",
            })

        # 3. Check Neo4j for existing relationships
        neo4j_joins = self.get_neo4j_relationships(
            e1.get("service_id", ""), e1.get("entity_name", ""),
            e2.get("service_id", ""), e2.get("entity_name", ""),
        )
        for nj in neo4j_joins:
            # Check if this join is already detected
            already = any(
                j["left_key"] == nj["left_key"] and j["right_key"] == nj["right_key"]
                for j in joins
            )
            if not already:
                joins.append({
                    "left_service": e1.get("service_id", ""),
                    "left_entity": e1.get("entity_name", ""),
                    "right_service": e2.get("service_id", ""),
                    "right_entity": e2.get("entity_name", ""),
                    "left_key": nj["left_key"],
                    "right_key": nj["right_key"],
                    "confidence": nj.get("confidence", 0.9),
                    "match_type": "neo4j",
                })
            else:
                # Boost confidence if Neo4j confirms
                for j in joins:
                    if j["left_key"] == nj["left_key"] and j["right_key"] == nj["right_key"]:
                        j["confidence"] = min(1.0, j["confidence"] + 0.15)

        return joins

    def find_common_columns(self, props1: List[str], props2: List[str]) -> List[str]:
        """Find exact matching column names between two entity property lists."""
        set1 = {p.lower().strip() for p in props1}
        set2 = {p.lower().strip() for p in props2}
        common = set1 & set2
        # Return original casing from props1
        result = []
        for p in props1:
            if p.lower().strip() in common and p not in result:
                result.append(p)
        return result

    def find_fuzzy_matches(self, props1: List[str], props2: List[str],
                           threshold: float = 0.75) -> List[Tuple[str, str, float]]:
        """Find similar column names using string similarity."""
        matches = []
        for p1 in props1:
            p1_lower = p1.lower().strip()
            for p2 in props2:
                p2_lower = p2.lower().strip()
                if p1_lower == p2_lower:
                    continue  # Skip exact matches
                score = SequenceMatcher(None, p1_lower, p2_lower).ratio()
                if score >= threshold:
                    matches.append((p1, p2, round(score, 3)))
        # Sort by score descending, take top matches
        matches.sort(key=lambda x: -x[2])
        # Remove duplicate pairings (keep best)
        seen = set()
        result = []
        for m in matches:
            key = (m[0], m[1])
            if key not in seen:
                seen.add(key)
                result.append(m)
        return result[:10]  # Limit to top 10

    def get_neo4j_relationships(self, svc1: str, entity1: str,
                                 svc2: str, entity2: str) -> List[Dict]:
        """Query Neo4j for existing relationships between two entities."""
        g = self._get_neo4j()
        if g is None or not g.is_available():
            return []

        try:
            results = g.find_related_entities(entity1, svc1)
            rels = []
            for r in results:
                if (r.get("related_service") == svc2 and
                    r.get("related_name") == entity2):
                    # Extract join column from relationship
                    join_col = r.get("join_column", "")
                    if not join_col:
                        # Try to infer from properties
                        join_col = self._infer_join_column(entity1, entity2, svc1, svc2)
                    if join_col:
                        rels.append({
                            "left_key": join_col,
                            "right_key": join_col,
                            "confidence": 0.85,
                        })
            return rels
        except Exception:
            return []

    def _infer_join_column(self, entity1: str, entity2: str,
                           svc1: str, svc2: str) -> str:
        """Try to infer join column from entity names or common patterns."""
        # Common SAP join patterns
        patterns = [
            r"(PurchaseOrder)",
            r"(ManufacturingOrder)",
            r"(Material)",
            r"(Plant)",
            r"(SalesOrder)",
            r"(Customer)",
            r"(Supplier)",
        ]
        name1 = entity1.lower()
        name2 = entity2.lower()
        for pattern in patterns:
            match = re.search(pattern, name1, re.IGNORECASE)
            if match:
                col = match.group(1)
                if col.lower() in name2.lower():
                    return col
        return ""

    def _score_exact_match(self, column: str, props1: List[str], props2: List[str]) -> float:
        """Score an exact column match based on how likely it is a join key."""
        col_lower = column.lower()

        # High confidence for common key patterns
        key_patterns = [
            r".*id$", r".*key$", r".*code$", r".*number$",
            r"purchaseorder", r"manufacturingorder", r"material",
            r"plant", r"customer", r"supplier",
        ]
        for pattern in key_patterns:
            if re.match(pattern, col_lower):
                return 0.95

        # Medium confidence for columns that appear in both entities
        return 0.80

    def _deduplicate_joins(self, joins: List[Dict]) -> List[Dict]:
        """Keep only the best join per entity pair + key combination."""
        seen = {}
        for j in joins:
            # Create a key for this pair (order-independent)
            pair = tuple(sorted([
                f"{j['left_service']}/{j['left_entity']}",
                f"{j['right_service']}/{j['right_entity']}",
            ]))
            key = (pair, j["left_key"], j["right_key"])
            if key not in seen or j["confidence"] > seen[key]["confidence"]:
                seen[key] = j
        return list(seen.values())

    def store_successful_join(self, left_service: str, left_entity: str,
                               right_service: str, right_entity: str,
                               left_key: str, right_key: str, confidence: float = 0.9):
        """Store a successfully used join in Neo4j for future reference."""
        g = self._get_neo4j()
        if g is None or not g.is_available():
            return
        try:
            g.upsert_entity_relationship(
                from_service=left_service,
                from_entity=left_entity,
                to_service=right_service,
                to_entity=right_entity,
                join_column=left_key if left_key == right_key else f"{left_key}={right_key}",
                confidence=confidence,
            )
        except Exception:
            pass


# Module-level singleton
entity_selector = EntitySelector()
