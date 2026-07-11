"""Guardrail Engine — controls AI write operations with input/output guards.

Every write operation (create/update/delete) passes through:
  1. Input guards (before execution)
  2. OData write call
  3. Output guards (after execution)

Guards return GuardResult(allow, reason, metadata).
"""
import re
import html
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set
from loguru import logger


@dataclass
class GuardResult:
    allow: bool
    reason: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


# ─── Rate Limiting ───────────────────────────────────────────────────────────
_write_timestamps: Dict[str, List[float]] = {}
RATE_LIMIT_WINDOW = 60  # seconds
RATE_LIMIT_MAX = 10     # writes per window per user


def _check_rate_limit(user_id: str) -> GuardResult:
    now = time.time()
    timestamps = _write_timestamps.get(user_id, [])
    # Remove old timestamps
    timestamps = [t for t in timestamps if now - t < RATE_LIMIT_WINDOW]
    if len(timestamps) >= RATE_LIMIT_MAX:
        return GuardResult(False, f"Rate limit: max {RATE_LIMIT_MAX} writes per minute")
    timestamps.append(now)
    _write_timestamps[user_id] = timestamps
    return GuardResult(True)


# ─── RBAC Guard ──────────────────────────────────────────────────────────────
ROLES_ALLOWED_TO_WRITE = {"super_admin", "admin", "editor"}


def _check_rbac(user_role: str) -> GuardResult:
    # Case-insensitive role check
    normalized_role = user_role.lower().replace(" ", "_")
    if normalized_role not in ROLES_ALLOWED_TO_WRITE:
        return GuardResult(False, f"Role '{user_role}' cannot perform writes. Required: {ROLES_ALLOWED_TO_WRITE}")
    return GuardResult(True)


# ─── Entity Whitelist Guard ──────────────────────────────────────────────────
# Entities that allow writes (empty = all entities allowed)
ENTITY_WRITE_WHITELIST: Set[str] = set()
ENTITY_WRITE_BLACKLIST: Set[str] = set()


def _check_entity_whitelist(entity_set: str) -> GuardResult:
    if entity_set in ENTITY_WRITE_BLACKLIST:
        return GuardResult(False, f"Entity '{entity_set}' is blacklisted for writes")
    if ENTITY_WRITE_WHITELIST and entity_set not in ENTITY_WRITE_WHITELIST:
        return GuardResult(False, f"Entity '{entity_set}' is not in write whitelist")
    return GuardResult(True)


# ─── Field Validation Guard ──────────────────────────────────────────────────
DANGEROUS_PATTERNS = [
    r"<script", r"javascript:", r"on\w+\s*=",  # XSS
    r";\s*DROP\b", r";\s*DELETE\b", r";\s*UPDATE\b", r";\s*INSERT\b",  # SQL injection
    r"\$\{",  # Template injection
]


def _sanitize_value(value: Any) -> Any:
    """Sanitize a single value."""
    if isinstance(value, str):
        # Strip HTML tags
        value = re.sub(r"<[^>]+>", "", value)
        # Escape HTML entities
        value = html.escape(value)
        # Strip null bytes
        value = value.replace("\x00", "")
        # Limit length
        if len(value) > 1000:
            value = value[:1000]
    return value


def _check_field_values(fields: Dict[str, Any]) -> GuardResult:
    for key, value in fields.items():
        if isinstance(value, str):
            for pattern in DANGEROUS_PATTERNS:
                if re.search(pattern, value, re.IGNORECASE):
                    return GuardResult(False, f"Potentially dangerous value in field '{key}'")
        # Type sanity: keys shouldn't contain special chars
        if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", key):
            return GuardResult(False, f"Invalid field name: '{key}'")
    return GuardResult(True)


# ─── Required Fields Guard ───────────────────────────────────────────────────
def _check_required_fields(
    fields: Dict[str, Any],
    required_fields: List[str],
) -> GuardResult:
    missing = [f for f in required_fields if f not in fields or fields[f] in (None, "")]
    if missing:
        return GuardResult(False, f"Missing required fields: {', '.join(missing)}")
    return GuardResult(True)


# ─── Confirmation Gate ───────────────────────────────────────────────────────
def _check_confirmation(confirmed: bool) -> GuardResult:
    if not confirmed:
        return GuardResult(False, "User has not confirmed the operation")
    return GuardResult(True)


# ─── Output Guards ───────────────────────────────────────────────────────────
SENSITIVE_FIELDS = {"password", "secret", "token", "api_key", "auth_token", "credential"}


def _guard_output_response(response: Dict[str, Any]) -> Dict[str, Any]:
    """Sanitize output response — remove sensitive/internal fields."""
    if not isinstance(response, dict):
        return response

    cleaned = {}
    for k, v in response.items():
        k_lower = k.lower()
        # Remove OData internals
        if k.startswith("@odata.") or k == "odata.etag":
            continue
        # Remove sensitive fields
        if any(s in k_lower for s in SENSITIVE_FIELDS):
            cleaned[k] = "***REDACTED***"
            continue
        cleaned[k] = v
    return cleaned


def _guard_error_message(error: str) -> str:
    """Mask internal error details from user."""
    # Hide internal IPs, stack traces, SAP technical details
    error = re.sub(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}", "[INTERNAL]", error)
    error = re.sub(r"Traceback.*", "Internal error occurred", error, flags=re.DOTALL)
    error = re.sub(r"Exception:.*", "Operation failed", error, flags=re.DOTALL)
    # Limit length
    if len(error) > 200:
        error = error[:200] + "..."
    return error


# ─── Public API ──────────────────────────────────────────────────────────────
def run_input_guards(
    user_role: str,
    user_id: str,
    entity_set: str,
    operation: str,
    fields: Dict[str, Any],
    required_fields: List[str] = None,
    confirmed: bool = False,
) -> GuardResult:
    """Run all input guards. Returns first failure or success."""
    guards = [
        ("rbac", lambda: _check_rbac(user_role)),
        ("rate_limit", lambda: _check_rate_limit(user_id)),
        ("entity_whitelist", lambda: _check_entity_whitelist(entity_set)),
        ("field_values", lambda: _check_field_values(fields)),
    ]

    if required_fields:
        guards.append(("required_fields", lambda: _check_required_fields(fields, required_fields)))

    if operation in ("create", "update", "delete"):
        gates = [
            ("confirmation", lambda: _check_confirmation(confirmed)),
        ]
        guards.extend(gates)

    for name, fn in guards:
        result = fn()
        if not result.allow:
            logger.warning(f"Guard '{name}' blocked {operation} on {entity_set}: {result.reason}")
            result.metadata["guard"] = name
            return result

    logger.info(f"All input guards passed for {operation} on {entity_set}")
    return GuardResult(True, metadata={"guard": "all_passed"})


def run_output_guards(
    response: Dict[str, Any],
    operation: str,
) -> Dict[str, Any]:
    """Run output guards on response. Returns sanitized response."""
    if response.get("error"):
        response["error"] = _guard_error_message(response["error"])
    if "table" in response and response["table"]:
        if "rows" in response["table"]:
            response["table"]["rows"] = [
                _guard_output_response(row) for row in response["table"]["rows"]
            ]
    return response


def build_write_summary(
    operation: str,
    entity_set: str,
    service_id: str,
    fields: Dict[str, Any],
    missing_fields: Optional[List[str]] = None,
) -> str:
    """Build a human-readable summary of the write operation."""
    if operation == "create":
        lines = [f"**Create** new record in `{entity_set}` ({service_id})"]
    elif operation == "update":
        lines = [f"**Update** record in `{entity_set}` ({service_id})"]
    elif operation == "delete":
        lines = [f"**Delete** record from `{entity_set}` ({service_id})"]
    else:
        lines = [f"**{operation.title()}** on `{entity_set}` ({service_id})"]

    if fields:
        lines.append("\n**Fields:**")
        for k, v in fields.items():
            lines.append(f"- `{k}`: {v}")

    if missing_fields:
        lines.append(f"\n**Required fields missing:** {', '.join(missing_fields)}")
        lines.append("Please provide these values in the form below.")

    return "\n".join(lines)


# ─── Entity Field Requirements ────────────────────────────────────────────────
# SAP CPI key fields that are auto-generated and should not be user-provided
AUTO_GENERATED_FIELDS = {
    "InternalId", "PurchaseOrder", "PurchaseOrderItem", "OrderID", "OrderItemID",
    "UUID", "CreatedAt", "ChangedAt", "CreatedBy", "ChangedBy",
    "CreationDate", "LastChangeDateTime", "LastChangeDate",
}

# SAP CPI fields that are typically required for creation
SAP_REQUIRED_FIELDS = {
    "PurchaseOrder": ["PurchaseOrderType", "CompanyCode", "PurchasingOrganization", "PurchasingGroup", "Supplier"],
    "PurchaseOrderItem": ["PurchaseOrder", "PurchaseOrderItem", "Material", "Plant"],
    "SalesOrder": ["SalesOrganization", "DistributionChannel", "Division", "SoldToParty"],
    "BusinessPartner": ["BusinessPartnerCategory"],
    "MaterialDocument": ["DocumentDate", "PostingDate"],
}


def get_entity_field_requirements(
    service_id: str,
    entity_set: str,
) -> Dict[str, Any]:
    """Get field requirements for an entity based on metadata and SAP patterns.

    Returns:
        {
            "required_fields": [...],
            "optional_fields": [...],
            "auto_generated_fields": [...],
            "all_fields": [...],
            "field_types": {field_name: edm_type}
        }
    """
    import re
    from app.services.service_manager import service_manager

    svc = service_manager._services.get(service_id, {})
    if not svc:
        return {"required_fields": [], "optional_fields": [], "auto_generated_fields": [], "all_fields": [], "field_types": {}}

    meta = svc.get("metadata", {})
    entity_types = meta.get("entity_types", [])
    entity_sets = meta.get("entity_sets", [])

    # Find the entity type for this entity set
    es_info = next((es for es in entity_sets if es["name"] == entity_set), None)
    if not es_info:
        return {"required_fields": [], "optional_fields": [], "auto_generated_fields": [], "all_fields": [], "field_types": {}}

    et_name = (es_info.get("entity_type") or entity_set).split(".")[-1]
    et = next(
        (e for e in entity_types if e["name"] == et_name or f"{e.get('namespace','')}.{e['name']}" == es_info.get("entity_type")),
        None,
    )

    all_fields = []
    field_types = {}
    if et:
        for prop in et.get("properties", []):
            # Handle both dict format and string format like "@{name=ContextId; label=Key}"
            if isinstance(prop, dict):
                fname = prop.get("name", "")
                ftype = prop.get("type", "Edm.String")
            elif isinstance(prop, str):
                # Parse "@{name=ContextId; label=Key}" format
                match = re.search(r"name=(\w+)", prop)
                fname = match.group(1) if match else prop
                ftype = "Edm.String"
            else:
                continue
            if fname:
                all_fields.append(fname)
                field_types[fname] = ftype

    # Determine required vs optional
    # SAP-specific required fields
    sap_required = set()
    for key, req_fields in SAP_REQUIRED_FIELDS.items():
        if key.lower() in entity_set.lower():
            sap_required.update(req_fields)

    # Auto-generated fields (don't ask user for these)
    auto_gen = {f for f in all_fields if f in AUTO_GENERATED_FIELDS}

    # Required = SAP required fields minus auto-generated
    required = list(sap_required - auto_gen)

    # Optional = everything else
    optional = [f for f in all_fields if f not in required and f not in auto_gen]

    return {
        "required_fields": required,
        "optional_fields": optional,
        "auto_generated_fields": list(auto_gen),
        "all_fields": all_fields,
        "field_types": field_types,
    }


# ─── Write History / Audit Log ────────────────────────────────────────────────
_write_history: List[Dict[str, Any]] = []
MAX_WRITE_HISTORY = 200


def log_write_operation(
    operation: str,
    entity_set: str,
    service_id: str,
    user_role: str,
    user_id: str,
    fields: Dict[str, Any],
    success: bool,
    error: str = "",
    entity_id: str = "",
) -> None:
    """Log a write operation for audit trail."""
    import time
    entry = {
        "timestamp": time.time(),
        "operation": operation,
        "entity_set": entity_set,
        "service_id": service_id,
        "user_role": user_role,
        "user_id": user_id,
        "fields": fields,
        "success": success,
        "error": error,
        "entity_id": entity_id,
    }
    _write_history.append(entry)
    # Trim to max
    if len(_write_history) > MAX_WRITE_HISTORY:
        _write_history.pop(0)
    logger.info(f"Write audit: {operation} {entity_set} by {user_id} ({user_role}) - {'OK' if success else 'BLOCKED'}")


def get_write_history(
    limit: int = 50,
    operation: str = "",
    entity_set: str = "",
) -> List[Dict[str, Any]]:
    """Get write operation history."""
    history = list(reversed(_write_history))
    if operation:
        history = [h for h in history if h["operation"] == operation]
    if entity_set:
        history = [h for h in history if h["entity_set"] == entity_set]
    return history[:limit]


def get_write_history_stats() -> Dict[str, Any]:
    """Get summary stats for write history."""
    import time
    now = time.time()
    last_hour = [h for h in _write_history if now - h["timestamp"] < 3600]
    last_day = [h for h in _write_history if now - h["timestamp"] < 86400]
    return {
        "total": len(_write_history),
        "last_hour": len(last_hour),
        "last_day": len(last_day),
        "creates": sum(1 for h in _write_history if h["operation"] == "create"),
        "updates": sum(1 for h in _write_history if h["operation"] == "update"),
        "deletes": sum(1 for h in _write_history if h["operation"] == "delete"),
        "blocked": sum(1 for h in _write_history if not h["success"]),
    }
