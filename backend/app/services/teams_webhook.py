import time
import hashlib
import json
from typing import Dict, Optional

import httpx
from fastapi import APIRouter, Request, Response, Depends, Header
from loguru import logger
from pydantic import BaseModel

from app.config import settings
from app.agents.orchestrator import orchestrator
from app.services.query_enhancements import query_cache
from app.schemas.models import TableData
from app.db.sqlite_store import create_session, add_message, touch_session


router = APIRouter(prefix="/webhook", tags=["teams"])


DEDUP_WINDOW_SECONDS = 30
_dedup_cache: Dict[str, float] = {}
_last_cleanup: float = 0.0

MAX_TABLE_ROWS = 8
MAX_TABLE_COLS = 8
MAX_CARD_LENGTH = 2000


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


def _build_teams_card(title: str, text: str) -> dict:
    return {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "contentUrl": None,
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.4",
                "body": [
                    {
                        "type": "TextBlock",
                        "text": title,
                        "weight": "bolder",
                        "size": "medium"
                    },
                    {
                        "type": "TextBlock",
                        "text": text,
                        "wrap": True
                    }
                ]
            }
        }]
    }


async def send_to_teams_webhook(webhook_url: str, message: str, title: str = "OData Chatbot") -> bool:
    card = _build_teams_card(title, message)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(webhook_url, json=card)
            return resp.status_code in (200, 201, 202)
    except Exception as e:
        logger.error(f"Failed to send to Teams webhook: {e}")
        return False


@router.post("/teams")
async def teams_webhook(request: Request):
    try:
        body = await request.json()

        text = ""
        from_name = "Teams User"
        channel_id = "teams:unknown"
        conversation_id = ""

        if isinstance(body, dict):
            text = body.get("text", "") or body.get("message", "") or body.get("body", "") or ""
            from_name = body.get("from", {})
            if isinstance(from_name, dict):
                from_name = from_name.get("name", "Teams User")
            channel_id = body.get("channelId", "") or body.get("channel_id", "teams:unknown")
            conversation_id = body.get("conversation", {})
            if isinstance(conversation_id, dict):
                conversation_id = conversation_id.get("id", "")
                if not channel_id or channel_id == "teams:unknown":
                    channel_id = conversation_id

            if body.get("type") == "message" and body.get("text"):
                text = body["text"]
                from_obj = body.get("from", {})
                if isinstance(from_obj, dict):
                    from_name = from_obj.get("name", "Teams User")

        text = text.strip()
        if not text:
            return {"status": "ok", "message": "empty"}

        logger.info(f"Teams message from {from_name}: {text[:80]}")

        if _check_dedup(channel_id, text):
            return {"status": "ok", "message": "dedup"}

        from app.services.service_manager import service_manager
        if not service_manager._services:
            await service_manager.recover_from_graph()

        user_role = "TeamsUser"
        session_id = create_session(title=f"[Teams] {text[:50]}", user_role=user_role)
        add_message(session_id, "user", text)

        cached_result = query_cache.get(text, session_id)
        if cached_result:
            summary = cached_result.get("summary", "")
            table_data = cached_result.get("table")
            table_text = _format_table_for_chat(TableData(**table_data)) if table_data else ""
        else:
            result = await orchestrator.run(
                user_query=text,
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

        if len(reply) > MAX_CARD_LENGTH:
            reply = reply[:MAX_CARD_LENGTH - 50] + "\n\n[Response truncated]"

        logger.info(f"Teams reply: {reply[:80]}")
        return _build_teams_card("OData Chatbot", reply)

    except Exception as e:
        logger.exception(f"Teams webhook error: {e}")
        return _build_teams_card("Error", "Sorry, an error occurred. Please try again.")


class TeamsChatRequest(BaseModel):
    message: str
    webhook_url: str
    session_id: Optional[str] = None


def verify_teams_token(authorization: Optional[str] = Header(None)) -> None:
    if not settings.n8n_integration_token:
        return
    if not authorization:
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or token != settings.n8n_integration_token:
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Invalid token")


@router.post("/teams/send")
async def teams_send(payload: TeamsChatRequest, auth: None = Depends(verify_teams_token)):
    """Send a query to OData and post the response to a Teams channel via webhook."""
    try:
        message = payload.message.strip()
        if not message:
            return {"error": "Empty message"}

        from app.services.service_manager import service_manager
        if not service_manager._services:
            await service_manager.recover_from_graph()

        user_role = "TeamsUser"
        session_id = payload.session_id or create_session(title=f"[Teams] {message[:50]}", user_role=user_role)
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

        if len(reply) > MAX_CARD_LENGTH:
            reply = reply[:MAX_CARD_LENGTH - 50] + "\n\n[Response truncated]"

        sent = await send_to_teams_webhook(payload.webhook_url, reply, "OData Chatbot")
        return {"sent": sent, "reply": reply, "session_id": session_id}

    except Exception as e:
        logger.exception(f"teams_send error: {e}")
        return {"error": str(e), "session_id": payload.session_id or ""}
