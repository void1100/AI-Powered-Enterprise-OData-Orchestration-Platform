"""
Admin routes - Auth, User Management, Dashboard.
"""
import json
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from fastapi import APIRouter, Request, HTTPException, Response, Query, Depends
from pydantic import BaseModel, EmailStr

from app.auth import (
    create_access_token, create_refresh_token, decode_token,
    get_current_user, require_role, check_permission,
    ACCESS_TOKEN_EXPIRE_MINUTES
)
from app.auth.password import hash_password, verify_password, validate_password_strength
from app.auth.db import get_auth_db
from app.schemas.models import ServiceRegister

router = APIRouter()

ROLE_PERMISSIONS = {
    "super_admin": {"dashboard", "users", "roles", "services", "custom_entities", "analytics", "audit", "settings"},
    "admin": {"dashboard", "users", "roles", "services", "custom_entities", "analytics", "audit", "settings"},
    "analyst": {"dashboard", "analytics", "services"},
    "user": {"dashboard"},
    "viewer": {"dashboard"},
}

def require_permission(permission: str):
    async def check(request: Request):
        user = get_current_user(request)
        if not user:
            raise HTTPException(status_code=401, detail="Authentication required")
        role = user.get("role", "viewer")
        allowed = ROLE_PERMISSIONS.get(role, {"dashboard"})
        if permission not in allowed:
            raise HTTPException(status_code=403, detail=f"Role '{role}' cannot access '{permission}'")
        request.state.user = user
        return user
    return check


# --- Request/Response Models ---

class LoginRequest(BaseModel):
    username: str
    password: str

class LoginResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user: Dict[str, Any]

class UserCreate(BaseModel):
    username: str
    email: str
    password: str
    role: str = "user"
    display_name: str = ""

class UserUpdate(BaseModel):
    email: Optional[str] = None
    role: Optional[str] = None
    status: Optional[str] = None
    display_name: Optional[str] = None
    password: Optional[str] = None

class ChangePassword(BaseModel):
    old_password: str
    new_password: str


# --- Auth Endpoints ---

@router.post("/auth/login", response_model=LoginResponse)
async def login(body: LoginRequest, request: Request, response: Response):
    db = get_auth_db()
    user = db.get_user(body.username)

    if not user:
        db.log_audit(action="login", resource="auth", details=f"Failed login for {body.username}", ip_address=request.client.host, status="failure")
        raise HTTPException(status_code=401, detail="Invalid credentials")

    if user["status"] == "disabled":
        raise HTTPException(status_code=403, detail="Account is disabled")
    if user["status"] == "locked":
        if user["locked_until"]:
            locked_until = datetime.fromisoformat(user["locked_until"])
            if datetime.now(timezone.utc) < locked_until:
                raise HTTPException(status_code=403, detail="Account is locked. Try again later.")
            else:
                db.update_user(user["id"], status="active", failed_attempts=0, locked_until=None)
                user["status"] = "active"

    if not verify_password(body.password, user["password_hash"]):
        attempts = user["failed_attempts"] + 1
        updates = {"failed_attempts": attempts}
        if attempts >= 5:
            from datetime import timedelta
            updates["status"] = "locked"
            updates["locked_until"] = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
        db.update_user(user["id"], **updates)
        db.log_audit(user_id=user["id"], username=user["username"], action="login", resource="auth", details="Invalid password", ip_address=request.client.host, status="failure")
        raise HTTPException(status_code=401, detail="Invalid credentials")

    db.update_user(user["id"], failed_attempts=0, locked_until=None, last_login=datetime.now(timezone.utc).isoformat())

    token_data = {"sub": user["username"], "user_id": user["id"], "role": user["role"], "email": user["email"]}
    access_token = create_access_token(token_data)
    refresh_token = create_refresh_token(token_data)

    import os as _os
    _is_prod = _os.getenv("ENVIRONMENT", "development").lower() == "production"
    response.set_cookie("access_token", access_token, httponly=True, secure=_is_prod, samesite="strict", max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60)

    db.log_audit(user_id=user["id"], username=user["username"], action="login", resource="auth", ip_address=request.client.host, status="success")

    safe_user = {k: v for k, v in user.items() if k != "password_hash"}
    return LoginResponse(access_token=access_token, refresh_token=refresh_token, user=safe_user)


@router.post("/auth/logout")
async def logout(request: Request, response: Response):
    user = get_current_user(request)
    if user:
        db = get_auth_db()
        db.log_audit(user_id=user.get("user_id"), username=user.get("sub"), action="logout", resource="auth", ip_address=request.client.host, status="success")
    response.delete_cookie("access_token")
    return {"message": "Logged out"}


@router.get("/auth/me")
async def get_me(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    db = get_auth_db()
    full_user = db.get_user(user["sub"])
    if not full_user:
        raise HTTPException(status_code=404, detail="User not found")
    return {k: v for k, v in full_user.items() if k != "password_hash"}


@router.post("/auth/refresh")
async def refresh_token(request: Request):
    token = request.cookies.get("refresh_token") or request.headers.get("X-Refresh-Token")
    if not token:
        raise HTTPException(status_code=401, detail="Refresh token required")
    payload = decode_token(token)
    if not payload or payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Invalid refresh token")
    db = get_auth_db()
    user = db.get_user(payload["sub"])
    if not user or user["status"] != "active":
        raise HTTPException(status_code=401, detail="User not found or inactive")
    new_access = create_access_token({"sub": user["username"], "user_id": user["id"], "role": user["role"], "email": user["email"]})
    return {"access_token": new_access, "token_type": "bearer"}


# --- User Management ---

@router.get("/admin/users")
async def list_users(request: Request, user=Depends(require_permission("users")), role: str = None, status: str = None, search: str = None):
    db = get_auth_db()
    users = db.list_users(role=role, status=status, search=search)
    return [{k: v for k, v in u.items() if k != "password_hash"} for u in users]


@router.post("/admin/users")
async def create_user(request: Request, body: UserCreate, user=Depends(require_permission("users"))):
    db = get_auth_db()

    valid, msg = validate_password_strength(body.password)
    if not valid:
        raise HTTPException(status_code=400, detail=msg)

    if db.get_user(body.username):
        raise HTTPException(status_code=409, detail="Username already exists")
    if db.get_user_by_email(body.email):
        raise HTTPException(status_code=409, detail="Email already exists")

    new_user = db.create_user(
        username=body.username,
        email=body.email,
        password_hash=hash_password(body.password),
        role=body.role,
        display_name=body.display_name
    )

    db.log_audit(user_id=user.get("user_id"), username=user.get("sub"), action="create", resource="users", resource_id=str(new_user["id"]), details=f"Created user {body.username}", ip_address=request.client.host, status="success")

    return {k: v for k, v in new_user.items() if k != "password_hash"}


@router.patch("/admin/users/{user_id}")
async def update_user(user_id: int, request: Request, body: UserUpdate, user=Depends(require_permission("users"))):
    db = get_auth_db()
    target = db.get_user_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    if target["id"] == user.get("user_id") and body.role and body.role != user.get("role"):
        if user.get("role") == "super_admin":
            users = db.list_users(role="super_admin")
            if len(users) <= 1:
                raise HTTPException(status_code=400, detail="Cannot demote the last super_admin")

    updates = {}
    if body.email:
        existing = db.get_user_by_email(body.email)
        if existing and existing["id"] != user_id:
            raise HTTPException(status_code=409, detail="Email already in use")
        updates["email"] = body.email
    if body.role:
        updates["role"] = body.role
    if body.status:
        updates["status"] = body.status
    if body.display_name:
        updates["display_name"] = body.display_name
    if body.password:
        valid, msg = validate_password_strength(body.password)
        if not valid:
            raise HTTPException(status_code=400, detail=msg)
        updates["password_hash"] = hash_password(body.password)

    updated = db.update_user(user_id, **updates)
    db.log_audit(user_id=user.get("user_id"), username=user.get("sub"), action="update", resource="users", resource_id=str(user_id), details=json.dumps(updates), ip_address=request.client.host, status="success")

    return {k: v for k, v in updated.items() if k != "password_hash"}


@router.delete("/admin/users/{user_id}")
async def delete_user(user_id: int, request: Request, user=Depends(require_permission("users"))):
    db = get_auth_db()
    target = db.get_user_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    if target["id"] == user.get("user_id"):
        raise HTTPException(status_code=400, detail="Cannot delete your own account")

    if target["role"] == "super_admin":
        users = db.list_users(role="super_admin")
        if len(users) <= 1:
            raise HTTPException(status_code=400, detail="Cannot delete the last super_admin")

    db.delete_user(user_id)
    db.log_audit(user_id=user.get("user_id"), username=user.get("sub"), action="delete", resource="users", resource_id=str(user_id), details=f"Deleted user {target['username']}", ip_address=request.client.host, status="success")

    return {"message": "User deleted"}


# --- Dashboard ---

@router.get("/admin/dashboard")
async def get_dashboard(request: Request, user=Depends(require_permission("dashboard"))):
    db = get_auth_db()
    user_counts = db.count_users()
    audit = db.get_audit_log(limit=20)

    return {
        "user_counts": user_counts,
        "total_users": sum(user_counts.values()),
        "recent_activity": audit,
    }


@router.get("/admin/roles")
async def list_roles(request: Request, user=Depends(require_permission("roles"))):
    db = get_auth_db()
    return db.list_roles()


@router.get("/admin/audit")
async def get_audit(request: Request, user=Depends(require_permission("audit")), limit: int = Query(100, le=500)):
    db = get_auth_db()
    return db.get_audit_log(limit=limit)


# --- Role Management ---

class RoleCreate(BaseModel):
    name: str
    display_name: str
    description: str = ""
    permissions: str = "{}"

class RoleUpdate(BaseModel):
    display_name: Optional[str] = None
    description: Optional[str] = None
    permissions: Optional[str] = None


@router.post("/admin/roles")
async def create_role(request: Request, body: RoleCreate, user=Depends(require_permission("roles"))):
    if user.get("role") != "super_admin":
        raise HTTPException(status_code=403, detail="Super admin only")

    db = get_auth_db()
    if db.get_role(body.name):
        raise HTTPException(status_code=409, detail="Role already exists")

    try:
        json.loads(body.permissions)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid permissions JSON")

    now = datetime.now(timezone.utc).isoformat()
    conn = db._get_conn()
    try:
        conn.execute(
            "INSERT INTO roles (name, display_name, description, permissions, is_system, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (body.name, body.display_name, body.description, body.permissions, 0, now, now)
        )
        conn.commit()
    finally:
        conn.close()

    db.log_audit(user_id=user.get("user_id"), username=user.get("sub"), action="create", resource="roles", resource_id=body.name, ip_address=request.client.host, status="success")
    return db.get_role(body.name)


@router.patch("/admin/roles/{role_name}")
async def update_role(role_name: str, request: Request, body: RoleUpdate, user=Depends(require_permission("roles"))):
    if user.get("role") != "super_admin":
        raise HTTPException(status_code=403, detail="Super admin only")

    db = get_auth_db()
    role = db.get_role(role_name)
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    if role.get("is_system"):
        raise HTTPException(status_code=400, detail="Cannot modify system roles")

    if body.permissions:
        try:
            json.loads(body.permissions)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid permissions JSON")

    now = datetime.now(timezone.utc).isoformat()
    updates = {"updated_at": now}
    if body.display_name is not None:
        updates["display_name"] = body.display_name
    if body.description is not None:
        updates["description"] = body.description
    if body.permissions is not None:
        updates["permissions"] = body.permissions

    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [role_name]
    conn = db._get_conn()
    try:
        conn.execute(f"UPDATE roles SET {set_clause} WHERE name = ?", values)
        conn.commit()
    finally:
        conn.close()

    db.log_audit(user_id=user.get("user_id"), username=user.get("sub"), action="update", resource="roles", resource_id=role_name, details=json.dumps(updates), ip_address=request.client.host, status="success")
    return db.get_role(role_name)


@router.delete("/admin/roles/{role_name}")
async def delete_role(role_name: str, request: Request, user=Depends(require_permission("roles"))):
    if user.get("role") != "super_admin":
        raise HTTPException(status_code=403, detail="Super admin only")

    db = get_auth_db()
    role = db.get_role(role_name)
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    if role.get("is_system"):
        raise HTTPException(status_code=400, detail="Cannot delete system roles")

    users = db.list_users(role=role_name)
    if users:
        raise HTTPException(status_code=400, detail=f"Cannot delete role: {len(users)} users assigned")

    conn = db._get_conn()
    try:
        conn.execute("DELETE FROM roles WHERE name = ?", (role_name,))
        conn.commit()
    finally:
        conn.close()

    db.log_audit(user_id=user.get("user_id"), username=user.get("sub"), action="delete", resource="roles", resource_id=role_name, ip_address=request.client.host, status="success")
    return {"message": "Role deleted"}


# --- Service Management ---

@router.get("/admin/services")
async def list_services_admin(request: Request, user=Depends(require_permission("services"))):
    from app.services.service_manager import service_manager
    services = service_manager.list_services()
    return services


@router.post("/admin/services")
async def register_service_admin(request: Request, body: ServiceRegister, user=Depends(require_permission("services"))):
    from app.services.service_manager import service_manager
    try:
        info = await service_manager.register_service(
            service_id=body.id,
            name=body.name,
            base_url=body.base_url,
            description=body.description or "",
        )
        db = get_auth_db()
        db.log_audit(user_id=user.get("user_id"), username=user.get("sub"), action="create", resource="services", resource_id=body.id, ip_address=request.client.host, status="success")
        return {"id": info["id"], "name": info["name"], "base_url": info["base_url"]}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/admin/services/{service_id}")
async def delete_service_admin(service_id: str, request: Request, user=Depends(require_permission("services"))):
    from app.services.service_manager import service_manager
    if service_id not in service_manager._services:
        raise HTTPException(status_code=404, detail="Service not found")
    del service_manager._services[service_id]
    service_manager._clients.pop(service_id, None)
    service_manager._entity_to_set.pop(service_id, None)

    db = get_auth_db()
    db.log_audit(user_id=user.get("user_id"), username=user.get("sub"), action="delete", resource="services", resource_id=service_id, ip_address=request.client.host, status="success")
    return {"message": "Service deleted"}


# --- Analytics ---

@router.get("/admin/analytics")
async def get_analytics(request: Request, user=Depends(require_permission("analytics"))):
    db = get_auth_db()
    conn = db._get_conn()
    try:
        rows = conn.execute("""
            SELECT DATE(created_at) as day, COUNT(*) as cnt
            FROM audit_log
            WHERE created_at >= datetime('now', '-7 days')
            GROUP BY DATE(created_at)
            ORDER BY day
        """).fetchall()
        query_volume = [{"date": r["day"], "count": r["cnt"]} for r in rows]

        rows = conn.execute("""
            SELECT action, COUNT(*) as cnt
            FROM audit_log
            GROUP BY action
            ORDER BY cnt DESC
        """).fetchall()
        action_breakdown = [{"action": r["action"], "count": r["cnt"]} for r in rows]

        rows = conn.execute("""
            SELECT resource, COUNT(*) as cnt
            FROM audit_log
            GROUP BY resource
            ORDER BY cnt DESC
        """).fetchall()
        resource_breakdown = [{"resource": r["resource"], "count": r["cnt"]} for r in rows]

        rows = conn.execute("""
            SELECT status, COUNT(*) as cnt
            FROM audit_log
            GROUP BY status
        """).fetchall()
        status_breakdown = [{"status": r["status"], "count": r["cnt"]} for r in rows]

        total_users = conn.execute("SELECT COUNT(*) as cnt FROM users").fetchone()["cnt"]
        total_audit = conn.execute("SELECT COUNT(*) as cnt FROM audit_log").fetchone()["cnt"]

        from app.services.service_manager import service_manager
        services = service_manager.list_services()

        return {
            "query_volume": query_volume,
            "action_breakdown": action_breakdown,
            "resource_breakdown": resource_breakdown,
            "status_breakdown": status_breakdown,
            "total_users": total_users,
            "total_audit_entries": total_audit,
            "total_services": len(services),
        }
    finally:
        conn.close()


# --- System Settings ---

@router.get("/admin/settings")
async def get_settings(request: Request, user=Depends(require_permission("settings"))):
    from app.config import settings
    return {
        "llm_provider": settings.llm_provider,
        "llm_model": settings.llm_model,
        "cors_origins": settings.cors_origins,
        "neo4j_uri": settings.neo4j_uri,
    }


class SettingsUpdate(BaseModel):
    llm_provider: Optional[str] = None
    llm_model: Optional[str] = None
    cors_origins: Optional[str] = None


@router.patch("/admin/settings")
async def update_settings(request: Request, body: SettingsUpdate, user=Depends(require_permission("settings"))):
    updates = {}
    if body.llm_provider is not None:
        updates["llm_provider"] = body.llm_provider
    if body.llm_model is not None:
        updates["llm_model"] = body.llm_model
    if body.cors_origins is not None:
        updates["cors_origins"] = body.cors_origins

    db = get_auth_db()
    db.log_audit(user_id=user.get("user_id"), username=user.get("sub"), action="update", resource="settings", details=json.dumps(updates), ip_address=request.client.host, status="success")

    return {"message": "Settings updated (restart required)", "updates": updates}
