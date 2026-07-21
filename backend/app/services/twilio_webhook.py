import time
import hashlib
import xml.etree.ElementTree as ET
from typing import Dict

from fastapi import APIRouter, Request, Response
from loguru import logger

from app.agents.orchestrator import orchestrator
from app.services.query_enhancements import query_cache
from app.schemas.models import TableData
from app.db.sqlite_store import create_session, add_message, touch_session


router = APIRouter(prefix="/webhook", tags=["twilio"])


DEDUP_WINDOW_SECONDS = 30
_dedup_cache: Dict[str, float] = {}
_last_cleanup: float = 0.0

MAX_TABLE_ROWS = 6
MAX_TABLE_COLS = 6
MAX_TWIML_LENGTH = 1500


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


def _build_twiml(message: str) -> Response:
    response = ET.Element("Response")
    msg_el = ET.SubElement(response, "Message")
    msg_el.text = message
    xml_bytes = ET.tostring(response, encoding="unicode", xml_declaration=False)
    xml_str = '<?xml version="1.0" encoding="UTF-8"?>\n' + xml_bytes
    return Response(content=xml_str, media_type="text/xml; charset=utf-8")


@router.post("/whatsapp")
async def twilio_whatsapp_webhook(request: Request):
    try:
        form = await request.form()
        body = form.get("Body", "")
        from_number = form.get("From", "")
        profile_name = form.get("ProfileName", "User")
        wa_id = form.get("WaId", from_number)

        message = str(body).strip()
        if not message:
            return _build_twiml("Please send a message to get started.")

        logger.info(f"WhatsApp from {from_number} ({profile_name}): {message[:80]}")

        if _check_dedup(from_number, message):
            return _build_twiml("")

        from app.services.service_manager import service_manager
        if not service_manager._services:
            await service_manager.recover_from_graph()

        user_role = "WhatsAppUser"
        channel_id = from_number
        session_id = create_session(title=f"[WhatsApp] {message[:50]}", user_role=user_role)

        add_message(session_id, "user", message)

        cached_result = query_cache.get(message, session_id)
        if cached_result:
            summary = cached_result.get("summary", "")
            table_data = cached_result.get("table")
            table_text = _format_table_for_chat(TableData(**table_data)) if table_data else ""
        else:
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
            if result.get("error"):
                summary += f"\n\nError: {result['error']}"

        parts = [summary] if summary else []
        if table_text:
            parts.append(table_text)
        reply = "\n\n".join(parts).strip() if parts else "No result found."

        if len(reply) > MAX_TWIML_LENGTH:
            reply = reply[: MAX_TWIML_LENGTH - 50] + "\n\n[Response truncated]"

        if not reply:
            reply = "No result found."

        logger.info(f"WhatsApp reply to {from_number}: {reply[:80]}")
        return _build_twiml(reply)

    except Exception as e:
        logger.exception(f"Twilio webhook error: {e}")
        return _build_twiml("Sorry, an error occurred. Please try again.")
