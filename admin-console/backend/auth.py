"""Admin Console — Auth (infra admin accounts).

- AdminUser: id, email, display_name, role[L5 infra-admin, L4 read-only], hashed_password, created_at
- Argon2id primary + bcrypt fallback, JWT HS256 8h expiry, get_current_admin.
- Dev/test seed admin@openit.co.kr / Admin123! when OAOS_ENV != production.
- Production (OAOS_ENV=production|prod) is fail-closed: no default seed; requires existing admin in DB/cache OR OAOS_ADMIN_BOOTSTRAP_PASSWORD (and optional OAOS_ADMIN_BOOTSTRAP_EMAIL) to create the initial L5 admin. Bootstrap secret is never logged. If neither exists at startup, the process raises RuntimeError.
- DB persistence (AdminUserORM) with in-memory fallback — uses oaos DB when
  DATABASE_URL/OAOS_DATABASE_URL is set, otherwise falls back to _users_by_id dict.
  All DB imports are lazy inside functions so tests pass without DB / drivers.
"""
from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional

import bcrypt
import logging as _logging
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from pydantic import BaseModel, EmailStr, Field

logger = _logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Password hashing — Argon2id primary, bcrypt fallback + verify-both
# ---------------------------------------------------------------------------
_argon2_hasher = None
try:
    from argon2 import PasswordHasher as _Argon2PasswordHasher  # type: ignore
    from argon2.exceptions import VerifyMismatchError as _Argon2VerifyError  # type: ignore
    from argon2.exceptions import InvalidHash as _Argon2InvalidHash  # type: ignore

    # Argon2id with OWASP-recommended params
    _argon2_hasher = _Argon2PasswordHasher(
        time_cost=2,
        memory_cost=19456,  # 19 MiB
        parallelism=1,
        hash_len=32,
        salt_len=16,
    )
    logger.info("Password hashing: Argon2id available (argon2-cffi)")
except Exception as _argon2_import_exc:  # pragma: no cover - missing lib path
    _argon2_hasher = None
    _logging.getLogger(__name__).warning(
        f"Password hashing: argon2-cffi unavailable, falling back to bcrypt ({_argon2_import_exc})"
    )

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
_DEV_JWT_SECRET = "dev-admin-jwt-secret-please-change"
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = 8

def _is_production() -> bool:
    return os.environ.get("OAOS_ENV", "").strip().lower() in ("production", "prod")

def get_jwt_secret() -> str:
    """Dynamic JWT secret — reads env on each call, fail-closed in production."""
    key = os.environ.get("ADMIN_JWT_SECRET", _DEV_JWT_SECRET)
    if _is_production() and key == _DEV_JWT_SECRET:
        raise RuntimeError("ADMIN_JWT_SECRET must be set to a strong value when OAOS_ENV=production (fail-closed)")
    if _is_production() and len(key.strip()) < 32:
        raise RuntimeError("ADMIN_JWT_SECRET must be at least 32 characters when OAOS_ENV=production (fail-closed)")
    return key

def __getattr__(name: str):  # PEP 562 — dynamic env resolution, no stale snapshot
    if name == "JWT_SECRET":
        return get_jwt_secret()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

# Fail-closed at import when already in production (early crash), but rotation is still dynamic via get_jwt_secret
if _is_production():
    get_jwt_secret()

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


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1)
    new_password: str = Field(min_length=8)


class UpdateProfileRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=64)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int

# ---------------------------------------------------------------------------
# In-memory store (fallback cache — always kept in sync)
# ---------------------------------------------------------------------------
_users_by_id: dict[str, AdminUser] = {}
_users_by_email: dict[str, AdminUser] = {}

def _hash_password(password: str) -> str:
    """Hash with Argon2id when available, else bcrypt (with warning already logged at import)."""
    if _argon2_hasher is not None:
        try:
            return _argon2_hasher.hash(password)
        except Exception as exc:  # pragma: no cover - extremely rare
            logger.warning(f"Argon2id hash failed, falling back to bcrypt: {exc}")
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _verify_password(password: str, hashed: str) -> bool:
    """Verify against Argon2id or bcrypt — detects hash prefix."""
    if not hashed:
        return False
    # Argon2 hashes start with $argon2id$ / $argon2i$ / $argon2d$
    if hashed.startswith("$argon2"):
        if _argon2_hasher is None:
            # Argon2 hash but lib missing — cannot verify (fail closed, but log)
            logger.warning("Password verify: argon2 hash present but argon2-cffi unavailable")
            return False
        try:
            return _argon2_hasher.verify(hashed, password)
        except Exception:
            # VerifyMismatchError, InvalidHash, etc. -> wrong password
            return False
    # legacy bcrypt (and fallback)
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        # In case hash was argon2 but without prefix detection edge — try argon2 as last resort
        if _argon2_hasher is not None:
            try:
                return _argon2_hasher.verify(hashed, password)
            except Exception:
                pass
        return False


def _needs_rehash(hashed: str) -> bool:
    """True if stored hash is legacy bcrypt and Argon2id is available (opportunistic upgrade)."""
    if _argon2_hasher is None:
        return False
    return hashed.startswith("$2")


def _create_jwt(email: str, role: str) -> tuple[str, int]:
    expire = datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRE_HOURS)
    payload = {"sub": email, "role": role, "exp": expire}
    token = jwt.encode(payload, get_jwt_secret(), algorithm=JWT_ALGORITHM)
    expires_in = int(JWT_EXPIRE_HOURS * 3600)
    return token, expires_in


# ---------------------------------------------------------------------------
# DB helpers — lazy, import-free at module load
# ---------------------------------------------------------------------------
def _db_enabled() -> bool:
    """True when DATABASE_URL / OAOS_DATABASE_URL is configured for persistence."""
    try:
        # try admin-console persistence helper first (handles OAOS_DATABASE_URL priority)
        try:
            from persistence import get_database_url  # type: ignore
        except ImportError:
            from .persistence import get_database_url  # type: ignore
        url = get_database_url()
        return bool(url and url.strip())
    except Exception:
        # fallback: check env directly
        url = os.environ.get("OAOS_DATABASE_URL") or os.environ.get("DATABASE_URL")
        return bool(url and url.strip())


def _normalize_sync_url(url: str) -> str:
    """Normalize async URL to sync psycopg driver (postgres/sqlite compat).

    Uses psycopg (v3) driver which is installed as psycopg 3.3.4 / psycopg-binary.
    Bare postgresql:// would require psycopg2 which is NOT installed -> ModuleNotFoundError.
    """
    u = url.strip()
    if u.startswith("postgresql+asyncpg://"):
        u = u.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)
    elif "+asyncpg" in u:
        # generic +asyncpg -> +psycopg (covers postgresql+asyncpg edge)
        u = u.replace("+asyncpg", "+psycopg")
    elif u.startswith("postgresql://"):
        u = u.replace("postgresql://", "postgresql+psycopg://", 1)
    # sqlite+aiosqlite -> sqlite
    if "+aiosqlite" in u:
        u = u.replace("+aiosqlite", "")
        u = u.replace("sqlite+://", "sqlite://")
    if u.startswith("sqlite+"):
        u = u.replace("sqlite+", "sqlite", 1)
    # also handle bare sqlite+aiosqlite prefix
    if u.startswith("sqlite+aiosqlite://"):
        u = u.replace("sqlite+aiosqlite://", "sqlite://", 1)
    return u


def _to_orm(user: AdminUser):
    """Convert Pydantic AdminUser -> AdminUserORM (lazy import)."""
    try:
        from security.models.orm import AdminUserORM  # type: ignore
    except ImportError:
        # fallback: try absolute path via sys.path injection (tests)
        import sys
        from pathlib import Path

        sec_path = Path(__file__).resolve().parents[2] / "security"
        if str(sec_path) not in sys.path:
            # security package parent
            parent = Path(__file__).resolve().parents[2]
            if str(parent) not in sys.path:
                sys.path.insert(0, str(parent))
        from security.models.orm import AdminUserORM  # type: ignore
    orm = AdminUserORM(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=user.role.value if hasattr(user.role, "value") else str(user.role),
        hashed_password=user.hashed_password,
        created_at=user.created_at,
    )
    # extra column present on ORM — leave None
    return orm


def _from_orm(orm_obj) -> AdminUser:
    """Convert AdminUserORM row -> Pydantic AdminUser."""
    role_val = getattr(orm_obj, "role", "L4")
    # normalize role to AdminRole
    try:
        role = AdminRole(role_val)
    except Exception:
        role = AdminRole.L4
        if str(role_val).upper() == "L5":
            role = AdminRole.L5
    created_at = getattr(orm_obj, "created_at")
    if created_at is None:
        created_at = datetime.now(timezone.utc)
    elif created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return AdminUser(
        id=str(getattr(orm_obj, "id")),
        email=str(getattr(orm_obj, "email")),
        display_name=str(getattr(orm_obj, "display_name")),
        role=role,
        hashed_password=str(getattr(orm_obj, "hashed_password")),
        created_at=created_at,
    )


def _db_sync_url() -> str | None:
    """Return normalized sync URL or None if not configured."""
    try:
        try:
            from persistence import get_database_url  # type: ignore
        except ImportError:
            from .persistence import get_database_url  # type: ignore
        url = get_database_url()
    except Exception:
        url = os.environ.get("OAOS_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url or not url.strip():
        return None
    return _normalize_sync_url(url.strip())


def _db_ensure_table(engine) -> None:
    """Ensure admin_users table exists on the given sync engine (idempotent)."""
    production = os.environ.get("OAOS_ENV", "").strip().lower() in {"production", "prod"}
    if production:
        from sqlalchemy import inspect
        if not inspect(engine).has_table("admin_users"):
            raise RuntimeError("admin_users schema is missing; apply Alembic migrations before starting production")
        return
    # Prefer ORM metadata — compatibility is allowed only outside production.
    try:
        from security.models.orm import AdminUserORM  # type: ignore  # noqa: F401
        from security.models.db import Base  # type: ignore
        # Production schema is migration-owned; never mutate it implicitly at runtime.
        if os.environ.get("OAOS_ENV", "").strip().lower() in {"production", "prod"}:
            from sqlalchemy import inspect
            if not inspect(engine).has_table("admin_users"):
                raise RuntimeError("admin_users schema is missing; apply Alembic migrations before starting production")
            return
        # Non-production compatibility only: create fixture tables explicitly.
        Base.metadata.create_all(bind=engine)  # type: ignore[arg-type]
        # also ensure extra column exists for legacy DBs created via raw DDL
        try:
            from sqlalchemy import text
            with engine.begin() as conn:
                # sqlite: add extra if missing (ignore if exists)
                try:
                    conn.execute(text("ALTER TABLE admin_users ADD COLUMN extra TEXT"))
                except Exception:
                    pass
                try:
                    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_admin_users_email ON admin_users (email)"))
                except Exception:
                    pass
        except Exception:
            pass
        return
    except Exception:
        pass
    try:
        from sqlalchemy import text
    except Exception:
        return
    ddl_sqlite = """
    CREATE TABLE IF NOT EXISTS admin_users (
        id TEXT PRIMARY KEY,
        email TEXT UNIQUE NOT NULL,
        display_name TEXT NOT NULL,
        role TEXT NOT NULL,
        hashed_password TEXT NOT NULL,
        created_at TEXT NOT NULL,
        extra TEXT
    )
    """
    ddl_pg = """
    CREATE TABLE IF NOT EXISTS admin_users (
        id TEXT PRIMARY KEY,
        email TEXT UNIQUE NOT NULL,
        display_name TEXT NOT NULL,
        role TEXT NOT NULL,
        hashed_password TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        extra JSONB
    )
    """
    url_str = str(getattr(engine, "url", ""))
    is_sqlite = url_str.startswith("sqlite")
    ddl = ddl_sqlite if is_sqlite else ddl_pg
    try:
        with engine.begin() as conn:
            conn.execute(text(ddl))
            # legacy fix: add extra column if table was created by older DDL
            try:
                conn.execute(text("ALTER TABLE admin_users ADD COLUMN extra TEXT"))
            except Exception:
                pass
    except Exception:
        pass


def _db_get_session():
    """Create a sync session + engine pair (caller must close / dispose). Lazy imports."""
    url = _db_sync_url()
    if not url:
        return None, None
    try:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
    except Exception:
        return None, None
    try:
        # sqlite memory needs check_same_thread=False for test compatibility
        connect_args = {}
        if url.startswith("sqlite"):
            connect_args = {"check_same_thread": False}
        engine = create_engine(url, echo=False, pool_pre_ping=False, connect_args=connect_args)
        _db_ensure_table(engine)
        Session = sessionmaker(bind=engine, expire_on_commit=False)
        session = Session()
        return session, engine
    except Exception:
        return None, None


def _db_close(session, engine) -> None:
    try:
        if session is not None:
            session.close()
    except Exception:
        pass
    try:
        if engine is not None:
            engine.dispose()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Seed
# ---------------------------------------------------------------------------
def _has_existing_admin() -> bool:
    """True if any admin exists in cache or DB (DB best-effort)."""
    if _users_by_email:
        return True
    if _db_enabled():
        try:
            session, engine = _db_get_session()
            if session is not None:
                try:
                    from security.models.orm import AdminUserORM  # type: ignore
                    cnt = session.query(AdminUserORM).count()  # type: ignore
                    return cnt > 0
                finally:
                    _db_close(session, engine)
        except Exception:
            pass
    return False

def _seed_admin() -> None:
    """Create seed/bootstrap admin — production fail-closed, dev/test seed."""
    if _is_production():
        if _has_existing_admin():
            return
        _bootstrap_pw = (os.environ.get("OAOS_ADMIN_BOOTSTRAP_PASSWORD") or os.environ.get("OAOS_ADMIN_BOOTSTRAP_TOKEN") or "").strip()
        if not _bootstrap_pw:
            raise RuntimeError("OAOS_ENV=production requires existing admin or OAOS_ADMIN_BOOTSTRAP_PASSWORD (fail-closed: no default Admin123! seed in production)")
        if len(_bootstrap_pw) < 12:
            raise RuntimeError("OAOS_ADMIN_BOOTSTRAP_PASSWORD must be at least 12 characters when OAOS_ENV=production (fail-closed)")
        _bootstrap_email = os.environ.get("OAOS_ADMIN_BOOTSTRAP_EMAIL", "").strip() or "admin@openit.co.kr"
        logger.info("Admin bootstrap: creating initial L5 admin in production (email=%s)", _bootstrap_email)
        uid = f"admin_{uuid.uuid4().hex[:8]}"
        user = AdminUser(
            id=uid,
            email=_bootstrap_email,
            display_name="Infra Admin",
            role=AdminRole.L5,
            hashed_password=_hash_password(_bootstrap_pw),
            created_at=datetime.now(timezone.utc),
        )
        _users_by_id[uid] = user
        _users_by_email[_bootstrap_email] = user
        # persist bootstrap admin to DB if enabled (best-effort, never log secret)
        if _db_enabled():
            try:
                session, engine = _db_get_session()
                if session is not None:
                    try:
                        from security.models.orm import AdminUserORM  # type: ignore
                        existing = session.query(AdminUserORM).filter(AdminUserORM.email == _bootstrap_email).first()  # type: ignore
                        if existing is None:
                            orm = _to_orm(user)
                            session.add(orm)
                            session.commit()
                        else:
                            db_user = _from_orm(existing)
                            _users_by_id[db_user.id] = db_user
                            _users_by_email[db_user.email] = db_user
                            if db_user.id != uid and uid in _users_by_id:
                                del _users_by_id[uid]
                    except Exception:
                        try:
                            session.rollback()
                        except Exception:
                            pass
                    finally:
                        _db_close(session, engine)
            except Exception:
                pass
        return
    # Non-production: dev/test seed (preserves existing tests)
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
    # persist to DB if enabled (best-effort, never raise)
    if _db_enabled():
        try:
            session, engine = _db_get_session()
            if session is not None:
                try:
                    from security.models.orm import AdminUserORM  # lazy
                    existing = session.query(AdminUserORM).filter(AdminUserORM.email == email).first()  # type: ignore
                    if existing is None:
                        orm = _to_orm(user)
                        session.add(orm)
                        session.commit()
                    else:
                        # already exists — hydrate cache from DB to keep ids in sync
                        db_user = _from_orm(existing)
                        _users_by_id[db_user.id] = db_user
                        _users_by_email[db_user.email] = db_user
                        # remove the temp uid entry if ids differ
                        if db_user.id != uid and uid in _users_by_id:
                            del _users_by_id[uid]
                except Exception:
                    try:
                        session.rollback()
                    except Exception:
                        pass
                finally:
                    _db_close(session, engine)
        except Exception:
            pass


_seed_admin()


# ---------------------------------------------------------------------------
# Helpers (for testing / infra) — DB first, fallback to cache
# ---------------------------------------------------------------------------
def get_user_by_email(email: str) -> Optional[AdminUser]:
    if _db_enabled():
        try:
            session, engine = _db_get_session()
            if session is not None:
                try:
                    from security.models.orm import AdminUserORM  # type: ignore
                    row = session.query(AdminUserORM).filter(AdminUserORM.email == email).first()  # type: ignore
                    if row is not None:
                        user = _from_orm(row)
                        # keep cache warm
                        _users_by_id[user.id] = user
                        _users_by_email[user.email] = user
                        return user
                finally:
                    _db_close(session, engine)
        except Exception:
            pass
    user = _users_by_email.get(email)
    if user is not None:
        return user
    if _is_production():
        return None
    for mod in list(sys.modules.values()):
        try:
            users_by_email = getattr(mod, "_users_by_email", None)
            if isinstance(users_by_email, dict) and email in users_by_email:
                found_user = users_by_email[email]
                _users_by_id[found_user.id] = found_user
                _users_by_email[email] = found_user
                return found_user
        except Exception:
            continue
    return None


def get_user_by_id(uid: str) -> Optional[AdminUser]:
    if _db_enabled():
        try:
            session, engine = _db_get_session()
            if session is not None:
                try:
                    from security.models.orm import AdminUserORM  # type: ignore
                    row = session.query(AdminUserORM).filter(AdminUserORM.id == uid).first()  # type: ignore
                    if row is not None:
                        user = _from_orm(row)
                        _users_by_id[user.id] = user
                        _users_by_email[user.email] = user
                        return user
                finally:
                    _db_close(session, engine)
        except Exception:
            pass
    user = _users_by_id.get(uid)
    if user is not None:
        return user
    if _is_production():
        return None
    for mod in list(sys.modules.values()):
        try:
            users_by_id = getattr(mod, "_users_by_id", None)
            if isinstance(users_by_id, dict) and uid in users_by_id:
                found_user = users_by_id[uid]
                _users_by_id[uid] = found_user
                _users_by_email[found_user.email] = found_user
                return found_user
        except Exception:
            continue
    return None


def clear_users() -> None:
    """Test helper — clear all users then re-seed (DB + cache)."""
    _users_by_id.clear()
    _users_by_email.clear()
    if _db_enabled():
        try:
            session, engine = _db_get_session()
            if session is not None:
                try:
                    from security.models.orm import AdminUserORM  # type: ignore
                    session.query(AdminUserORM).delete()  # type: ignore
                    session.commit()
                except Exception:
                    try:
                        session.rollback()
                    except Exception:
                        pass
                finally:
                    _db_close(session, engine)
        except Exception:
            pass
    _seed_admin()


def list_users() -> list[AdminUser]:
    if _db_enabled():
        try:
            session, engine = _db_get_session()
            if session is not None:
                try:
                    from security.models.orm import AdminUserORM  # type: ignore
                    rows = session.query(AdminUserORM).all()  # type: ignore
                    users = [_from_orm(r) for r in rows]
                    # refresh cache
                    _users_by_id.clear()
                    _users_by_email.clear()
                    for u in users:
                        _users_by_id[u.id] = u
                        _users_by_email[u.email] = u
                    return users
                finally:
                    _db_close(session, engine)
        except Exception:
            pass
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
        payload = jwt.decode(token, get_jwt_secret(), algorithms=[JWT_ALGORITHM])
        email: str | None = payload.get("sub")
        if email is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    except JWTError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=f"Invalid token: {e}")
    user = get_user_by_email(email)
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
    # check duplicate via DB-aware helper
    if get_user_by_email(req.email) is not None:
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
    # try DB first
    if _db_enabled():
        try:
            session, engine = _db_get_session()
            if session is not None:
                try:
                    from security.models.orm import AdminUserORM  # type: ignore
                    # double-check duplicate inside DB tx
                    existing = session.query(AdminUserORM).filter(AdminUserORM.email == req.email).first()  # type: ignore
                    if existing is not None:
                        raise HTTPException(status_code=409, detail="Email already registered")
                    orm = _to_orm(user)
                    session.add(orm)
                    session.commit()
                    _users_by_id[uid] = user
                    _users_by_email[req.email] = user
                    return AdminUserPublic(**user.model_dump(exclude={"hashed_password"}))
                except HTTPException:
                    try:
                        session.rollback()
                    except Exception:
                        pass
                    raise
                except Exception:
                    try:
                        session.rollback()
                    except Exception:
                        pass
                    # fall through to cache fallback
                finally:
                    _db_close(session, engine)
        except HTTPException:
            raise
        except Exception:
            pass
    _users_by_id[uid] = user
    _users_by_email[req.email] = user
    return AdminUserPublic(**user.model_dump(exclude={"hashed_password"}))


@router.post("/login", response_model=TokenResponse)
def login(req: LoginRequest):
    user = get_user_by_email(req.email)
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
    users = list_users()
    return [AdminUserPublic(**u.model_dump(exclude={"hashed_password"})) for u in users]


@router.delete("/users/{user_id}")
def delete_admin_user(user_id: str, admin: AdminUser = Depends(require_l5)):
    """Delete admin user — L5 only, self-delete blocked."""
    if admin.id == user_id:
        raise HTTPException(status_code=400, detail="자기 자신은 삭제할 수 없습니다")
    target = get_user_by_id(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="user not found")
    # try DB delete first
    if _db_enabled():
        try:
            session, engine = _db_get_session()
            if session is not None:
                try:
                    from security.models.orm import AdminUserORM  # type: ignore
                    row = session.query(AdminUserORM).filter(AdminUserORM.id == user_id).first()  # type: ignore
                    if row is None:
                        raise HTTPException(status_code=404, detail="user not found")
                    session.delete(row)
                    session.commit()
                except HTTPException:
                    try:
                        session.rollback()
                    except Exception:
                        pass
                    raise
                except Exception:
                    try:
                        session.rollback()
                    except Exception:
                        pass
                finally:
                    _db_close(session, engine)
        except HTTPException:
            raise
        except Exception:
            pass
    # remove from cache
    if user_id in _users_by_id:
        del _users_by_id[user_id]
    for em, u in list(_users_by_email.items()):
        if u.id == user_id:
            del _users_by_email[em]
            break
    return {"status": "deleted", "id": user_id}


@router.post("/change-password")
def change_password(req: ChangePasswordRequest, admin: AdminUser = Depends(get_current_admin)):
    """Change own password — requires current password verification."""
    # always re-fetch fresh user (DB-aware)
    fresh = get_user_by_email(admin.email)
    if fresh is None:
        fresh = admin
    if not _verify_password(req.current_password, fresh.hashed_password):
        raise HTTPException(status_code=401, detail="현재 비밀번호가 일치하지 않습니다")
    new_hashed = _hash_password(req.new_password)
    if _db_enabled():
        try:
            session, engine = _db_get_session()
            if session is not None:
                try:
                    from security.models.orm import AdminUserORM  # type: ignore
                    row = session.query(AdminUserORM).filter(AdminUserORM.email == fresh.email).first()  # type: ignore
                    if row is not None:
                        row.hashed_password = new_hashed  # type: ignore
                        session.commit()
                except Exception:
                    try:
                        session.rollback()
                    except Exception:
                        pass
                finally:
                    _db_close(session, engine)
        except Exception:
            pass
    # update cache object(s)
    fresh.hashed_password = new_hashed
    admin.hashed_password = new_hashed
    if fresh.id in _users_by_id:
        _users_by_id[fresh.id].hashed_password = new_hashed
    if fresh.email in _users_by_email:
        _users_by_email[fresh.email].hashed_password = new_hashed
    return {"status": "changed"}


@router.patch("/me", response_model=AdminUserPublic)
def update_profile(req: UpdateProfileRequest, admin: AdminUser = Depends(get_current_admin)):
    """Update own display_name."""
    fresh = get_user_by_email(admin.email)
    if fresh is None:
        fresh = admin
    if _db_enabled():
        try:
            session, engine = _db_get_session()
            if session is not None:
                try:
                    from security.models.orm import AdminUserORM  # type: ignore
                    row = session.query(AdminUserORM).filter(AdminUserORM.email == fresh.email).first()  # type: ignore
                    if row is not None:
                        row.display_name = req.display_name  # type: ignore
                        session.commit()
                except Exception:
                    try:
                        session.rollback()
                    except Exception:
                        pass
                finally:
                    _db_close(session, engine)
        except Exception:
            pass
    fresh.display_name = req.display_name
    admin.display_name = req.display_name
    if fresh.id in _users_by_id:
        _users_by_id[fresh.id].display_name = req.display_name
    if fresh.email in _users_by_email:
        _users_by_email[fresh.email].display_name = req.display_name
    return AdminUserPublic(**fresh.model_dump(exclude={"hashed_password"}))
