"""
Query Enhancement module:
- Result summarization (auto NL insights)
- Auto chart recommendations
- Query caching
- Query suggestions / autocomplete
"""
import time
import hashlib
import json
from typing import Any, Dict, List, Optional, Tuple
from loguru import logger


# ─── Query Cache ──────────────────────────────────────────────────────────────

class QueryCache:
    """LRU cache for repeated queries with TTL."""

    def __init__(self, max_size: int = 100, ttl_seconds: int = 300):
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._max_size = max_size
        self._ttl = ttl_seconds

    def _make_key(self, query: str, session_id: str = "") -> str:
        raw = f"{query.strip().lower()}::{session_id}"
        return hashlib.md5(raw.encode()).hexdigest()

    def get(self, query: str, session_id: str = "") -> Optional[Dict[str, Any]]:
        key = self._make_key(query, session_id)
        entry = self._cache.get(key)
        if not entry:
            return None
        if time.time() - entry["ts"] > self._ttl:
            del self._cache[key]
            return None
        return entry["data"]

    def set(self, query: str, data: Dict[str, Any], session_id: str = ""):
        key = self._make_key(query, session_id)
        if len(self._cache) >= self._max_size:
            oldest = min(self._cache, key=lambda k: self._cache[k]["ts"])
            del self._cache[oldest]
        self._cache[key] = {"data": data, "ts": time.time(), "session_id": session_id}

    def clear_session(self, session_id: str):
        """Remove all cache entries belonging to a specific session."""
        keys_to_remove = [
            k for k, v in self._cache.items()
            if v.get("session_id") == session_id
        ]
        for k in keys_to_remove:
            del self._cache[k]
        if keys_to_remove:
            logger.info(f"Cache cleared {len(keys_to_remove)} entries for session {session_id}")

    def clear(self):
        self._cache.clear()

    def stats(self) -> Dict[str, Any]:
        return {"size": len(self._cache), "max_size": self._max_size, "ttl": self._ttl}


query_cache = QueryCache()


# ─── Result Summarization ─────────────────────────────────────────────────────

def summarize_results(
    rows: List[Dict[str, Any]],
    columns: List[str],
    query: str,
    primary_service: str = "",
    entity_set: str = "",
) -> str:
    """Generate a natural language summary of query results."""
    if not rows:
        return "No results found."

    n = len(rows)
    numeric_cols = _get_numeric_columns(rows, columns)
    categorical_cols = [c for c in columns if c not in numeric_cols]

    parts = []

    # Basic count
    parts.append(f"**{n}** records returned")

    if primary_service:
        parts[-1] += f" from **{primary_service}**"
    if entity_set:
        parts[-1] += f" ({entity_set})"

    # Numeric summaries
    for col in numeric_cols[:3]:
        vals = [float(r[col]) for r in rows if r.get(col) is not None and _is_numeric(r[col])]
        if not vals:
            continue
        avg = sum(vals) / len(vals)
        mn, mx = min(vals), max(vals)
        total = sum(vals)
        if total > 0 and col.lower() in ("quantity", "amount", "price", "unitprice", "total", "revenue", "sales", "cost"):
            parts.append(f"**{col}**: total = **{_fmt_num(total)}**, avg = {_fmt_num(avg)}")
        else:
            parts.append(f"**{col}**: range {_fmt_num(mn)} – {_fmt_num(mx)}, avg = {_fmt_num(avg)}")

    # Categorical summaries
    for col in categorical_cols[:2]:
        vals = [str(r[col]) for r in rows if r.get(col) is not None]
        if not vals:
            continue
        unique_count = len(set(vals))
        if unique_count <= 20:
            value_counts = {}
            for v in vals:
                value_counts[v] = value_counts.get(v, 0) + 1
            top3 = sorted(value_counts.items(), key=lambda x: -x[1])[:3]
            top_str = ", ".join(f"**{k}** ({v})" for k, v in top3)
            parts.append(f"**{col}**: {unique_count} unique values — top: {top_str}")
        else:
            parts.append(f"**{col}**: {unique_count} unique values")

    # Trend detection for time-like columns
    for col in columns:
        if any(kw in col.lower() for kw in ("date", "time", "year", "month", "day")):
            vals = [str(r[col]) for r in rows if r.get(col) is not None]
            if len(vals) >= 2:
                parts.append(f"Data spans **{vals[-1]}** to **{vals[0]}**")

    return ". ".join(parts) + "."


def _get_numeric_columns(rows: List[Dict], columns: List[str]) -> List[str]:
    numeric = []
    for c in columns:
        sample = [r.get(c) for r in rows[:20] if r.get(c) is not None]
        if sample and sum(1 for v in sample if _is_numeric(v)) > len(sample) * 0.6:
            numeric.append(c)
    return numeric


def _is_numeric(v: Any) -> bool:
    if isinstance(v, (int, float)):
        return True
    if isinstance(v, str):
        try:
            float(v.replace(",", ""))
            return True
        except (ValueError, TypeError):
            return False
    return False


def _fmt_num(v: float) -> str:
    if v == int(v):
        return f"{int(v):,}"
    return f"{v:,.2f}"


# ─── Auto Chart Recommendations ───────────────────────────────────────────────

def recommend_charts(
    rows: List[Dict[str, Any]],
    columns: List[str],
    query: str,
) -> List[Dict[str, Any]]:
    """Recommend chart types based on data shape. Returns list of {type, title, config}."""
    if not rows or len(rows) < 2:
        return []

    recommendations = []
    numeric_cols = _get_numeric_columns(rows, columns)
    categorical_cols = [c for c in columns if c not in numeric_cols and not c.startswith("@odata")]

    # 1. Bar chart: categorical key + numeric value
    for cat_col in categorical_cols[:2]:
        for num_col in numeric_cols[:2]:
            unique_cats = set(str(r.get(cat_col, "")) for r in rows if r.get(cat_col) is not None)
            if 2 <= len(unique_cats) <= 30:
                recommendations.append({
                    "type": "bar",
                    "title": f"{num_col} by {cat_col}",
                    "x_col": cat_col,
                    "y_col": num_col,
                    "confidence": "high" if len(unique_cats) <= 15 else "medium",
                })

    # 2. Pie/Donut: categorical with counts (small cardinality)
    for cat_col in categorical_cols[:2]:
        unique_cats = set(str(r.get(cat_col, "")) for r in rows if r.get(cat_col) is not None)
        if 2 <= len(unique_cats) <= 8:
            # Check if there's a natural count or numeric
            for num_col in numeric_cols[:1]:
                recommendations.append({
                    "type": "pie",
                    "title": f"Distribution of {num_col} by {cat_col}",
                    "label_col": cat_col,
                    "value_col": num_col,
                    "confidence": "high" if len(unique_cats) <= 6 else "medium",
                })
            # If no numeric, use count
            if not numeric_cols:
                recommendations.append({
                    "type": "pie",
                    "title": f"Count by {cat_col}",
                    "label_col": cat_col,
                    "value_col": "__count__",
                    "confidence": "high",
                })

    # 3. Line chart: time-like column + numeric
    for col in columns:
        if any(kw in col.lower() for kw in ("date", "time", "year", "month", "day")):
            for num_col in numeric_cols[:1]:
                recommendations.append({
                    "type": "line",
                    "title": f"{num_col} over {col}",
                    "x_col": col,
                    "y_col": num_col,
                    "confidence": "medium",
                })

    # 4. Scatter: two numeric columns
    if len(numeric_cols) >= 2:
        recommendations.append({
            "type": "scatter",
            "title": f"{numeric_cols[1]} vs {numeric_cols[0]}",
            "x_col": numeric_cols[0],
            "y_col": numeric_cols[1],
            "confidence": "medium",
        })

    # 5. Table summary: if too many columns
    if len(columns) > 10:
        recommendations.insert(0, {
            "type": "table",
            "title": f"Summary table ({len(columns)} columns, {len(rows)} rows)",
            "confidence": "high",
        })

    # Sort by confidence
    conf_order = {"high": 0, "medium": 1, "low": 2}
    recommendations.sort(key=lambda r: conf_order.get(r.get("confidence", "low"), 2))

    return recommendations[:5]


# ─── Query Suggestions ────────────────────────────────────────────────────────

def generate_suggestions(
    services: List[Dict[str, Any]],
    recent_queries: List[str] = None,
) -> List[Dict[str, str]]:
    """Generate query suggestions based on available services and entities."""
    suggestions = []

    for svc in services:
        svc_id = svc.get("id", "")
        svc_name = svc.get("name", svc_id)
        entity_sets = svc.get("entity_sets", [])

        for es in entity_sets:
            # Skip views/computed entities
            es_lower = es.lower()
            if any(v in es_lower for v in ("view", "summary", "list", "by_", "for_", "extended", "subtotal")):
                continue

            # Get columns for this entity
            entity_props = svc.get("entity_properties", {})
            cols = entity_props.get(es, [])

            if not cols:
                suggestions.append({
                    "query": f"Show me all {es} from {svc_name}",
                    "description": f"Fetch all records from {es}",
                    "service": svc_id,
                    "entity": es,
                })
                continue

            numeric_cols = [c for c in cols if any(kw in c.lower() for kw in ("price", "cost", "amount", "quantity", "stock", "total", "id"))]
            categorical_cols = [c for c in cols if c not in numeric_cols and not c.startswith("@odata") and c.lower() not in ("id", "description", "notes")]

            # Basic fetch
            suggestions.append({
                "query": f"Show me all {es} from {svc_name}",
                "description": f"Fetch all records from {es}",
                "service": svc_id,
                "entity": es,
            })

            # Count by category
            for cat in categorical_cols[:2]:
                suggestions.append({
                    "query": f"Count {es} by {cat}",
                    "description": f"Group {es} by {cat} and count",
                    "service": svc_id,
                    "entity": es,
                })

            # Top/bottom by numeric
            for num in numeric_cols[:1]:
                suggestions.append({
                    "query": f"Show top 10 {es} by {num}",
                    "description": f"Top 10 {es} sorted by {num}",
                    "service": svc_id,
                    "entity": es,
                })

            # Aggregation
            for num in numeric_cols[:1]:
                suggestions.append({
                    "query": f"What is the total {num} of {es}",
                    "description": f"Sum of {num} across all {es}",
                    "service": svc_id,
                    "entity": es,
                })

    # Add recent queries at the top
    if recent_queries:
        for q in reversed(recent_queries[-5:]):
            suggestions.insert(0, {"query": q, "description": "Recent query", "service": "", "entity": ""})

    return suggestions[:30]


# ─── Drill-Down Metadata ──────────────────────────────────────────────────────

def get_drill_down_links(
    entity_set: str,
    row: Dict[str, Any],
    services: List[Dict[str, Any]],
) -> List[Dict[str, str]]:
    """Suggest drill-down links based on entity relationships.
    Uses entity_sets name patterns and row columns to infer relationships."""
    links = []
    row_keys = set(row.keys())
    # Find ID-like columns in the current row (foreign keys)
    fk_columns = [k for k in row_keys if k.lower().endswith("id") and k.lower() != f"{entity_set.lower()}id"]

    for svc in services:
        svc_id = svc.get("id", "")
        entity_sets = svc.get("entity_sets", [])
        entity_props = svc.get("entity_properties", {})

        for other_es in entity_sets:
            if other_es == entity_set:
                continue

            # Method 1: Use entity_properties if available
            other_cols = set(entity_props.get(other_es, [])) if entity_props else set()

            # Method 2: Heuristic — entity names that share a prefix or contain FK columns
            other_lower = other_es.lower()
            shared_keys = []
            for fk in fk_columns:
                fk_base = fk.lower().replace("id", "")
                # Check if entity name contains the FK base (e.g. CustomerID → Customers)
                if fk_base and fk_base in other_lower:
                    shared_keys.append(fk)
                # Check if entity name + "id" matches
                elif other_lower.endswith("s") and fk.lower() == other_lower[:-1] + "id":
                    shared_keys.append(fk)
                elif fk.lower() == other_lower + "id":
                    shared_keys.append(fk)

            if shared_keys:
                key = shared_keys[0]
                val = row.get(key)
                if val is not None:
                    links.append({
                        "query": f"Show me {other_es} where {key} is {val}",
                        "description": f"Drill into {other_es} (via {key})",
                        "service": svc_id,
                        "entity": other_es,
                        "via_column": key,
                        "via_value": str(val),
                    })

    return links[:5]


def format_drill_down_links(links: List[Dict[str, str]]) -> str:
    """Format drill-down links as clickable chips for the frontend."""
    if not links:
        return ""
    chips = []
    for link in links:
        chips.append(
            f'<button class="drill-chip" onclick="sendQuery(\'{link["query"]}\')" '
            f'title="{link["description"]}">{link["entity"]} → {link["via_column"]}={link["via_value"]}</button>'
        )
    return '<div class="drill-down-container">' + "".join(chips) + "</div>"
