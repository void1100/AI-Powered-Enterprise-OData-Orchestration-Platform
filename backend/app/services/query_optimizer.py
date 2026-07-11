"""
Query Optimizer — reduces tokens, speeds up execution, improves accuracy.

Features:
1. Intent classification (skip LLM for known patterns)
2. Query plan caching (reuse plans for similar queries)
3. Smart $select (only fetch needed columns)
4. Response validation (self-correct on empty/bad responses)
"""
import hashlib
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class QueryIntent:
    READ = "read"
    AGGREGATE = "aggregate"
    PREDICT = "predict"
    EXTREMUM = "extremum"
    COMPARE = "compare"
    FILTER = "filter"
    COUNT = "count"
    CREATE = "create"
    UPDATE = "update"
    DELETE_INTENT = "delete"
    UNKNOWN = "unknown"


class QueryOptimizer:
    """Optimizes queries for token reduction, speed, and accuracy."""

    def __init__(self, cache_ttl: int = 300, max_cache_size: int = 200):
        self._plan_cache: Dict[str, Tuple[Dict, float]] = {}
        self._cache_ttl = cache_ttl
        self._max_cache_size = max_cache_size
        self._stats = {
            "cache_hits": 0,
            "cache_misses": 0,
            "llm_skipped": 0,
            "intent_classified": 0,
        }

    @property
    def stats(self) -> Dict[str, int]:
        return dict(self._stats)

    # ── Intent Classification ────────────────────────────────────────────

    AGGREGATION_KEYWORDS = {
        "count", "sum", "total", "average", "avg", "minimum", "maximum",
        "min", "max", "per", "by", "group", "aggregate", "rollup",
    }
    EXTREMUM_KEYWORDS = {
        "most", "least", "highest", "lowest", "top", "bottom", "best",
        "worst", "largest", "smallest", "maximum", "minimum", "fewest",
    }
    PREDICT_KEYWORDS = {
        "predict", "forecast", "estimate", "project", "will be",
        "likely", "probability", "chance", "discontinued",
    }
    COMPARE_KEYWORDS = {
        "compare", "vs", "versus", "difference", "对比", "differ",
    }
    COUNT_KEYWORDS = {"count", "how many", "number of", "total number"}
    CREATE_KEYWORDS = {"create", "add", "new", "insert", "make", "register", "submit"}
    UPDATE_KEYWORDS = {"update", "modify", "change", "set", "edit", "replace", "patch"}
    DELETE_KEYWORDS = {"delete", "remove", "destroy", "drop", "eliminate"}

    # Signals that a query is too complex for the mock planner (needs LLM)
    COMPLEXITY_SIGNALS = [
        r"\bby\s+\w+\s+count\b",           # "by order count"
        r"\bby\s+\w+\s+total\b",           # "by sales total"
        r"\bby\s+\w+\s+sum\b",             # "by revenue sum"
        r"\bincluding\b",                   # "including their country"
        r"\bsorted\s+by\b",                 # "sorted by most orders"
        r"\band\s+\w+\b.*\band\s+\w+\b",   # multiple "and" conditions
        r"\bwith\s+their\b",                # "with their country"
        r"\bjoined?\b",                     # "joined"
        r"\bfrom\s+\w+\s+and\s+\w+\b",     # "from Customers and Orders"
        r"\bbetween\b",                     # "between date X and Y"
        r"\brank\b",                        # "rank by"
        r"\branking\b",
        r"\bper\s+\w+\b",                  # "per customer" (needs grouping)
    ]

    def _is_complex_query(self, q: str) -> bool:
        """Detect queries too complex for the mock planner."""
        for pattern in self.COMPLEXITY_SIGNALS:
            if re.search(pattern, q):
                return True
        # Multiple aggregation keywords = complex
        agg_hits = sum(1 for kw in self.AGGREGATION_KEYWORDS if kw in q)
        if agg_hits >= 2:
            return True
        # "top N" + "by" + aggregation = complex (needs join)
        if re.search(r"\btop\s+\d+\b", q) and re.search(r"\bby\s+\w+", q):
            return True
        return False

    def classify_intent(self, query: str) -> str:
        """Classify query intent without LLM (saves tokens)."""
        q = query.lower().strip()

        # Write intents (must be before read checks)
        if any(kw in q for kw in self.DELETE_KEYWORDS):
            return QueryIntent.DELETE_INTENT
        if any(kw in q for kw in self.UPDATE_KEYWORDS):
            return QueryIntent.UPDATE
        if any(kw in q for kw in self.CREATE_KEYWORDS):
            return QueryIntent.CREATE

        # Prediction (must be before other checks)
        if any(kw in q for kw in self.PREDICT_KEYWORDS):
            return QueryIntent.PREDICT

        # Check complexity FIRST — complex queries need LLM
        if self._is_complex_query(q):
            # Still classify for metadata, but don't skip LLM
            if re.search(r"\b(count|sum|total|average|avg)\b", q):
                return QueryIntent.AGGREGATE
            if re.search(r"\b(most|least|highest|lowest|top|bottom)\b", q):
                return QueryIntent.AGGREGATE
            return QueryIntent.AGGREGATE

        # Simple extremum: "which country has the most customers"
        extremum_patterns = [
            r"\bwhich\b.*\b(has|have|is|are)\b.*\b(most|least|highest|lowest|fewest|largest|smallest)\b",
            r"\b(most|least|highest|lowest|fewest|largest|smallest|best|worst)\b",
        ]
        if any(re.search(p, q) for p in extremum_patterns):
            return QueryIntent.EXTREMUM

        # Simple aggregation
        agg_patterns = [
            r"\b(count|sum|total|average|avg|min|minimum|max|maximum)\b",
        ]
        if any(re.search(p, q) for p in agg_patterns):
            return QueryIntent.COUNT

        # Compare
        if any(kw in q for kw in self.COMPARE_KEYWORDS):
            return QueryIntent.COMPARE

        # Filter
        if re.search(r"\b(where|with|that|which|whose|filter|having)\b", q):
            return QueryIntent.FILTER

        return QueryIntent.READ

    def can_skip_llm(self, intent: str, has_explicit_service: bool, is_complex: bool = False) -> bool:
        """Determine if we can skip the LLM entirely."""
        # NEVER skip LLM for complex queries
        if is_complex:
            return False
        # Always skip LLM for prediction
        if intent == QueryIntent.PREDICT:
            return True
        # Skip for simple extremum with explicit service
        if intent == QueryIntent.EXTREMUM and has_explicit_service:
            return True
        # Skip for simple reads with explicit service
        if intent == QueryIntent.READ and has_explicit_service:
            return True
        # Skip for simple counts with explicit service
        if intent == QueryIntent.COUNT and has_explicit_service:
            return True
        return False

    # ── Query Plan Cache ─────────────────────────────────────────────────

    def _cache_key(self, query: str, services: List[str]) -> str:
        """Generate cache key from query and available services."""
        normalized = query.lower().strip()
        # Remove session-specific words
        normalized = re.sub(r"\b(session|chat|history)\b", "", normalized)
        content = f"{normalized}|{'|'.join(sorted(services))}"
        return hashlib.md5(content.encode()).hexdigest()

    def get_cached_plan(self, query: str, services: List[str]) -> Optional[Dict]:
        """Get cached plan if available and not expired."""
        key = self._cache_key(query, services)
        if key in self._plan_cache:
            plan, ts = self._plan_cache[key]
            if time.time() - ts < self._cache_ttl:
                self._stats["cache_hits"] += 1
                logger.info(f"Plan cache hit for query: {query[:50]}...")
                return plan
            else:
                del self._plan_cache[key]
        self._stats["cache_misses"] += 1
        return None

    def cache_plan(self, query: str, services: List[str], plan: Dict):
        """Cache a query plan."""
        if len(self._plan_cache) >= self._max_cache_size:
            # Remove oldest entry
            oldest_key = min(self._plan_cache, key=lambda k: self._plan_cache[k][1])
            del self._plan_cache[oldest_key]

        key = self._cache_key(query, services)
        self._plan_cache[key] = (plan, time.time())
        logger.info(f"Cached plan for query: {query[:50]}...")

    def invalidate_cache(self, query: Optional[str] = None):
        """Invalidate cache entries."""
        if query:
            keys_to_remove = [k for k in self._plan_cache if query.lower() in k]
            for k in keys_to_remove:
                del self._plan_cache[k]
        else:
            self._plan_cache.clear()

    # ── Smart $select ────────────────────────────────────────────────────

    # Common words that are NOT column names
    STOP_COLUMN_WORDS = {
        "show", "me", "the", "all", "from", "where", "with", "and", "or",
        "that", "which", "who", "how", "many", "much", "count", "sum",
        "total", "average", "min", "max", "top", "bottom", "first", "last",
        "list", "get", "find", "display", "give", "tell", "please",
        "order", "by", "group", "per", "in", "on", "at", "to", "for",
        "of", "a", "an", "is", "are", "was", "were", "be", "been",
        "has", "have", "had", "do", "does", "did", "will", "would",
        "could", "should", "may", "might", "can", "not", "no", "yes",
        "customers", "orders", "products", "suppliers", "employees",
        "categories", "regions", "territories", "shippers",
        "materials", "plants", "operations", "manufacturing",
        "purchase", "orders", "items",
    }

    def compute_smart_select(
        self,
        query: str,
        entity_properties: List[str],
        max_columns: int = 15,
    ) -> Optional[List[str]]:
        """
        Compute smart $select — only columns relevant to the query.
        Returns None if we should select all (no optimization possible).
        """
        if not entity_properties:
            return None

        q = query.lower()
        def normalize_word(word: str) -> str:
            if word.endswith("ies") and len(word) > 4:
                return word[:-3] + "y"
            if word.endswith("es") and len(word) > 3:
                return word[:-2]
            if word.endswith("s") and len(word) > 3:
                return word[:-1]
            return word

        q_words = {normalize_word(w) for w in re.findall(r'[a-z]+', q)}
        q_words = q_words - self.STOP_COLUMN_WORDS

        if not q_words:
            return None

        # Match query words to column names
        matched_cols = []
        for prop in entity_properties:
            prop_lower = prop.lower()
            prop_words = {normalize_word(w) for w in re.findall(r'[a-z]+', prop_lower)}

            # Direct match: query word appears in column name
            if q_words & prop_words:
                matched_cols.append(prop)
                continue

            # Partial match: query word is substring of column name or vice versa
            for qw in q_words:
                if len(qw) >= 3 and (qw in prop_lower or prop_lower in qw):
                    matched_cols.append(prop)
                    break

        if not matched_cols:
            return None

        # Always include ID columns
        id_cols = [c for c in entity_properties if "id" in c.lower() and c not in matched_cols]
        result = id_cols + matched_cols

        # Limit columns
        if len(result) > max_columns:
            result = result[:max_columns]

        return result if result else None

    # ── Response Validation ──────────────────────────────────────────────

    def validate_response(
        self,
        plan: Dict,
        response_data: Any,
        entity_properties: List[str],
    ) -> Tuple[bool, str]:
        """
        Validate response and suggest corrections.
        Returns (is_valid, reason).
        """
        if response_data is None:
            return False, "No response data"

        # Check if response is empty
        if isinstance(response_data, dict):
            rows = response_data.get("value", response_data.get("results", []))
            if not rows:
                # Check if entity has data at all
                return False, "Empty response — entity may have no data"
            # Validate columns exist
            if rows and entity_properties:
                first_row = rows[0] if isinstance(rows[0], dict) else {}
                missing = [c for c in entity_properties[:5] if c not in first_row]
                if missing:
                    return False, f"Missing columns: {', '.join(missing)}"
        elif isinstance(response_data, list):
            if not response_data:
                return False, "Empty list response"

        return True, "OK"

    def suggest_correction(
        self,
        plan: Dict,
        error_reason: str,
        available_entities: List[str],
    ) -> Optional[Dict]:
        """
        Suggest a corrected plan based on validation failure.
        """
        if "empty" in error_reason.lower():
            # Try a different entity if available
            current_entity = plan.get("steps", [{}])[0].get("entity_set") if plan.get("steps") else None
            if current_entity and available_entities:
                # Find similar entity
                for alt in available_entities:
                    if alt != current_entity:
                        corrected = json.loads(json.dumps(plan))
                        if corrected.get("steps"):
                            corrected["steps"][0]["entity_set"] = alt
                        return corrected

        if "missing columns" in error_reason.lower():
            # Relax $select
            corrected = json.loads(json.dumps(plan))
            if corrected.get("steps"):
                corrected["steps"][0].pop("select", None)
            return corrected

        return None

    # ── Query Optimization Pipeline ──────────────────────────────────────

    def optimize_plan(self, plan: Dict, query: str) -> Dict:
        """
        Optimize a query plan for faster execution.
        - Remove unnecessary $select *
        - Optimize $top based on intent
        - Clean up filters
        """
        if not plan.get("steps"):
            return plan

        optimized = json.loads(json.dumps(plan))

        for step in optimized["steps"]:
            # Remove $select * (let backend decide)
            select = step.get("select", [])
            if select == ["*"]:
                step.pop("select", None)

            # Optimize $top
            top = step.get("top")
            intent = self.classify_intent(query)

            if intent == QueryIntent.READ and not top:
                step["top"] = 50  # Default for reads
            elif intent == QueryIntent.COUNT:
                step.pop("top", None)  # Count needs all rows
            elif intent == QueryIntent.EXTREMUM:
                if not top or top > 200:
                    step["top"] = 200  # Extremum needs enough data

            # Clean up empty filters
            filt = step.get("filter")
            if filt and filt.strip() in ("", "null", "None"):
                step.pop("filter", None)

        return optimized

    def clear_cache(self):
        """Clear all cached plans."""
        self._plan_cache.clear()
        self._stats["cache_hits"] = 0
        self._stats["cache_misses"] = 0


query_optimizer = QueryOptimizer()
