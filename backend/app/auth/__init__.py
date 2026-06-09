"""
Auth module - JWT, bcrypt, login/logout, role checking.
"""
import jwt
import time
import os
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any
from functools import wraps
from fastapi import Request, HTTPException

from app.auth.password import hash_password, verify_password, validate_password_strength

SECRET_KEY = os.getenv("JWT_SECRET", "odata-orchestration-secret-key-change-in-production")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30
REFRESH_TOKEN_EXPIRE_DAYS = 7
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_MINUTES = 15


def create_access_token(data: Dict[str, Any], expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire, "type": "access"})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def create_refresh_token(data: Dict[str, Any]) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    to_encode.update({"exp": expire, "type": "refresh"})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> Optional[Dict[str, Any]]:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


def get_current_user(request: Request) -> Optional[Dict[str, Any]]:
    token = None
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header[7:]
    if not token:
        token = request.cookies.get("access_token")
    if not token:
        return None
    payload = decode_token(token)
    if not payload or payload.get("type") != "access":
        return None
    return payload


def require_auth(handler):
    @wraps(handler)
    async def wrapper(request: Request, *args, **kwargs):
        user = get_current_user(request)
        if not user:
            raise HTTPException(status_code=401, detail="Authentication required")
        request.state.user = user
        return await handler(request, *args, **kwargs)
    return wrapper


def require_role(*roles):
    def decorator(handler):
        @wraps(handler)
        async def wrapper(request: Request, *args, **kwargs):
            user = get_current_user(request)
            if not user:
                raise HTTPException(status_code=401, detail="Authentication required")
            if user.get("role") not in roles:
                raise HTTPException(status_code=403, detail="Insufficient permissions")
            request.state.user = user
            return await handler(request, *args, **kwargs)
        return wrapper
    return decorator


def check_permission(user_role: str, resource: str, action: str) -> bool:
    if user_role == "super_admin":
        return True
    from app.auth.db import get_auth_db
    db = get_auth_db()
    role = db.get_role(user_role)
    if not role:
        return False
    import json
    permissions = json.loads(role.get("permissions", "{}"))
    if "*" in permissions:
        return True
    if resource in permissions:
        perm = permissions[resource]
        if perm == "*" or action in perm:
            return True
    return False
