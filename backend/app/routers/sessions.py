"""Sessions router — chat session CRUD and message history."""
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Request

from app.auth import get_current_user
from app.schemas.models import MessageInfo, SessionCreate, SessionInfo
from app.db.sqlite_store import (
    create_session,
    delete_session,
    get_messages,
    get_session,
    list_sessions,
    rename_session,
)

router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.get("", response_model=List[SessionInfo])
async def get_sessions(request: Request):
    """Return sessions scoped to the logged-in user.
    Admins (super_admin / admin) see all sessions.
    """
    user = get_current_user(request)
    user_id = user.get("sub") if user else None
    user_role = user.get("role", "") if user else ""
    is_admin = user_role in ("super_admin", "admin")
    return [SessionInfo(**s) for s in list_sessions(user_id=user_id, is_admin=is_admin)]


@router.post("", response_model=SessionInfo)
async def create_session_endpoint(payload: SessionCreate, request: Request):
    """Create a new session owned by the logged-in user."""
    user = get_current_user(request)
    user_id = user.get("sub") if user else None
    user_role = user.get("role", payload.user_role) if user else payload.user_role
    sid = create_session(title=payload.title, user_role=user_role, user_id=user_id)
    session = get_session(sid)
    if not session:
        raise HTTPException(status_code=500, detail="Failed to create session")
    return SessionInfo(**session)


@router.patch("/{session_id}")
async def patch_session(session_id: str, payload: Dict[str, str], request: Request):
    """Rename a session. Users can only rename their own; admins can rename any."""
    user = get_current_user(request)
    user_id = user.get("sub") if user else None
    user_role = user.get("role", "") if user else ""
    is_admin = user_role in ("super_admin", "admin")
    session = get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not is_admin and session.get("user_id") and user_id and session["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="You do not own this session")
    if "title" in payload:
        rename_session(session_id, payload["title"])
    return {"ok": True}


@router.delete("/{session_id}")
async def delete_session_endpoint(session_id: str, request: Request):
    """Delete a session. Users can only delete their own; admins can delete any."""
    user = get_current_user(request)
    user_id = user.get("sub") if user else None
    user_role = user.get("role", "") if user else ""
    is_admin = user_role in ("super_admin", "admin")
    session = get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not is_admin and session.get("user_id") and user_id and session["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="You do not own this session")
    delete_session(session_id)
    return {"deleted": session_id}


@router.get("/{session_id}/messages", response_model=List[MessageInfo])
async def get_session_messages(session_id: str, request: Request):
    """Return messages for a session. Users can only read their own; admins can read any."""
    user = get_current_user(request)
    user_id = user.get("sub") if user else None
    user_role = user.get("role", "") if user else ""
    is_admin = user_role in ("super_admin", "admin")
    session = get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not is_admin and session.get("user_id") and user_id and session["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="You do not own this session")
    return [MessageInfo(**m) for m in get_messages(session_id)]
