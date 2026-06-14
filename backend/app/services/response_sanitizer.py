"""Sanitizes raw OData responses for safe LLM consumption and tabular display."""
from typing import Any, Dict, List


SENSITIVE_KEYS = {"password", "pwd", "secret", "token", "apikey", "api_key", "authorization", "creditcard", "ssn"}

# Columns to exclude from output (OData metadata)
EXCLUDE_COLUMNS = {"@odata.etag", "odata.etag"}


def _scrub(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if any(s in str(k).lower() for s in SENSITIVE_KEYS):
                out[k] = "***REDACTED***"
            else:
                out[k] = _scrub(v)
        return out
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def sanitize(odata_payload: Dict[str, Any], max_rows: int = 50) -> Dict[str, Any]:
    scrubbed = _scrub(odata_payload)
    rows = scrubbed.get("value", []) if isinstance(scrubbed, dict) else []
    if not isinstance(rows, list):
        rows = []
    truncated = rows[:max_rows]
    columns: List[str] = []
    for r in truncated:
        if isinstance(r, dict):
            for k in r.keys():
                if k not in columns and k not in EXCLUDE_COLUMNS:
                    columns.append(k)
    # Remove excluded columns from rows
    cleaned_rows = []
    for r in truncated:
        if isinstance(r, dict):
            cleaned_rows.append({k: v for k, v in r.items() if k not in EXCLUDE_COLUMNS})
        else:
            cleaned_rows.append(r)
    return {
        "columns": columns,
        "rows": cleaned_rows,
        "row_count": len(rows),
        "truncated": len(rows) > max_rows,
        "total_count": scrubbed.get("@odata.count") if isinstance(scrubbed, dict) else None,
    }
