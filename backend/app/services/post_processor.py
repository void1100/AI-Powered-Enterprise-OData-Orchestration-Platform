"""
Post-aggregation computation engine.
Handles percentage, comparison, ratio, and multi-step calculations
on already-aggregated results.
"""
import re
from typing import Any, Dict, List, Optional, Tuple
from loguru import logger

STOP_WORDS = {"it", "the", "this", "that", "with", "and", "or", "in", "of", "from", "to", "a", "an", "is", "are", "was", "were", "be", "been", "being", "have", "has", "had", "do", "does", "did", "will", "would", "could", "should", "may", "might", "shall", "can", "which", "what", "show", "get", "list", "find", "calculate", "compare"}


def _is_stop_word(word: str) -> bool:
    return word.lower().strip() in STOP_WORDS


def detect_post_processing(query: str) -> Optional[Dict[str, Any]]:
    """Detect if the query needs post-aggregation computation."""
    q = query.lower().strip()

    from app.services.aggregator import COUNTRY_NAMES
    mentioned = [c for c in COUNTRY_NAMES if c in q]

    has_percentage = bool(re.search(r'percent|percentage|%', q))
    has_compare = bool(re.search(r'compare|comparison|versus|vs', q)) or len(mentioned) >= 2
    has_extremum = bool(re.search(r'least|fewest|lowest|minimum|most|highest|greatest|maximum', q))
    has_ratio = bool(re.search(r'ratio', q))

    if has_compare and len(mentioned) >= 2:
        return {"type": "comparison", "compare_groups": mentioned[:2]}

    if has_percentage:
        return {"type": "percentage", "target_groups": mentioned}

    if has_extremum:
        extremum = "min" if re.search(r'least|fewest|lowest|minimum|min', q) else "max"
        return {"type": "extremum", "extremum": extremum}

    if has_ratio:
        m = re.search(r'ratio\s+(?:of\s+)?(\w+)\s+(?:to|and|versus|vs)\s+(\w+)', q)
        if m:
            return {"type": "ratio", "ratio_groups": list(m.groups())}

    return None


def post_process(
    rows: List[Dict],
    columns: List[str],
    pp_info: Dict[str, Any],
    original_query: str = "",
) -> Dict[str, Any]:
    """Perform post-aggregation computation."""
    pp_type = pp_info.get("type", "")

    if pp_type == "percentage":
        return _compute_percentage(rows, columns, pp_info)
    elif pp_type == "comparison":
        return _compute_comparison(rows, columns, pp_info)
    elif pp_type in ("which_extremum", "extremum"):
        return _compute_extremum(rows, columns, pp_info)
    elif pp_type == "ratio":
        return _compute_ratio(rows, columns, pp_info)
    else:
        return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": False, "total_count": len(rows)}


def _find_value_column(columns: List[str]) -> Optional[str]:
    """Find the numeric/count column in aggregated results."""
    for c in columns:
        cl = c.lower()
        if cl in ("count", "total", "sum", "avg", "min", "max", "total_count"):
            return c
    for c in columns:
        if c.lower().endswith("count") or c.lower().startswith("sum_") or c.lower().startswith("avg_"):
            return c
    return columns[-1] if columns else None


def _compute_percentage(rows: List[Dict], columns: List[str], pp_info: Dict[str, Any]) -> Dict[str, Any]:
    """Compute percentages for each group."""
    group_col = columns[0] if columns else None
    value_col = _find_value_column(columns)

    if not group_col or not value_col or not rows:
        return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": False, "total_count": len(rows)}

    total = sum(float(r.get(value_col, 0)) for r in rows)
    if total == 0:
        total = 1

    result_rows = []
    for r in rows:
        val = float(r.get(value_col, 0))
        pct = round((val / total) * 100, 2)
        result_rows.append({
            group_col: r.get(group_col),
            "count": int(val),
            "percentage": pct,
        })

    result_rows.sort(key=lambda x: x.get("percentage", 0), reverse=True)

    return {
        "columns": [group_col, "count", "percentage"],
        "rows": result_rows,
        "row_count": len(result_rows),
        "truncated": False,
        "total_count": len(result_rows),
    }


def _compute_comparison(rows: List[Dict], columns: List[str], pp_info: Dict[str, Any]) -> Dict[str, Any]:
    """Compare two specific groups side by side."""
    compare_groups = pp_info.get("compare_groups", [])
    group_col = columns[0] if columns else None
    value_col = _find_value_column(columns)

    if not group_col or not value_col or not rows:
        return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": False, "total_count": len(rows)}

    group_map = {str(r.get(group_col, "")).lower(): r for r in rows}

    matched = []
    for g in compare_groups:
        for key, row in group_map.items():
            if g.lower() == key or g.lower() in key or key in g.lower():
                matched.append(row)
                break

    if len(matched) < 2:
        matched = rows[:2]

    total = sum(float(r.get(value_col, 0)) for r in rows) or 1

    result_rows = []
    for r in matched:
        val = float(r.get(value_col, 0))
        pct = round((val / total) * 100, 2)
        result_rows.append({
            group_col: r.get(group_col),
            "count": int(val),
            "percentage": pct,
        })

    if len(result_rows) >= 2:
        v0 = float(result_rows[0].get("count", 0))
        v1 = float(result_rows[1].get("count", 0))
        diff = abs(v0 - v1)
        pct_diff = round(abs(result_rows[0].get("percentage", 0) - result_rows[1].get("percentage", 0)), 2)
        winner = result_rows[0] if v0 > v1 else result_rows[1] if v1 > v0 else None
        loser = result_rows[1] if v0 > v1 else result_rows[0] if v1 > v0 else None

        summary_row = {
            "comparison": f"{result_rows[0][group_col]} vs {result_rows[1][group_col]}",
            "difference": int(diff),
            "percentage_diff": pct_diff,
            "larger": winner.get(group_col) if winner else "Equal",
            "smaller": loser.get(group_col) if loser else "Equal",
        }
        result_rows.append(summary_row)

    return {
        "columns": list(result_rows[0].keys()) if result_rows else columns,
        "rows": result_rows,
        "row_count": len(result_rows),
        "truncated": False,
        "total_count": len(result_rows),
    }


def _compute_extremum(rows: List[Dict], columns: List[str], pp_info: Dict[str, Any]) -> Dict[str, Any]:
    """Find the group with min or max value."""
    extremum = pp_info.get("extremum", "min")
    group_col = columns[0] if columns else None
    value_col = _find_value_column(columns)

    if not group_col or not value_col or not rows:
        return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": False, "total_count": len(rows)}

    total = sum(float(r.get(value_col, 0)) for r in rows) or 1

    result_rows = []
    for r in rows:
        val = float(r.get(value_col, 0))
        pct = round((val / total) * 100, 2)
        result_rows.append({
            group_col: r.get(group_col),
            "count": int(val),
            "percentage": pct,
        })

    if extremum == "min":
        result_rows.sort(key=lambda x: x.get("count", 0))
    else:
        result_rows.sort(key=lambda x: x.get("count", 0), reverse=True)

    winner = result_rows[0] if result_rows else None
    if winner:
        result_rows.append({
            "result": f"{'Least' if extremum == 'min' else 'Most'}: {winner[group_col]} with {winner['count']} ({winner['percentage']}%)",
        })

    return {
        "columns": list(result_rows[0].keys()) if result_rows else columns,
        "rows": result_rows,
        "row_count": len(result_rows),
        "truncated": False,
        "total_count": len(result_rows),
    }


def _compute_ratio(rows: List[Dict], columns: List[str], pp_info: Dict[str, Any]) -> Dict[str, Any]:
    """Compute ratio between two groups."""
    ratio_groups = pp_info.get("ratio_groups", [])
    group_col = columns[0] if columns else None
    value_col = _find_value_column(columns)

    if not group_col or not value_col or not rows:
        return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": False, "total_count": len(rows)}

    group_map = {str(r.get(group_col, "")).lower(): r for r in rows}

    matched = []
    for g in ratio_groups:
        for key, row in group_map.items():
            if g.lower() in key or key in g.lower():
                matched.append(row)
                break

    if len(matched) < 2:
        return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": False, "total_count": len(rows)}

    v0 = float(matched[0].get(value_col, 0))
    v1 = float(matched[1].get(value_col, 0))

    ratio = round(v0 / v1, 2) if v1 > 0 else "N/A"
    inverse_ratio = round(v1 / v0, 2) if v0 > 0 else "N/A"

    result_rows = [
        {group_col: matched[0].get(group_col), "count": int(v0)},
        {group_col: matched[1].get(group_col), "count": int(v1)},
        {"ratio": f"{ratio_groups[0]}:{ratio_groups[1]} = {ratio}", "inverse": f"{ratio_groups[1]}:{ratio_groups[0]} = {inverse_ratio}"},
    ]

    return {
        "columns": list(result_rows[0].keys()),
        "rows": result_rows,
        "row_count": len(result_rows),
        "truncated": False,
        "total_count": len(result_rows),
    }
