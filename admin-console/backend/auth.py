"""Admin Console — Auth (infra admin accounts).

- AdminUser: id, email, display_name, role[L5 infra-admin, L4 read-only], hashed_password, created_at
- bcrypt hash, JWT HS256 8h expiry, get_current_admin, seed admin@openit.co.kr / Admin123!
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from pydantic import BaseModel, EmailStr, Field

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
JWT_SECRET = os.environ.get("ADMIN_JWT_SECRET", "dev-admin-jwt-secret-please-change")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = 8

# ---------------------------------------------------------------------------
# Role
# ---------------------------------------------------------------------------
class AdminRole(str, Enum):
    L5 = "L5"  # infra-admin (full)
    L4 = "L4"  # admin read-only


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class AdminUser(BaseModel):
    id: str
    email: str
    display_name: str
    role: AdminRole
    hashed_password: str
    created_at: datetime


class AdminUserPublic(BaseModel):
    id: str
    email: str
    display_name: str
    role: AdminRole
    created_at: datetime


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    display_name: str = Field(min_length=1)
    role: AdminRole = AdminRole.L4


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


# ---------------------------------------------------------------------------
# In-memory store
# ---------------------------------------------------------------------------
_users_by_id: dict[str, AdminUser] = {}
_users_by_email: dict[str, AdminUser] = {}


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))


def _create_jwt(email: str, role: str) -> tuple[str, int]:
    expire = datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRE_HOURS)
    payload = {"sub": email, "role": role, "exp": expire}
    token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    expires_in = int(JWT_EXPIRE_HOURS * 3600)
    return token, expires_in


def _seed_admin() -> None:
    """Create dev seed account if not exists."""
    email = "admin@openit.co.kr"
    if email in _users_by_email:
        return
    uid = f"admin_{uuid.uuid4().hex[:8]}"
    user = AdminUser(
        id=uid,
        email=email,
        display_name="Infra Admin",
        role=AdminRole.L5,
        hashed_password=_hash_password("Admin123!"),
        created_at=datetime.now(timezone.utc),
    )
    _users_by_id[uid] = user
    _users_by_email[email] = user


_seed_admin()


# ---------------------------------------------------------------------------
# Helpers (for testing / infra)
# ---------------------------------------------------------------------------
def get_user_by_email(email: str) -> Optional[AdminUser]:
    return _users_by_email.get(email)


def get_user_by_id(uid: str) -> Optional[AdminUser]:
    return _users_by_id.get(uid)


def clear_users() -> None:
    """Test helper — clear all users then re-seed."""
    _users_by_id.clear()
    _users_by_email.clear()
    _seed_admin()


def list_users() -> list[AdminUser]:
    return list(_users_by_id.values())


# ---------------------------------------------------------------------------
# Auth dependencies
# ---------------------------------------------------------------------------
_bearer = HTTPBearer(auto_error=False)


def get_current_admin(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> AdminUser:
    if credentials is None or not credentials.credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    token = credentials.credentials
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        email: str | None = payload.get("sub")
        if email is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    except JWTError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=f"Invalid token: {e}")
    user = _users_by_email.get(email)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return user


def require_l5(admin: AdminUser = Depends(get_current_admin)) -> AdminUser:
    if admin.role != AdminRole.L5:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="L5 infra-admin required")
    return admin


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------
router = APIRouter(prefix="/v1/auth", tags=["auth"])


@router.post("/register", response_model=AdminUserPublic, status_code=201)
def register(req: RegisterRequest, admin: AdminUser = Depends(require_l5)):
    """Register new admin — L5 only."""
    if req.email in _users_by_email:
        raise HTTPException(status_code=409, detail="Email already registered")
    uid = f"admin_{uuid.uuid4().hex[:8]}"
    user = AdminUser(
        id=uid,
        email=req.email,
        display_name=req.display_name,
        role=req.role,
        hashed_password=_hash_password(req.password),
        created_at=datetime.now(timezone.utc),
    )
    _users_by_id[uid] = user
    _users_by_email[req.email] = user
    return AdminUserPublic(**user.model_dump(exclude={"hashed_password"}))


@router.post("/login", response_model=TokenResponse)
def login(req: LoginRequest):
    user = _users_by_email.get(req.email)
    if user is None or not _verify_password(req.password, user.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    token, expires_in = _create_jwt(user.email, user.role.value)
    return TokenResponse(access_token=token, expires_in=expires_in)


@router.get("/me", response_model=AdminUserPublic)
def me(admin: AdminUser = Depends(get_current_admin)):
    return AdminUserPublic(**admin.model_dump(exclude={"hashed_password"}))


@router.get("/users", response_model=list[AdminUserPublic])
def list_admin_users(admin: AdminUser = Depends(get_current_admin)):
    """List all admin users — any authenticated admin."""
    return [AdminUserPublic(**u.model_dump(exclude={"hashed_password"})) for u in _users_by_id.values()]


@router.delete("/users/{user_id}")
def delete_admin_user(user_id: str, admin: AdminUser = Depends(require_l5)):
    """Delete admin user — L5 only, self-delete blocked."""
    if admin.id == user_id:
        raise HTTPException(status_code=400, detail="자기 자신은 삭제할 수 없습니다")
    target = _users_by_id.get(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="user not found")
    # prevent deleting last L5? keep allowed but warn — enforce self-block only per spec
    del _users_by_id[user_id]
    # remove email mapping
    for em, u in list(_users_by_email.items()):
        if u.id == user_id:
            del _users_by_email[em]
            break
    return {"status": "deleted", "id": user_id}
