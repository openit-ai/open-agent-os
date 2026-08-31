"""Open Agent OS — Security & Governance FastAPI (Section 7.3).
Endpoints:
  POST /v1/policy/evaluate
  POST /v1/delegation/grant
  POST /v1/token/issue
  POST /v1/approval/request
  POST /v1/audit/verify
"""
from __future__ import annotations

import sys
import os

# security 하위 패키지 경로를 sys.path에 추가 (editable install 없이 동작)
# - policy_engine, vault, crypto 는 2단계 패키지이므로 전용 경로 필요
# - delegation/approval/audit/token 은 security 루트 자체로 resolve
_sys_root = os.path.dirname(__file__)
sys.path.insert(0, _sys_root)  # delegation, approval, audit
sys.path.insert(0, os.path.join(_sys_root, "policy-engine"))
sys.path.insert(0, os.path.join(_sys_root, "credential-vault"))
sys.path.insert(0, os.path.join(_sys_root, "token"))
sys.path.insert(0, os.path.join(_sys_root, "crypto"))

from datetime import datetime, timezone
from typing import Optional
import time

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel

# C1: verified bearer JWT or mTLS — health remains public
# Robust import: use package-qualified or file location, never bare 'auth' which collides with admin-console/backend/auth.py
try:
    from security.auth import verify_security_auth, verify_tenant_binding  # type: ignore
except ImportError:
    try:
        from auth import verify_security_auth, verify_tenant_binding  # type: ignore  # fallback for direct execution without package
        # validate it is security auth (has verify_tenant_binding and ALLOWED_AUDIENCE)
        if getattr(sys.modules.get("auth"), "ALLOWED_AUDIENCE", None) != "security":
            raise ImportError("bare auth collision, not security.auth")
    except ImportError:
        import importlib.util as _ilu_auth
        import pathlib as _pl_auth
        import sys as _sys_auth
        _auth_path = _pl_auth.Path(__file__).parent / "auth.py"
        _spec_auth = _ilu_auth.spec_from_file_location("_security_auth_impl", str(_auth_path))
        _mod_auth = _ilu_auth.module_from_spec(_spec_auth)  # type: ignore
        _sys_auth.modules["_security_auth_impl"] = _mod_auth
        _spec_auth.loader.exec_module(_mod_auth)  # type: ignore
        verify_security_auth = _mod_auth.verify_security_auth  # type: ignore
        verify_tenant_binding = _mod_auth.verify_tenant_binding  # type: ignore

# 모델 import
from policy_model import PolicyBundle, PolicyDecision, PolicyEvaluationRequest, PolicyEvaluationResult
from policy_engine.engine import PolicyEngine
from policy_engine.default_bundle import default_bundle
from delegation_model import Delegation
from delegation.delegation_service.service import DelegationService
from token_service.service import TokenService, clear_global_stores
from approval.approval_workflow.workflow import ApprovalStore, ApprovalDecision
from audit.audit_ledger.ledger import AuditLedger
import hashlib
import uuid

from audit_model import AuditEvent, AuditEventType

app = FastAPI(title="Open Agent OS — Security & Governance", version="0.1.3")

# ── 전역 싱글톤 (프로세스 내 공유) ──────────────────────────────
_DEV_SIGNING_KEY = "dev-signing-key-please-change"
# One explicit env-configured signing key contract: OAOS_SECURITY_SERVICE_SIGNING_KEY primary, OAOS_SIGNING_KEY fallback
# Dynamic getter — no stale snapshot, rotation-safe, production fail-closed
def get_signing_key() -> str:
    key = os.environ.get("OAOS_SECURITY_SERVICE_SIGNING_KEY") or os.environ.get("OAOS_SIGNING_KEY") or _DEV_SIGNING_KEY
    if os.environ.get("OAOS_ENV", "").lower() == "production" and key == _DEV_SIGNING_KEY:
        raise RuntimeError("OAOS_SIGNING_KEY must be set to a strong value when OAOS_ENV=production (fail-closed)")
    return key

def __getattr__(name: str):  # PEP 562 — dynamic env resolution, no stale snapshot
    if name == "SIGNING_KEY":
        return get_signing_key()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

# Early fail-closed at import if already production
if os.environ.get("OAOS_ENV", "").lower() == "production" and (os.environ.get("OAOS_SECURITY_SERVICE_SIGNING_KEY") or os.environ.get("OAOS_SIGNING_KEY") or _DEV_SIGNING_KEY) == _DEV_SIGNING_KEY:
    # validate via getter (raises if dev key in prod)
    get_signing_key()

def _ensure_signing_key_fresh() -> None:
    """Refresh singleton signing keys from env on each request — rotation-safe, preserves in-memory stores."""
    try:
        fresh = get_signing_key()
    except RuntimeError:
        raise
    # Update in place so existing object identity (and in-memory nonce stores) is preserved
    for svc in (token_service, approval_store, audit_ledger):
        try:
            if hasattr(svc, "signing_key") and getattr(svc, "signing_key") != fresh:
                svc.signing_key = fresh
        except Exception:
            pass
    # Also handle TokenService's alternate attribute names
    for svc in (token_service,):
        for attr in ("signing_key", "_signing_key"):
            try:
                if hasattr(svc, attr) and getattr(svc, attr) != fresh:
                    setattr(svc, attr, fresh)
            except Exception:
                pass

ENCRYPTION_KEY = os.environ.get("OAOS_ENCRYPTION_KEY", "dev-encryption-key-32bytes!!").encode()

delegation_service = DelegationService()
token_service = TokenService(signing_key=get_signing_key())
approval_store = ApprovalStore(signing_key=get_signing_key())
audit_ledger = AuditLedger(signing_key=get_signing_key())
policy_engine = PolicyEngine(bundles=[default_bundle(tenant_id="default")])

@app.middleware("http")
async def _refresh_signing_key_middleware(request: Request, call_next):  # rotation-safe, no stale snapshot
    try:
        _ensure_signing_key_fresh()
    except RuntimeError:
        # fail-closed in production — propagate as 503 so token/approval/audit signing doesn't use stale key
        from fastapi.responses import JSONResponse
        if os.environ.get("OAOS_ENV", "").lower() == "production":
            return JSONResponse(status_code=503, content={"detail": "signing key not configured in production"})
    return await call_next(request)

# ── revoke cascade wiring (MemoryStore + Vault) — lazy, best-effort ──
# DelegationService.revoke() will try these; wiring here ensures app singleton is connected.
vault_instance = None
_memory_store_instance = None
try:
    try:
        from governance.governance import get_default_store as _get_mem_store  # type: ignore
    except ImportError:
        from security.memory_governance.governance.governance import get_default_store as _get_mem_store  # type: ignore
    _memory_store_instance = _get_mem_store()
    try:
        delegation_service.set_memory_store(_memory_store_instance)
    except Exception:
        pass
except Exception:
    pass
try:
    # create vault singleton for cascade (reuses ENCRYPTION_KEY); lazy so tests without DB still pass
    try:
        from vault.vault import EncryptedPostgresVault  # type: ignore
    except ImportError:
        try:
            from security.credential_vault.vault.vault import EncryptedPostgresVault  # type: ignore
        except Exception:
            EncryptedPostgresVault = None  # type: ignore
    if EncryptedPostgresVault is not None:
        try:
            vault_instance = EncryptedPostgresVault(encryption_key=ENCRYPTION_KEY, audit_ledger=audit_ledger, delegation_service=delegation_service)
            try:
                delegation_service.set_vault(vault_instance)
            except Exception:
                pass
        except Exception:
            vault_instance = None
except Exception:
    pass


# ── Request / Response 모델 ────────────────────────────────────
class DelegationGrantRequest(BaseModel):
    user_id: str
    agent_id: str
    provider: str
    scope: str


class DelegationRevokeRequest(BaseModel):
    delegation_id: str


class TokenIssueRequest(BaseModel):
    sub: str
    on_behalf_of: str
    action: str
    resource: str
    session_id: str
    request_id: str
    delegation_id: str | None = None
    ttl_seconds: int = 300


class TokenVerifyRequest(BaseModel):
    token: str


class ApprovalRequestBody(BaseModel):
    user_id: str
    agent_id: str
    action: str
    resource: str
    risk: str = "HIGH"
    ttl_minutes: int = 60


class ApprovalDecideBody(BaseModel):
    approval_id: str
    decision: ApprovalDecision
    decided_by: str
    group_id: str | None = None


class AuditVerifyRequest(BaseModel):
    chain_head_hash: str | None = None
    signature: str | None = None


# ── Health ─────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok"}


def _is_production() -> bool:
    for k in ("OAOS_ENV", "ENV", "OAOS_ENVIRONMENT", "APP_ENV", "ENVIRONMENT"):
        if os.getenv(k, "").strip().lower() in ("production", "prod"):
            return True
    return False

_shutting_down: bool = False
_active_requests: int = 0

def _handle_sigterm_sec(signum, frame):
    global _shutting_down
    _shutting_down = True
    try:
        import logging as _lg
        _lg.getLogger(__name__).warning("SIGTERM draining security %s", _active_requests)
    except Exception:
        pass

try:
    import signal as _sig
    _sig.signal(_sig.SIGTERM, _handle_sigterm_sec)
except Exception:
    pass

@app.middleware("http")
async def _track_active_sec(request: Request, call_next):
    global _active_requests
    _active_requests += 1
    try:
        return await call_next(request)
    finally:
        _active_requests -= 1

def _check_latency(fn):
    start = time.monotonic()
    try:
        fn()
        latency = round((time.monotonic() - start) * 1000, 2)
        return {"status": "ok", "latency_ms": latency}
    except Exception as e:
        latency = round((time.monotonic() - start) * 1000, 2)
        return {"status": "degraded", "latency_ms": latency, "error": str(e)[:200]}

def _bounded_db_ping(db_url: str, timeout_s: float = 0.8) -> None:
    if "://" not in db_url:
        raise RuntimeError("invalid db url")
    if db_url.startswith("sqlite") and (":memory:" in db_url or "mode=memory" in db_url):
        return
    try:
        from sqlalchemy import create_engine, text  # type: ignore
        sync_url = db_url
        if sync_url.startswith("postgresql+asyncpg://"):
            sync_url = sync_url.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)
        elif sync_url.startswith("postgresql://"):
            sync_url = sync_url.replace("postgresql://", "postgresql+psycopg://", 1)
        if "+aiosqlite" in sync_url:
            sync_url = sync_url.replace("+aiosqlite", "")
        kwargs: dict = {}
        if sync_url.startswith("postgresql"):
            kwargs = {"connect_args": {"connect_timeout": timeout_s}}  # type: ignore
        elif sync_url.startswith("sqlite"):
            kwargs = {"connect_args": {"timeout": timeout_s}}
        eng = create_engine(sync_url, **kwargs, pool_pre_ping=False)  # type: ignore
        import concurrent.futures
        def _ping():
            with eng.connect() as conn:
                conn.execute(text("SELECT 1"))
        ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            fut = ex.submit(_ping)
            fut.result(timeout=timeout_s + 0.5)
        finally:
            try:
                ex.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                ex.shutdown(wait=False)
        try:
            eng.dispose()
        except Exception:
            pass
    except RuntimeError:
        raise
    except Exception as e:
        msg = str(e).lower()
        if "no such module" in msg or "could not parse" in msg or "not found" in msg:
            return
        raise RuntimeError(f"db ping failed: {e}") from e

def _bounded_redis_ping(redis_url: str, timeout_s: float = 0.8) -> None:
    if "://" not in redis_url:
        raise RuntimeError("invalid redis url")
    try:
        import redis as _redis  # type: ignore
        client = _redis.Redis.from_url(redis_url, socket_connect_timeout=timeout_s, socket_timeout=timeout_s)
        import concurrent.futures
        def _ping():
            client.ping()
        ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            fut = ex.submit(_ping)
            fut.result(timeout=timeout_s + 0.5)
        finally:
            try:
                ex.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                ex.shutdown(wait=False)
        try:
            client.close()
        except Exception:
            pass
    except RuntimeError:
        raise
    except Exception as e:
        msg = str(e).lower()
        if "no module" in msg:
            return
        raise RuntimeError(f"redis ping failed: {e}") from e

def _bounded_vault_ping(timeout_s: float = 0.8) -> None:
    vault_addr = (os.getenv("VAULT_ADDR", "") or "").strip()
    vault_backend = (os.getenv("VAULT_BACKEND", "") or "").strip().lower()
    legacy = {"", "encrypted_postgres", "encrypted-postgres", "legacy", "postgres", "none"}
    configured = bool(vault_addr) or (vault_backend and vault_backend not in legacy)
    if not configured:
        raise RuntimeError("vault not configured")
    try:
        if vault_addr:
            import concurrent.futures as _cf
            def _http_check():
                try:
                    try:
                        import httpx  # type: ignore
                        import asyncio as _asyncio
                        async def _do():
                            async with httpx.AsyncClient(timeout=timeout_s) as client:
                                resp = await client.get(vault_addr.rstrip("/") + "/v1/sys/health", headers={})
                                if resp.status_code not in (200, 204, 429, 472, 473):
                                    raise RuntimeError(f"vault health {resp.status_code}")
                        _asyncio.run(_do())
                        return
                    except ImportError:
                        pass
                    except Exception as e:
                        raise e
                    import urllib.request
                    import ssl
                    ctx = ssl._create_unverified_context() if vault_addr.startswith("https") else None
                    req = urllib.request.Request(vault_addr.rstrip("/") + "/v1/sys/health")
                    if os.getenv("VAULT_TOKEN"):
                        req.add_header("X-Vault-Token", os.getenv("VAULT_TOKEN", ""))
                    with urllib.request.urlopen(req, timeout=timeout_s, context=ctx) as resp:  # type: ignore
                        code = getattr(resp, "status", 200) or 200
                        if code not in (200, 204, 429, 472, 473):
                            raise RuntimeError(f"vault health {code}")
                except RuntimeError:
                    raise
                except Exception as e:
                    raise RuntimeError(f"vault ping failed: {e}") from e
            ex = _cf.ThreadPoolExecutor(max_workers=1)
            try:
                fut = ex.submit(_http_check)
                fut.result(timeout=timeout_s + 0.5)
            finally:
                try:
                    ex.shutdown(wait=False, cancel_futures=True)
                except TypeError:
                    ex.shutdown(wait=False)
            return
        try:
            from vault.external import get_vault_backend  # type: ignore
            be = get_vault_backend()
            if be is None:
                return
            import concurrent.futures as _cf2
            import asyncio as _asyncio2
            def _be_check():
                try:
                    ok = _asyncio2.run(be.health_check())  # type: ignore
                    if not ok:
                        raise RuntimeError("vault backend health_check false")
                except Exception as e:
                    raise RuntimeError(f"vault backend health failed: {e}") from e
            ex2 = _cf2.ThreadPoolExecutor(max_workers=1)
            try:
                fut = ex2.submit(_be_check)
                fut.result(timeout=timeout_s + 0.5)
            finally:
                try:
                    ex2.shutdown(wait=False, cancel_futures=True)
                except TypeError:
                    ex2.shutdown(wait=False)
        except Exception as e:
            raise RuntimeError(f"vault ping failed: {e}") from e
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"vault ping failed: {e}") from e

def _ha_checks():
    checks: dict = {}
    db_url = os.getenv("DATABASE_URL", "") or os.getenv("OAOS_DATABASE_URL", "")
    if db_url:
        def _db():
            _bounded_db_ping(db_url)
        checks["db"] = _check_latency(_db)
    else:
        checks["db"] = {"status": "skipped", "latency_ms": 0, "reason": "no DATABASE_URL"}
    redis_url = os.getenv("REDIS_URL", "")
    if redis_url:
        def _redis():
            _bounded_redis_ping(redis_url)
        checks["redis"] = _check_latency(_redis)
    else:
        checks["redis"] = {"status": "skipped", "latency_ms": 0, "reason": "no REDIS_URL"}
    vault_addr = (os.getenv("VAULT_ADDR", "") or "").strip()
    vault_backend = (os.getenv("VAULT_BACKEND", "") or "").strip().lower()
    legacy = {"", "encrypted_postgres", "encrypted-postgres", "legacy", "postgres", "none"}
    vault_configured = bool(vault_addr) or (vault_backend and vault_backend not in legacy)
    if vault_configured:
        def _vault():
            _bounded_vault_ping()
        checks["vault"] = _check_latency(_vault)
    else:
        checks["vault"] = {"status": "skipped", "latency_ms": 0, "reason": "no VAULT_ADDR/VAULT_BACKEND"}
    if _shutting_down:
        checks["self"] = {"status": "draining", "latency_ms": 0, "active_requests": _active_requests}
    else:
        checks["self"] = {"status": "ok", "latency_ms": 0, "active_requests": _active_requests}
    return checks

@app.get("/healthz")
def healthz():
    return {"status": "ok", "service": "security"}

@app.get("/readyz")
def readyz():
    checks = _ha_checks()
    degraded = any(v.get("status") in ("degraded", "draining") for v in checks.values())
    draining = checks.get("self", {}).get("status") == "draining"
    status = "draining" if draining else ("degraded" if degraded else "ok")
    body = {"status": status, "service": "security", "checks": checks}
    if (degraded or draining) and _is_production():
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=503, content=body)
    return body

@app.get("/v1/health/detailed")
def health_detailed():
    start = time.monotonic()
    checks = _ha_checks()
    total = round((time.monotonic() - start) * 1000, 2)
    degraded = any(v.get("status") in ("degraded", "draining") for v in checks.values())
    draining = checks.get("self", {}).get("status") == "draining"
    status = "draining" if draining else ("degraded" if degraded else "ok")
    return {"status": status, "service": "security", "checks": checks, "latency_ms": total, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


# ── Policy evaluate ────────────────────────────────────────────
@app.post("/v1/policy/evaluate", response_model=PolicyEvaluationResult)
def policy_evaluate(req: PolicyEvaluationRequest, payload: dict = Depends(verify_security_auth)):
    """Section 25 — deterministic policy evaluation. Auth: verified JWT/mTLS + tenant binding."""
    verify_tenant_binding(payload, req.tenant_id)
    result = policy_engine.evaluate(req)
    # audit 기록
    try:
        evt = AuditEvent(
            event_id=f"evt_{uuid.uuid4().hex[:12]}",
            event_type=AuditEventType.POLICY_DECISION,
            timestamp=datetime.now(timezone.utc),
            tenant_id=req.tenant_id,
            user_id=req.user_id,
            agent_id=req.agent_id,
            resource=req.resource,
            action=req.action,
            decision=result.decision.value,
            policy_version=result.policy_version,
        )
        audit_ledger.append(evt)
    except Exception:
        pass
    return result


# ── Delegation ─────────────────────────────────────────────────
@app.post("/v1/delegation/grant", response_model=Delegation)
def delegation_grant(req: DelegationGrantRequest, payload: dict = Depends(verify_security_auth)):
    d = delegation_service.grant(
        user_id=req.user_id, agent_id=req.agent_id, provider=req.provider, scope=req.scope
    )
    try:
        evt = AuditEvent(
            event_id=f"evt_{uuid.uuid4().hex[:12]}",
            event_type=AuditEventType.DELEGATION_CREATED,
            timestamp=datetime.now(timezone.utc),
            tenant_id="default",
            user_id=req.user_id,
            agent_id=req.agent_id,
            delegation_id=d.id,
        )
        audit_ledger.append(evt)
    except Exception:
        pass
    return d


@app.post("/v1/delegation/revoke")
def delegation_revoke(req: DelegationRevokeRequest, payload: dict = Depends(verify_security_auth)):
    d = delegation_service.revoke(req.delegation_id)
    if d is None:
        raise HTTPException(status_code=404, detail="delegation not found")
    try:
        evt = AuditEvent(
            event_id=f"evt_{uuid.uuid4().hex[:12]}",
            event_type=AuditEventType.DELEGATION_REVOKED,
            timestamp=datetime.now(timezone.utc),
            tenant_id="default",
            user_id=d.user_id,
            agent_id=d.agent_id,
            delegation_id=d.id,
        )
        audit_ledger.append(evt)
    except Exception:
        pass
    return {"status": "revoked", "delegation_id": d.id, "delegation": d}


@app.get("/v1/delegation/{delegation_id}")
def delegation_get(delegation_id: str, payload: dict = Depends(verify_security_auth)):
    d = delegation_service.get(delegation_id)
    if d is None:
        raise HTTPException(status_code=404, detail="not found")
    return d


# ── Token ──────────────────────────────────────────────────────
@app.post("/v1/token/issue")
def token_issue(req: TokenIssueRequest, payload: dict = Depends(verify_security_auth)):
    # C1 tenant binding: JWT tenant must match requested capability? Enforce sub mismatch 403
    if payload.get("sub") and payload.get("sub") != req.sub:
        raise HTTPException(status_code=403, detail="token sub mismatch: JWT sub != body sub")
    # revoke 된 delegation 으로는 발급 불가
    if req.delegation_id and not delegation_service.is_active(req.delegation_id):
        raise HTTPException(status_code=403, detail="delegation not active or revoked")
    token = token_service.issue(
        sub=req.sub,
        on_behalf_of=req.on_behalf_of,
        action=req.action,
        resource=req.resource,
        session_id=req.session_id,
        request_id=req.request_id,
        delegation_id=req.delegation_id,
        ttl_seconds=req.ttl_seconds,
    )
    try:
        evt = AuditEvent(
            event_id=f"evt_{uuid.uuid4().hex[:12]}",
            event_type=AuditEventType.CAPABILITY_ISSUED,
            timestamp=datetime.now(timezone.utc),
            tenant_id="default",
            user_id=req.on_behalf_of,
            agent_id=req.sub,
            resource=req.resource,
            action=req.action,
            delegation_id=req.delegation_id,
        )
        audit_ledger.append(evt)
    except Exception:
        pass
    return {"token": token}


@app.post("/v1/token/verify")
def token_verify(req: TokenVerifyRequest, payload: dict = Depends(verify_security_auth)):
    try:
        inner = token_service.verify(req.token)
        return {"valid": True, "payload": inner}
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e))


@app.post("/v1/token/revoke")
def token_revoke(req: TokenVerifyRequest, payload: dict = Depends(verify_security_auth)):
    token_service.revoke(req.token)
    return {"status": "revoked"}


# ── Approval ───────────────────────────────────────────────────
@app.post("/v1/approval/request")
def approval_request(req: ApprovalRequestBody, payload: dict = Depends(verify_security_auth)):
    ar = approval_store.create(
        user_id=req.user_id,
        agent_id=req.agent_id,
        action=req.action,
        resource=req.resource,
        risk=req.risk,
        ttl_minutes=req.ttl_minutes,
    )
    try:
        evt = AuditEvent(
            event_id=f"evt_{uuid.uuid4().hex[:12]}",
            event_type=AuditEventType.APPROVAL_REQUEST,
            timestamp=datetime.now(timezone.utc),
            tenant_id="default",
            user_id=req.user_id,
            agent_id=req.agent_id,
            resource=req.resource,
            action=req.action,
        )
        audit_ledger.append(evt)
    except Exception:
        pass
    return ar


@app.post("/v1/approval/decide")
def approval_decide(req: ApprovalDecideBody, payload: dict = Depends(verify_security_auth)):
    try:
        ar = approval_store.decide(
            approval_id=req.approval_id,
            decision=req.decision,
            decided_by=req.decided_by,
            group_id=req.group_id,
        )
        try:
            evt = AuditEvent(
                event_id=f"evt_{uuid.uuid4().hex[:12]}",
                event_type=AuditEventType.APPROVAL_DECISION,
                timestamp=datetime.now(timezone.utc),
                tenant_id="default",
                user_id=ar.user_id,
                agent_id=ar.agent_id,
                resource=ar.resource,
                action=ar.action,
                decision=req.decision.value,
            )
            audit_ledger.append(evt)
        except Exception:
            pass
        return ar
    except (KeyError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/v1/approval/{approval_id}")
def approval_get(approval_id: str, payload: dict = Depends(verify_security_auth)):
    ar = approval_store.get(approval_id)
    if ar is None:
        raise HTTPException(status_code=404, detail="not found")
    return ar


# ── Audit ──────────────────────────────────────────────────────
@app.post("/v1/audit/verify")
def audit_verify(req: AuditVerifyRequest | None = None, payload: dict = Depends(verify_security_auth)):
    """Hash-chain 검증 + optional checkpoint 검증."""
    chain_valid = audit_ledger.verify_chain()
    result: dict = {
        "chain_valid": chain_valid,
        "event_count": audit_ledger.count,
        "head": audit_ledger.head,
    }
    # checkpoint 검증이 요청된 경우
    if req and req.chain_head_hash and req.signature:
        from audit_model import AuditCheckpoint

        cp = AuditCheckpoint(
            chain_head_hash=req.chain_head_hash,
            event_count=audit_ledger.count,
            created_at=datetime.now(timezone.utc),
            signature=req.signature,
        )
        result["checkpoint_valid"] = audit_ledger.verify_checkpoint(cp)
    return result


@app.get("/v1/audit/checkpoint")
def audit_checkpoint(verify_external: bool = True, payload: dict = Depends(verify_security_auth)):
    """GET /v1/audit/checkpoint — current checkpoint + external anchor verification.
    Includes external_verified flag by reading OAOS_AUDIT_CHECKPOINT_S3 or local file /var/lib/oaos/audit-checkpoint.json.
    """
    cp = audit_ledger.checkpoint()
    # base payload
    try:
        base = cp.model_dump(mode="json") if hasattr(cp, "model_dump") else dict(cp)
    except Exception:
        base = {"chain_head_hash": getattr(cp, "chain_head_hash", ""), "event_count": getattr(cp, "event_count", 0), "created_at": str(getattr(cp, "created_at", "")), "signature": getattr(cp, "signature", "")}
    # external verification (best-effort, never fails 200)
    try:
        if verify_external and hasattr(audit_ledger, "verify_external_checkpoint"):
            ext_info = audit_ledger.verify_external_checkpoint()
            ext_cp = ext_info.get("external_checkpoint")
            base["external_verified"] = bool(ext_info.get("external_verified", False))
            base["external_exists"] = bool(ext_info.get("external_exists", False))
            base["external_path"] = ext_info.get("external_path", "")
            base["external_head_match"] = bool(ext_info.get("head_match", False)) if ext_info.get("external_exists") else False
            if ext_cp is not None:
                try:
                    base["external_checkpoint"] = ext_cp.model_dump(mode="json") if hasattr(ext_cp, "model_dump") else dict(ext_cp)
                except Exception:
                    base["external_checkpoint"] = {"chain_head_hash": getattr(ext_cp, "chain_head_hash", ""), "event_count": getattr(ext_cp, "event_count", 0), "signature": getattr(ext_cp, "signature", "")}
            else:
                base["external_checkpoint"] = None
        else:
            base["external_verified"] = False
            base["external_exists"] = False
            base["external_path"] = audit_ledger._external_checkpoint_path() if hasattr(audit_ledger, "_external_checkpoint_path") else ""
            base["external_checkpoint"] = None
            base["external_head_match"] = False
    except Exception as e:
        base["external_verified"] = False
        base["external_exists"] = False
        base["external_error"] = str(e)[:200]
        base["external_checkpoint"] = None
    return base


@app.get("/v1/audit/events")
def audit_events(payload: dict = Depends(verify_security_auth)):
    return {"events": [e.model_dump(mode="json") for e in audit_ledger.events]}
