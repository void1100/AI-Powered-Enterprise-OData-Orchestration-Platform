import time
import hashlib
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Header
from loguru import logger
from pydantic import BaseModel

from app.config import settings
from app.agents.orchestrator import orchestrator
from app.services.query_enhancements import query_cache
from app.schemas.models import TableData
from app.db.sqlite_store import create_session, add_message, touch_session


router = APIRouter(prefix="/integrations/n8n", tags=["n8n"])


DEDUP_WINDOW_SECONDS = 30
_dedup_cache: Dict[str, float] = {}
_last_cleanup: float = 0.0

MAX_TABLE_ROWS = 10
MAX_TABLE_COLS = 8


class N8nChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
    channel_id: str
    channel_type: str
    user_name: Optional[str] = None


class N8nChatResponse(BaseModel):
    text: Optional[str] = None
    table_text: Optional[str] = None
    blocks: Optional[List[Dict[str, Any]]] = None
    session_id: str
    error: Optional[str] = None


def verify_integration_token(authorization: Optional[str] = Header(None)) -> None:
    if not settings.n8n_integration_token:
        return
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or token != settings.n8n_integration_token:
        raise HTTPException(status_code=401, detail="Invalid integration token")


def _dedup_key(channel_id: str, message: str) -> str:
    raw = f"{channel_id}:{message.strip().lower()}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _check_dedup(channel_id: str, message: str) -> bool:
    global _last_cleanup
    now = time.time()
    if now - _last_cleanup > 60:
        expired = [k for k, v in _dedup_cache.items() if now - v > DEDUP_WINDOW_SECONDS]
        for k in expired:
            _dedup_cache.pop(k, None)
        _last_cleanup = now
    key = _dedup_key(channel_id, message)
    if key in _dedup_cache:
        return True
    _dedup_cache[key] = now
    return False


def _format_table_for_chat(table: TableData, max_rows: int = MAX_TABLE_ROWS, max_cols: int = MAX_TABLE_COLS) -> str:
    if not table or not table.rows:
        return ""
    cols = table.columns[:max_cols]
    rows = table.rows[:max_rows]
    sep = " | "
    header = sep.join(cols)
    divider = "-" * len(header)
    lines = [header, divider]
    for row in rows:
        lines.append(sep.join(str(row.get(c, "")) for c in cols))
    if len(table.rows) > max_rows:
        lines.append(f"... and {len(table.rows) - max_rows} more rows")
    return "\n".join(lines)


def _truncate_text(text: str, max_len: int = 2000) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 100] + f"\n\n[Response truncated at {max_len} chars]"


@router.get("/chat")
async def n8n_chat_health():
    return {"status": "ok", "message": "n8n integration endpoint active"}


@router.post("/chat", response_model=N8nChatResponse)
async def n8n_chat(payload: N8nChatRequest, auth: None = Depends(verify_integration_token)):
    try:
        message = payload.message.strip()
        channel_id = payload.channel_id
        channel_type = payload.channel_type
        user_name = payload.user_name or "User"

        if _check_dedup(channel_id, message):
            logger.debug(f"Dedup hit for {channel_id}: {message[:50]}")
            return N8nChatResponse(
                text="",
                session_id=payload.session_id or "",
            )

        from app.services.service_manager import service_manager
        if not service_manager._services:
            await service_manager.recover_from_graph()

        user_role = "N8nUser"

        session_id = payload.session_id
        if not session_id:
            session_id = create_session(title=f"[{channel_type}] {message[:50]}", user_role=user_role)
        else:
            touch_session(session_id)

        add_message(session_id, "user", message)

        cached_result = query_cache.get(message, session_id)
        if cached_result:
            summary = cached_result.get("summary", "")
            table_data = cached_result.get("table")
            table_text = _format_table_for_chat(TableData(**table_data)) if table_data else ""
            return N8nChatResponse(
                text=_truncate_text(summary),
                table_text=table_text,
                session_id=session_id,
            )

        result = await orchestrator.run(
            user_query=message,
            session_id=session_id,
            user_role=user_role,
        )

        add_message(
            session_id,
            "assistant",
            result.get("summary", ""),
            plan=result.get("plan"),
            result={"table": result.get("table"), "tool_calls": result.get("tool_calls")},
        )

        summary = result.get("summary", "")
        table_raw = result.get("table")
        table_data = TableData(**table_raw) if table_raw else None
        table_text = _format_table_for_chat(table_data) if table_data else ""

        text_parts = [summary]
        if table_text:
            text_parts.append(f"\n---\n{table_text}")

        if result.get("error"):
            text_parts.append(f"\n\nError: {result['error']}")

        final_text = _truncate_text("\n".join(text_parts))

        return N8nChatResponse(
            text=final_text,
            table_text=table_text,
            session_id=session_id,
        )

    except Exception as e:
        logger.exception(f"n8n_chat error: {e}")
        return N8nChatResponse(
            text="Sorry, an unexpected error occurred. Please try again.",
            session_id=payload.session_id or "",
            error=str(e),
        )
