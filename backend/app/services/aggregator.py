"""
Post-fetch aggregation engine for OData results.
Handles GROUP BY, COUNT, SUM, AVG, MIN, MAX on fetched data.
"""
import re
from typing import Any, Dict, List, Optional, Tuple
from collections import defaultdict
from loguru import logger


AGGREGATION_PATTERNS = [
    (r'\bcount\b.*\bper\b\s+(\w+)', "count", None),
    (r'\bcount\b.*\beach\b\s+(\w+)', "count", None),
    (r'\bhow many\b.*\bper\b\s+(\w+)', "count", None),
    (r'\bhow many\b.*\beach\b\s+(\w+)', "count", None),
    (r'\bhow many\b.*\bin\b\s+(\w+)', "count", None),
    (r'\bnumber of\b.*\bper\b\s+(\w+)', "count", None),
    (r'\bcount\b.*\bby\b\s+(\w+)', "count", None),
    (r'\btotal\b\s+(\w+).*\bper\b\s+(\w+)', "sum", None),
    (r'\bsum\b.*\bper\b\s+(\w+)', "sum", None),
    (r'\baverage\b\s+(\w+).*\bper\b\s+(\w+)', "avg", None),
    (r'\bavg\b.*\bper\b\s+(\w+)', "avg", None),
    (r'\bminimum\b\s+(\w+).*\bper\b\s+(\w+)', "min", None),
    (r'\bmin\b.*\bper\b\s+(\w+)', "min", None),
    (r'\bmaximum\b\s+(\w+).*\bper\b\s+(\w+)', "max", None),
    (r'\bmax\b.*\bper\b\s+(\w+)', "max", None),
]

SIMPLE_COUNT_PATTERNS = [
    (r'\bcount\b.*\bcustomers?\b', "count", "CustomerID"),
    (r'\bcount\b.*\borders?\b', "count", "OrderID"),
    (r'\bcount\b.*\bproducts?\b', "count", "ProductID"),
    (r'\bcount\b.*\bemployees?\b', "count", "EmployeeID"),
    (r'\bcount\b.*\bsuppliers?\b', "count", "SupplierID"),
    (r'\bcount\b.*\bcategories?\b', "count", "CategoryID"),
    (r'\bhow many\b.*\bcustomers?\b', "count", "CustomerID"),
    (r'\bhow many\b.*\borders?\b', "count", "OrderID"),
    (r'\bhow many\b.*\bproducts?\b', "count", "ProductID"),
    (r'\bhow many\b.*\bemployees?\b', "count", "EmployeeID"),
    (r'\bnumber of\b.*\bcustomers?\b', "count", "CustomerID"),
    (r'\bnumber of\b.*\borders?\b', "count", "OrderID"),
]

PERCENTAGE_PATTERNS = [
    (r'percentage.*(?:customers?|people|users?).*per\s+(\w+)', "count", None),
    (r'percentage.*(?:customers?|people|users?).*in\s+(\w+)', "count", None),
    (r'percentage.*(?:customers?|people|users?).*of\s+(\w+)', "count", None),
    (r'(?:what|show)\s+percentage.*(\w+)', "count", None),
    (r'percentage.*(\w+)\s+(?:and|vs|compare)', "count", None),
    (r'compare.*(\w+).*and.*(\w+)', "count", None),
    (r'which.*(?:has|have).*(?:least|fewest|most|highest|lowest).*in\s+(\w+)', "count", None),
    (r'which.*(?:country|city|category).*has.*(?:least|fewest|most|highest|lowest)', "count", None),
    (r'what.*(?:country|city|category).*has.*(?:least|fewest|most|highest|lowest)', "count", None),
    (r'(?:show|find|get).*(?:least|fewest|most|highest|lowest)', "count", None),
]

COUNTRY_NAMES = {
    "germany", "france", "usa", "uk", "brazil", "spain", "mexico", "canada",
    "italy", "austria", "belgium", "denmark", "finland", "ireland", "norway",
    "poland", "portugal", "sweden", "switzerland", "venezuela", "argentina",
    "china", "japan", "india", "australia", "russia",
}


def _is_country(name: str) -> bool:
    return name.lower().strip() in COUNTRY_NAMES


def detect_aggregation(query: str) -> Optional[Dict[str, Any]]:
    """Detect if the user query requires post-fetch aggregation."""
    q = query.lower().strip()

    for pattern, func, group_col in AGGREGATION_PATTERNS:
        m = re.search(pattern, q)
        if m:
            groups = m.groups()
            if len(groups) == 1:
                return {"func": func, "group_by": groups[0], "agg_col": None}
            elif len(groups) == 2:
                return {"func": func, "agg_col": groups[0], "group_by": groups[1]}

    for pattern, func, count_col in SIMPLE_COUNT_PATTERNS:
        if re.search(pattern, q):
            return {"func": func, "group_by": None, "agg_col": count_col}

    for pattern, func, count_col in PERCENTAGE_PATTERNS:
        m = re.search(pattern, q)
        if m:
            groups = m.groups()
            group_name = groups[0] if groups else None
            if group_name and (_is_country(group_name) or group_name in ("percentage", "the", "that", "this", "and", "or", "vs", "compare")):
                group_name = None
            return {"func": func, "group_by": group_name or "Country", "agg_col": count_col}

    return None


def _find_column(columns: List[str], name: str) -> Optional[str]:
    """Find the best matching column name (case-insensitive, partial match).
    Prefers name/text columns over ID columns."""
    name_lower = name.lower().strip()
    id_cols = {"id", "orderid", "customerid", "productid", "employeeid", "supplierid", "categoryid", "territoryid"}

    for c in columns:
        if c.lower() == name_lower:
            return c

    name_matches = []
    for c in columns:
        cl = c.lower()
        if name_lower in cl or cl.startswith(name_lower):
            if cl not in id_cols and not cl.endswith("id"):
                name_matches.append(c)
    if name_matches:
        return name_matches[0]

    for c in columns:
        cl = c.lower()
        if name_lower in cl or cl.startswith(name_lower):
            return c

    return None


def _find_numeric_column(rows: List[Dict], columns: List[str], hint: Optional[str] = None) -> Optional[str]:
    """Find a suitable numeric column for aggregation."""
    if hint:
        col = _find_column(columns, hint)
        if col:
            try:
                float(rows[0].get(col, 0))
                return col
            except (ValueError, TypeError):
                pass

    for c in columns:
        if c.lower() in ("id", "orderid", "customerid", "productid", "employeeid", "supplierid"):
            continue
        try:
            vals = [float(r.get(c, 0)) for r in rows if r.get(c) is not None]
            if vals and all(isinstance(v, (int, float)) for v in vals):
                return c
        except (ValueError, TypeError):
            continue
    return None


def aggregate(rows: List[Dict], columns: List[str], agg_info: Dict[str, Any]) -> Dict[str, Any]:
    """Perform aggregation on fetched rows.

    Returns: {"columns": [...], "rows": [...], "row_count": N, "truncated": False, "total_count": N}
    """
    func = agg_info["func"]
    group_col_name = agg_info.get("group_by")
    agg_col_name = agg_info.get("agg_col")

    group_col = _find_column(columns, group_col_name) if group_col_name else None
    agg_col = _find_column(columns, agg_col_name) if agg_col_name else None

    if not agg_col and func != "count":
        agg_col = _find_numeric_column(rows, columns)

    if func == "count" and not group_col:
        result_row = {f"total_{func}": len(rows)}
        return {
            "columns": list(result_row.keys()),
            "rows": [result_row],
            "row_count": 1,
            "truncated": False,
            "total_count": 1,
        }

    if group_col:
        groups = defaultdict(list)
        for r in rows:
            key = r.get(group_col, "N/A")
            groups[key].append(r)

        result_rows = []
        for key, group_rows in sorted(groups.items(), key=lambda x: str(x[0])):
            row = {group_col: key}
            if func == "count":
                row[f"count"] = len(group_rows)
            elif func == "sum" and agg_col:
                vals = [float(r.get(agg_col, 0)) for r in group_rows if r.get(agg_col) is not None]
                row[f"sum_{agg_col}"] = round(sum(vals), 2)
            elif func == "avg" and agg_col:
                vals = [float(r.get(agg_col, 0)) for r in group_rows if r.get(agg_col) is not None]
                row[f"avg_{agg_col}"] = round(sum(vals) / len(vals), 2) if vals else 0
            elif func == "min" and agg_col:
                vals = [float(r.get(agg_col, 0)) for r in group_rows if r.get(agg_col) is not None]
                row[f"min_{agg_col}"] = min(vals) if vals else 0
            elif func == "max" and agg_col:
                vals = [float(r.get(agg_col, 0)) for r in group_rows if r.get(agg_col) is not None]
                row[f"max_{agg_col}"] = max(vals) if vals else 0
            else:
                row["count"] = len(group_rows)
            result_rows.append(row)

        result_rows.sort(key=lambda r: list(r.values())[-1] if len(r) > 1 else 0, reverse=True)

        agg_columns = list(result_rows[0].keys()) if result_rows else []
        return {
            "columns": agg_columns,
            "rows": result_rows,
            "row_count": len(result_rows),
            "truncated": False,
            "total_count": len(result_rows),
        }

    if func == "count":
        return {
            "columns": ["count"],
            "rows": [{"count": len(rows)}],
            "row_count": 1,
            "truncated": False,
            "total_count": 1,
        }

    return {
        "columns": columns,
        "rows": rows[:50],
        "row_count": len(rows),
        "truncated": len(rows) > 50,
        "total_count": len(rows),
    }
