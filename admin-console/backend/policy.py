"""Admin policy configuration — Draft -> validation/simulation -> L4 approval -> Publish -> rollback.

Minimal production-safe feature, isolated to OAOS admin console.
- Auth: all routes require JWT; mutations require L5 (require_l5), reads allow L4/L5.
- Persistent storage: oaos DB when OAOS_DATABASE_URL/DATABASE_URL is set; safe in-memory fallback only in non-prod (OAOS_ENV != production). Mirrors admin-console/backend/auth.py & persistence.py conventions.
- Immutable version history: every publish creates a new immutable row with incremented version; rollback is not a delete — it creates a new published version copying the target historical rules.
- Validation: valid enums (source/effect), nonempty action/resource_pattern, explicit deny protections, default deny (implicit), prevent removal of mandatory security rules unless L5 explicit opt-in (allow_remove_mandatory).
- Audit: appends to AuditLedger if available; in production, DB-less mutation fails closed (RuntimeError).
- Active published bundle is what GET /v1/policy/bundles returns/uses (DB published overrides default_bundle when present).

Endpoints (router prefix /v1/policy):
  GET    /bundles           — published + draft + approved (back-compat, returns bundles + evaluation_order)
  GET    /draft             — current draft if any
  GET    /history           — immutable version history (published + draft + approved)
  POST   /draft             — create/update draft (L5)
  PUT    /draft             — synonym for POST /draft (L5, for client flexibility)
  POST   /validate          — validate draft rules (auth, no mutation)
  POST   /simulate          — dry-run evaluation over draft or published (auth)
  POST   /approve           — approve draft (L5)  — draft -> approved
  POST   /publish           — publish approved (or draft if already approved) (L5)
  POST   /rollback          — rollback to historical version as new published version (L5)
"""
from __future__ import annotations

import os
import sys
import fnmatch
import uuid
import json
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

try:
    from .auth import AdminUser, get_current_admin, require_l5, AdminRole  # type: ignore
except ImportError:
    from auth import AdminUser, get_current_admin, require_l5, AdminRole  # type: ignore

# ── constants ────────────────────────────────────────────────────────

VALID_SOURCES = {"explicit_deny", "security_boundary_deny", "personal_delegation", "persistent_user_grant", "group_grant", "default_bundle", "jit_approval", "default_deny"}
VALID_EFFECTS = {"ALLOW", "DENY", "APPROVAL_REQUIRED"}
# Mandatory explicit-deny rules that must not be removed unless L5 explicitly opts in
MANDATORY_RULE_IDS = {"deny-external-export", "deny-external-share", "deny-external-send"}
# For validation convenience, also consider base mandatory id from legacy default bundle
MANDATORY_RULE_IDS_MIN = {"deny-external-export"}
DEFAULT_TENANT = "default"
DEFAULT_BUNDLE_ID = "default-bundle-v1"
DEFAULT_BUNDLE_NAME = "Default Policy Bundle"

POLICY_EVAL_ORDER_FALLBACK = ["explicit_deny","security_boundary_deny","personal_delegation","persistent_user_grant","group_grant","default_bundle","jit_approval","default_deny"]

# ── DB helpers ───────────────────────────────────────────────────────

def _is_prod() -> bool:
    return os.environ.get("OAOS_ENV","").strip().lower() in ("production","prod")

def _db_enabled() -> bool:
    try:
        try:
            from persistence import get_database_url  # type: ignore
        except ImportError:
            from .persistence import get_database_url  # type: ignore
        url = get_database_url()
        return bool(url and url.strip())
    except Exception:
        url = os.environ.get("OAOS_DATABASE_URL") or os.environ.get("DATABASE_URL")
        return bool(url and url.strip())

def _db_sync_url() -> Optional[str]:
    try:
        try:
            from persistence import get_database_url  # type: ignore
        except ImportError:
            from .persistence import get_database_url  # type: ignore
        url = get_database_url()
        # also consider control-plane var for systemd
        if not url:
            url = os.environ.get("OAOS_CP_DATABASE_URL")
    except Exception:
        url = os.environ.get("OAOS_DATABASE_URL") or os.environ.get("OAOS_CP_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url or not url.strip():
        # fallback: read repo .env for systemd missing EnvironmentFile
        try:
            _repo_env = Path(__file__).resolve().parents[2] / ".env"
            if _repo_env.exists():
                for _line in _repo_env.read_text().splitlines():
                    _line=_line.strip()
                    if not _line or _line.startswith("#") or "=" not in _line:
                        continue
                    _k,_sep,_v=_line.partition("=")
                    if _k.strip() in ("OAOS_DATABASE_URL","DATABASE_URL","OAOS_CP_DATABASE_URL"):
                        _cand=_v.strip().strip('"').strip("'")
                        if _cand:
                            url=_cand
                            break
        except Exception:
            pass
        if not url or not url.strip():
            return None
    u = url.strip()
    if u.startswith("postgresql+asyncpg://"):
        u = u.replace("postgresql+asyncpg://","postgresql+psycopg://",1)
    elif "+asyncpg" in u:
        u = u.replace("+asyncpg","+psycopg")
    elif u.startswith("postgresql://"):
        u = u.replace("postgresql://","postgresql+psycopg://",1)
    if "+aiosqlite" in u:
        u = u.replace("+aiosqlite","")
        u = u.replace("sqlite+://","sqlite://")
    if u.startswith("sqlite+"):
        u = u.replace("sqlite+","sqlite",1)
    if u.startswith("sqlite+aiosqlite://"):
        u = u.replace("sqlite+aiosqlite://","sqlite://",1)
    return u

def _ensure_policy_tables_sync(engine) -> None:
    try:
        from sqlalchemy import text  # type: ignore
    except Exception:
        return
    is_sqlite = str(getattr(engine,"url","")).startswith("sqlite")
    if is_sqlite:
        ddls = [
            """CREATE TABLE IF NOT EXISTS admin_policy_versions (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                bundle_id TEXT NOT NULL,
                name TEXT NOT NULL,
                version TEXT NOT NULL,
                status TEXT NOT NULL,
                rules_json TEXT NOT NULL,
                created_by TEXT,
                created_at TEXT NOT NULL,
                approved_by TEXT,
                approved_at TEXT,
                published_at TEXT,
                parent_version TEXT
            )""",
            "CREATE INDEX IF NOT EXISTS ix_policy_tenant_status ON admin_policy_versions (tenant_id, status)",
            "CREATE INDEX IF NOT EXISTS ix_policy_bundle ON admin_policy_versions (bundle_id, version)",
        ]
    else:
        ddls = [
            """CREATE TABLE IF NOT EXISTS admin_policy_versions (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                bundle_id TEXT NOT NULL,
                name TEXT NOT NULL,
                version TEXT NOT NULL,
                status TEXT NOT NULL,
                rules_json TEXT NOT NULL,
                created_by TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                approved_by TEXT,
                approved_at TIMESTAMPTZ,
                published_at TIMESTAMPTZ,
                parent_version TEXT
            )""",
            "CREATE INDEX IF NOT EXISTS ix_policy_tenant_status ON admin_policy_versions (tenant_id, status)",
            "CREATE INDEX IF NOT EXISTS ix_policy_bundle ON admin_policy_versions (bundle_id, version)",
        ]
    try:
        with engine.begin() as conn:
            for ddl in ddls:
                conn.execute(text(ddl))
    except Exception as e:
        logger.debug(f"policy ensure tables failed: {e}")

def _db_get_sync_engine():
    url = _db_sync_url()
    if not url:
        return None
    try:
        from sqlalchemy import create_engine  # type: ignore
    except Exception:
        return None
    try:
        connect_args = {}
        if url.startswith("sqlite"):
            connect_args = {"check_same_thread": False}
        eng = create_engine(url, echo=False, pool_pre_ping=False, connect_args=connect_args)
        _ensure_policy_tables_sync(eng)
        return eng
    except Exception as e:
        logger.debug(f"policy db engine failed: {e}")
        return None

# ── in-memory fallback (non-prod only) ───────────────────────────────
# Stored as list of dicts mirroring DB rows, process-local only.
_mem_versions: list[dict] = []  # immutable rows
_mem_draft: Optional[dict] = None  # at most one draft per tenant (simplified)

def _store_is_db() -> bool:
    if _is_prod():
        return _db_enabled()
    return _db_enabled()

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

# ── validation ───────────────────────────────────────────────────────

def _validate_rules(rules: list[dict], allow_remove_mandatory: bool = False) -> tuple[bool, list[str]]:
    errors: list[str] = []
    if not isinstance(rules, list) or len(rules) == 0:
        errors.append("rules must be a non-empty list (default deny requires at least one explicit rule)")
        return False, errors
    seen_ids: set[str] = set()
    for idx, r in enumerate(rules):
        rid = r.get("id")
        if not rid or not str(rid).strip():
            errors.append(f"rules[{idx}].id is required")
            continue
        if rid in seen_ids:
            errors.append(f"duplicate rule id: {rid}")
        seen_ids.add(rid)
        src = r.get("source")
        if src not in VALID_SOURCES:
            errors.append(f"rules[{idx}].source must be one of {sorted(VALID_SOURCES)} (got {src})")
        eff = r.get("effect")
        if eff not in VALID_EFFECTS:
            errors.append(f"rules[{idx}].effect must be one of {sorted(VALID_EFFECTS)} (got {eff})")
        act = r.get("action")
        if not act or not str(act).strip():
            errors.append(f"rules[{idx}].action must be non-empty")
        pat = r.get("resource_pattern")
        if not pat or not str(pat).strip():
            errors.append(f"rules[{idx}].resource_pattern must be non-empty")
        # priority if present must be int
        if "priority" in r and r["priority"] is not None:
            try:
                int(r["priority"])
            except Exception:
                errors.append(f"rules[{idx}].priority must be integer")
    # explicit deny protections: at least one explicit_deny that denies external
    has_explicit_deny = any((r.get("source")=="explicit_deny" and r.get("effect")=="DENY") for r in rules)
    if not has_explicit_deny:
        errors.append("at least one explicit_deny DENY rule is required (default deny + explicit deny protection)")
    # default deny is implicit — warn if no catch-all DENY? not error; engine has implicit default deny
    if not allow_remove_mandatory:
        # mandatory rule ids must be present (at least the minimal one)
        for mid in MANDATORY_RULE_IDS_MIN:
            if mid not in seen_ids:
                errors.append(f"mandatory security rule '{mid}' is missing — set allow_remove_mandatory=true as L5 to bypass")
        # also warn if none of mandatory set present
        if not (MANDATORY_RULE_IDS & seen_ids):
            # if minimal already reported, skip duplicate
            if "deny-external-export" in seen_ids:
                pass
            elif "mandatory security rule 'deny-external-export'" not in " ".join(errors):
                errors.append("mandatory security rule 'deny-external-export' is missing — set allow_remove_mandatory=true as L5 to bypass")
    return (len(errors)==0), errors

def _audit_append(action: str, admin: AdminUser, detail: dict, version: str | None = None) -> None:
    """Best-effort audit ledger append; fail-closed in production if DB required."""
    try:
        # Reuse security audit ledger pattern: try import security.app audit_ledger
        ledger = None
        try:
            import security.app as sec  # type: ignore
            ledger = getattr(sec, "audit_ledger", None)
            if ledger is None:
                # try create via AuditLedger
                from security.audit.audit_ledger.ledger import AuditLedger  # type: ignore
                ledger = AuditLedger()
        except Exception:
            try:
                from security.audit.audit_ledger.ledger import AuditLedger  # type: ignore
                ledger = AuditLedger()
            except Exception:
                ledger = None
        if ledger is None:
            if _is_prod():
                raise RuntimeError("AuditLedger unavailable in production — fail-closed for policy mutation")
            return
        from audit_model.model import AuditEvent, AuditEventType  # type: ignore
        evt = AuditEvent(
            event_id=f"evt_{uuid.uuid4().hex[:12]}",
            event_type=AuditEventType.POLICY_DECISION if hasattr(AuditEventType,"POLICY_DECISION") else AuditEventType.USER_MESSAGE,
            timestamp=datetime.now(timezone.utc),
            tenant_id=detail.get("tenant_id") or "default",
            user_id=admin.email,
            agent_id=admin.id,
            resource=detail.get("resource") or action,
            action=action,
            decision=detail.get("decision") or version or action,
            policy_version=version,
        )
        # attach result_hash as short json snippet if available
        try:
            ledger.append(evt)
        except Exception as e:
            if _is_prod():
                raise
            logger.debug(f"audit append failed non-prod ignored: {e}")
    except RuntimeError:
        raise
    except Exception as e:
        if _is_prod():
            raise RuntimeError(f"Audit append failed in production: {e}") from e
        logger.debug(f"audit append skipped: {e}")

def _next_version(current: str | None) -> str:
    if not current:
        return "1.0.0"
    parts = current.split(".")
    try:
        major, minor, patch = [int(x) for x in (parts + ["0","0","0"])[:3]]
        patch += 1
        return f"{major}.{minor}.{patch}"
    except Exception:
        return "1.0.1"

# ── storage ops ──────────────────────────────────────────────────────

def _db_list_versions(tenant_id: str = "default") -> list[dict]:
    if not _store_is_db():
        # mem fallback: filter by tenant
        return [v for v in _mem_versions if v.get("tenant_id")==tenant_id]
    eng = _db_get_sync_engine()
    if eng is None:
        if _is_prod():
            raise RuntimeError("DB required in production for policy history")
        return [v for v in _mem_versions if v.get("tenant_id")==tenant_id]
    try:
        from sqlalchemy import text  # type: ignore
        with eng.connect() as conn:
            rows = conn.execute(text("SELECT id, tenant_id, bundle_id, name, version, status, rules_json, created_by, created_at, approved_by, approved_at, published_at, parent_version FROM admin_policy_versions WHERE tenant_id=:t ORDER BY created_at ASC, version ASC"), {"t": tenant_id}).fetchall()
            out: list[dict] = []
            for r in rows:
                try:
                    rules = json.loads(r[6]) if isinstance(r[6], str) else r[6]
                except Exception:
                    rules = []
                out.append({
                    "id": r[0], "tenant_id": r[1], "bundle_id": r[2], "name": r[3], "version": r[4], "status": r[5],
                    "rules": rules, "rules_json": r[6], "created_by": r[7], "created_at": str(r[8]), "approved_by": r[9], "approved_at": str(r[10]) if r[10] else None, "published_at": str(r[11]) if r[11] else None, "parent_version": r[12]
                })
            return out
    finally:
        try: eng.dispose()
        except: pass

def _db_get_active_published(tenant_id: str = "default") -> Optional[dict]:
    versions = _db_list_versions(tenant_id)
    published = [v for v in versions if v.get("status")=="published"]
    if not published:
        return None
    # latest by published_at or version
    def _key(v):
        return (v.get("published_at") or v.get("created_at") or "", v.get("version",""))
    published.sort(key=_key)
    return published[-1]

def _db_get_draft(tenant_id: str = "default") -> Optional[dict]:
    if not _store_is_db():
        if _mem_draft and _mem_draft.get("tenant_id")==tenant_id:
            return _mem_draft
        return None
    eng = _db_get_sync_engine()
    if eng is None:
        if _is_prod():
            raise RuntimeError("DB required in production")
        if _mem_draft and _mem_draft.get("tenant_id")==tenant_id:
            return _mem_draft
        return None
    try:
        from sqlalchemy import text  # type: ignore
        with eng.connect() as conn:
            row = conn.execute(text("SELECT id, tenant_id, bundle_id, name, version, status, rules_json, created_by, created_at, approved_by, approved_at, published_at, parent_version FROM admin_policy_versions WHERE tenant_id=:t AND status IN ('draft','approved') ORDER BY created_at DESC LIMIT 1"), {"t": tenant_id}).mappings().first()
            if row is None:
                return None
            rules = json.loads(row["rules_json"]) if isinstance(row["rules_json"], str) else row["rules_json"]
            d = dict(row)
            d["rules"] = rules
            d["created_at"] = str(d["created_at"])
            if d.get("approved_at"): d["approved_at"] = str(d["approved_at"])
            if d.get("published_at"): d["published_at"] = str(d["published_at"])
            return d
    finally:
        try: eng.dispose()
        except: pass

def _db_save_draft(tenant_id: str, bundle_id: str, name: str, rules: list[dict], created_by: str, version: str | None = None) -> dict:
    # draft is mutable until approved/published; but we treat as upsert of single draft per tenant
    existing = _db_get_draft(tenant_id)
    draft_version = version or (existing.get("version") if existing and existing.get("status")=="draft" else None) or "draft"
    payload_rules_json = json.dumps(rules, ensure_ascii=False)
    now = _now_iso()
    new_id = existing.get("id") if existing and existing.get("status")=="draft" else f"pv_{uuid.uuid4().hex[:10]}"
    record = {
        "id": new_id, "tenant_id": tenant_id, "bundle_id": bundle_id, "name": name, "version": draft_version,
        "status": "draft", "rules": rules, "rules_json": payload_rules_json, "created_by": created_by, "created_at": now,
        "approved_by": None, "approved_at": None, "published_at": None, "parent_version": existing.get("parent_version") if existing else None
    }
    if not _store_is_db():
        global _mem_draft
        # replace draft
        _mem_draft = record
        return record
    eng = _db_get_sync_engine()
    if eng is None:
        if _is_prod():
            raise RuntimeError("DB required in production for draft")
        _mem_draft = record
        return record
    try:
        from sqlalchemy import text  # type: ignore
        with eng.begin() as conn:
            # upsert: delete previous draft if exists (immutable history only for published/approved; draft is mutable)
            if existing and existing.get("status")=="draft":
                conn.execute(text("DELETE FROM admin_policy_versions WHERE id=:id"), {"id": existing["id"]})
            conn.execute(text("INSERT INTO admin_policy_versions (id, tenant_id, bundle_id, name, version, status, rules_json, created_by, created_at, approved_by, approved_at, published_at, parent_version) VALUES (:id,:tenant_id,:bundle_id,:name,:version,:status,:rules_json,:created_by,:created_at,:approved_by,:approved_at,:published_at,:parent_version)"),
                         {"id": new_id, "tenant_id": tenant_id, "bundle_id": bundle_id, "name": name, "version": draft_version, "status": "draft", "rules_json": payload_rules_json, "created_by": created_by, "created_at": now, "approved_by": None, "approved_at": None, "published_at": None, "parent_version": record.get("parent_version")})
        return record
    finally:
        try: eng.dispose()
        except: pass

def _db_mark_approved(tenant_id: str, approved_by: str) -> Optional[dict]:
    draft = _db_get_draft(tenant_id)
    if draft is None or draft.get("status")!="draft":
        return None
    now = _now_iso()
    if not _store_is_db():
        draft["status"] = "approved"
        draft["approved_by"] = approved_by
        draft["approved_at"] = now
        global _mem_draft
        _mem_draft = draft
        return draft
    eng = _db_get_sync_engine()
    if eng is None:
        if _is_prod():
            raise RuntimeError("DB required")
        draft["status"] = "approved"
        draft["approved_by"] = approved_by
        draft["approved_at"] = now
        _mem_draft = draft
        return draft
    try:
        from sqlalchemy import text  # type: ignore
        with eng.begin() as conn:
            conn.execute(text("UPDATE admin_policy_versions SET status='approved', approved_by=:ab, approved_at=:at WHERE id=:id"), {"ab": approved_by, "at": now, "id": draft["id"]})
        draft["status"]="approved"; draft["approved_by"]=approved_by; draft["approved_at"]=now
        return draft
    finally:
        try: eng.dispose()
        except: pass

def _db_publish(tenant_id: str, published_by: str) -> dict:
    draft = _db_get_draft(tenant_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="No draft/approved version to publish")
    if draft.get("status") not in ("draft","approved"):
        raise HTTPException(status_code=400, detail=f"Cannot publish status={draft.get('status')}")
    # validation must pass
    ok, errs = _validate_rules(draft.get("rules") or [], allow_remove_mandatory=False)
    # if mandatory missing and L5 explicit? publish already validated draft strictly — but allow if draft was saved with allow_remove_mandatory
    # We store that flag implicitly? Instead re-validate but allow if draft has explicit bypass note? Simpl simpler: re-check but if fails due to mandatory, require L5 already checked at draft time. So fail if missing now.
    if not ok and not draft.get("allow_remove_mandatory"):
        # check if error is only mandatory — still fail
        raise HTTPException(status_code=400, detail="Validation failed: " + "; ".join(errs))
    # compute next version
    active = _db_get_active_published(tenant_id)
    next_ver = _next_version(active.get("version") if active else None)
    now = _now_iso()
    new_id = f"pv_{uuid.uuid4().hex[:10]}"
    published_record = {
        "id": new_id, "tenant_id": tenant_id, "bundle_id": draft.get("bundle_id") or DEFAULT_BUNDLE_ID, "name": draft.get("name") or DEFAULT_BUNDLE_NAME,
        "version": next_ver, "status": "published", "rules": draft.get("rules") or [], "rules_json": json.dumps(draft.get("rules") or [], ensure_ascii=False),
        "created_by": published_by, "created_at": now, "approved_by": draft.get("approved_by"), "approved_at": draft.get("approved_at"), "published_at": now, "parent_version": draft.get("version")
    }
    if not _store_is_db():
        _mem_versions.append(published_record)
        # clear draft after publish
        global _mem_draft
        _mem_draft = None
        # also keep published in versions for history
        return published_record
    eng = _db_get_sync_engine()
    if eng is None:
        if _is_prod():
            raise RuntimeError("DB required for publish")
        _mem_versions.append(published_record); _mem_draft=None; return published_record
    try:
        from sqlalchemy import text  # type: ignore
        with eng.begin() as conn:
            conn.execute(text("INSERT INTO admin_policy_versions (id, tenant_id, bundle_id, name, version, status, rules_json, created_by, created_at, approved_by, approved_at, published_at, parent_version) VALUES (:id,:tenant_id,:bundle_id,:name,:version,:status,:rules_json,:created_by,:created_at,:approved_by,:approved_at,:published_at,:parent_version)"),
                         {"id": new_id, "tenant_id": tenant_id, "bundle_id": published_record["bundle_id"], "name": published_record["name"], "version": next_ver, "status": "published", "rules_json": published_record["rules_json"], "created_by": published_by, "created_at": now, "approved_by": published_record["approved_by"], "approved_at": published_record["approved_at"], "published_at": now, "parent_version": published_record["parent_version"]})
            # remove the draft/approved row (it becomes history as published copy; draft cleared)
            conn.execute(text("DELETE FROM admin_policy_versions WHERE id=:id"), {"id": draft["id"]})
        return published_record
    finally:
        try: eng.dispose()
        except: pass

def _db_rollback(target_version: str, tenant_id: str, actor: str, allow_remove_mandatory: bool = False) -> dict:
    # find target historical published version
    versions = _db_list_versions(tenant_id)
    target = next((v for v in versions if v.get("version")==target_version and v.get("status")=="published"), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"Published version {target_version} not found")
    ok, errs = _validate_rules(target.get("rules") or [], allow_remove_mandatory=allow_remove_mandatory)
    if not ok:
        raise HTTPException(status_code=400, detail="Rollback target validation failed: " + "; ".join(errs))
    active = _db_get_active_published(tenant_id)
    next_ver = _next_version(active.get("version") if active else None)
    now = _now_iso()
    new_id = f"pv_{uuid.uuid4().hex[:10]}"
    record = {
        "id": new_id, "tenant_id": tenant_id, "bundle_id": target.get("bundle_id") or DEFAULT_BUNDLE_ID, "name": target.get("name") or DEFAULT_BUNDLE_NAME,
        "version": next_ver, "status": "published", "rules": target.get("rules") or [], "rules_json": json.dumps(target.get("rules") or [], ensure_ascii=False),
        "created_by": actor, "created_at": now, "approved_by": None, "approved_at": None, "published_at": now, "parent_version": target_version
    }
    if not _store_is_db():
        _mem_versions.append(record)
        return record
    eng = _db_get_sync_engine()
    if eng is None:
        if _is_prod():
            raise RuntimeError("DB required for rollback")
        _mem_versions.append(record); return record
    try:
        from sqlalchemy import text  # type: ignore
        with eng.begin() as conn:
            conn.execute(text("INSERT INTO admin_policy_versions (id, tenant_id, bundle_id, name, version, status, rules_json, created_by, created_at, approved_by, approved_at, published_at, parent_version) VALUES (:id,:tenant_id,:bundle_id,:name,:version,:status,:rules_json,:created_by,:created_at,:approved_by,:approved_at,:published_at,:parent_version)"),
                         {"id": new_id, "tenant_id": tenant_id, "bundle_id": record["bundle_id"], "name": record["name"], "version": next_ver, "status": "published", "rules_json": record["rules_json"], "created_by": actor, "created_at": now, "approved_by": None, "approved_at": None, "published_at": now, "parent_version": target_version})
        return record
    finally:
        try: eng.dispose()
        except: pass

# ── simulation ───────────────────────────────────────────────────────
def _evaluate_rules(rules: list[dict], action: str, resource: str) -> dict:
    # minimal deterministic evaluation mirroring PolicyEngine order
    order = POLICY_EVAL_ORDER_FALLBACK
    by_source: dict[str,list[dict]] = {s:[] for s in order}
    for r in rules:
        s = r.get("source")
        if s in by_source:
            by_source[s].append(r)
        else:
            # unknown source -> ignore (defense)
            pass
    for src in order:
        if src == "default_deny":
            continue
        sorted_rules = sorted(by_source.get(src,[]), key=lambda x: (int(x.get("priority") or 0), x.get("id") or ""))
        for rule in sorted_rules:
            ra = rule.get("action")
            if ra != "*" and ra != action:
                continue
            pat = rule.get("resource_pattern") or ""
            if not fnmatch.fnmatch(resource, pat):
                continue
            return {"decision": rule.get("effect"), "matched_rule": rule, "source": src, "reason": f"matched {rule.get('id')} @ {src}"}
    return {"decision":"DENY","matched_rule":None,"source":"default_deny","reason":"no rule matched — default deny"}

# ── router ───────────────────────────────────────────────────────────

router = APIRouter(prefix="/v1/policy", tags=["policy"])

class DraftRequest(BaseModel):
    tenant_id: str = DEFAULT_TENANT
    bundle_id: str = DEFAULT_BUNDLE_ID
    name: str = DEFAULT_BUNDLE_NAME
    version: str | None = None
    rules: list[dict] = Field(default_factory=list)
    allow_remove_mandatory: bool = False
    description: str | None = None

class ValidateRequest(BaseModel):
    rules: list[dict]
    allow_remove_mandatory: bool = False

class SimulateRequest(BaseModel):
    action: str
    resource: str
    use_draft: bool = True
    tenant_id: str = DEFAULT_TENANT
    rules: list[dict] | None = None

class ApproveRequest(BaseModel):
    tenant_id: str = DEFAULT_TENANT

class PublishRequest(BaseModel):
    tenant_id: str = DEFAULT_TENANT
    allow_remove_mandatory: bool = False

class RollbackRequest(BaseModel):
    target_version: str
    tenant_id: str = DEFAULT_TENANT
    allow_remove_mandatory: bool = False

def _bundle_from_record(rec: dict | None) -> dict | None:
    if rec is None:
        return None
    return {
        "id": rec.get("bundle_id") or DEFAULT_BUNDLE_ID,
        "tenant_id": rec.get("tenant_id") or DEFAULT_TENANT,
        "name": rec.get("name") or DEFAULT_BUNDLE_NAME,
        "version": rec.get("version") or "draft",
        "rules": rec.get("rules") or [],
        "status": rec.get("status"),
        "created_by": rec.get("created_by"),
        "created_at": rec.get("created_at"),
        "approved_by": rec.get("approved_by"),
        "approved_at": rec.get("approved_at"),
        "published_at": rec.get("published_at"),
        "parent_version": rec.get("parent_version"),
        "id_row": rec.get("id"),
    }

# GET /bundles is served by app.py's policy_bundles (delegates to this module's DB). No duplicate router entry.
# Kept as alias for direct policy-router consumers: use /history or /draft + app's /bundles
@router.get("/bundles-alias")
def policy_bundles_alias(auth: AdminUser = Depends(get_current_admin)):
    active = _db_get_active_published(DEFAULT_TENANT)
    draft = _db_get_draft(DEFAULT_TENANT)
    return {"active": _bundle_from_record(active) if active else None, "draft": _bundle_from_record(draft) if draft else None}

@router.get("/draft")
def get_draft(auth: AdminUser = Depends(get_current_admin)):
    draft = _db_get_draft(DEFAULT_TENANT)
    if draft is None:
        return {"draft": None}
    return {"draft": _bundle_from_record(draft)}

@router.get("/history")
def get_history(auth: AdminUser = Depends(get_current_admin)):
    versions = _db_list_versions(DEFAULT_TENANT)
    # include active published and draft
    items = [_bundle_from_record(v) for v in versions]
    draft = _db_get_draft(DEFAULT_TENANT)
    # _db_list includes draft in DB case; in mem case versions excluded draft, so add
    if not _store_is_db() and draft is not None:
        # avoid duplicate if already in items
        if not any(i and i.get("id_row")==draft.get("id") for i in items):
            items.append(_bundle_from_record(draft))
    # sort: published by published_at, draft last
    return {"items": items, "count": len(items), "active_version": (_db_get_active_published(DEFAULT_TENANT) or {}).get("version")}

@router.post("/draft")
@router.put("/draft")
def upsert_draft(body: DraftRequest, admin: AdminUser = Depends(require_l5)):
    ok, errs = _validate_rules(body.rules, allow_remove_mandatory=body.allow_remove_mandatory)
    if not ok:
        raise HTTPException(status_code=400, detail="Validation failed: " + "; ".join(errs))
    if _is_prod() and not _db_enabled():
        raise HTTPException(status_code=500, detail="DB required in production for policy draft (fail-closed)")
    rec = _db_save_draft(body.tenant_id, body.bundle_id, body.name, body.rules, admin.email, version=body.version)
    # attach flag for publish bypass
    if body.allow_remove_mandatory:
        rec["allow_remove_mandatory"] = True  # type: ignore
    _audit_append("policy.draft", admin, {"tenant_id": body.tenant_id, "resource": body.bundle_id, "decision": rec.get("version")}, version=rec.get("version"))
    return {"draft": _bundle_from_record(rec), "validation": {"ok": True}}

@router.post("/validate")
def validate_rules_ep(body: ValidateRequest, auth: AdminUser = Depends(get_current_admin)):
    ok, errs = _validate_rules(body.rules, allow_remove_mandatory=body.allow_remove_mandatory)
    return {"ok": ok, "errors": errs, "valid": ok}

@router.post("/simulate")
def simulate(body: SimulateRequest, auth: AdminUser = Depends(get_current_admin)):
    if not body.action or not body.action.strip():
        raise HTTPException(status_code=400, detail="action is required")
    if not body.resource or not body.resource.strip():
        raise HTTPException(status_code=400, detail="resource is required")
    rules: list[dict] | None = body.rules
    if rules is None:
        if body.use_draft:
            d = _db_get_draft(body.tenant_id)
            if d is not None and d.get("rules"):
                rules = d.get("rules")
            else:
                active = _db_get_active_published(body.tenant_id)
                if active is not None:
                    rules = active.get("rules") or []
                else:
                    # fallback to default bundle rules
                    try:
                        from policy_engine.default_bundle import default_bundle as _db  # type: ignore
                        b = _db(tenant_id=body.tenant_id)
                        rules = [r.model_dump(mode="json") if hasattr(r, "model_dump") else dict(r) for r in b.rules]
                    except Exception:
                        rules = []
        else:
            active = _db_get_active_published(body.tenant_id)
            if active is not None:
                rules = active.get("rules") or []
            else:
                try:
                    from policy_engine.default_bundle import default_bundle as _db  # type: ignore
                    b = _db(tenant_id=body.tenant_id)
                    rules = [r.model_dump(mode="json") if hasattr(r, "model_dump") else dict(r) for r in b.rules]
                except Exception:
                    rules = []
    result = _evaluate_rules(rules or [], body.action, body.resource)
    return {"request": {"action": body.action, "resource": body.resource, "tenant_id": body.tenant_id, "use_draft": body.use_draft}, "result": result, "evaluated_rules": len(rules or [])}

@router.post("/approve")
def approve(body: ApproveRequest, admin: AdminUser = Depends(require_l5)):
    if _is_prod() and not _db_enabled():
        raise HTTPException(status_code=500, detail="DB required in production (fail-closed)")
    draft = _db_get_draft(body.tenant_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="No draft to approve")
    ok, errs = _validate_rules(draft.get("rules") or [], allow_remove_mandatory=bool(draft.get("allow_remove_mandatory")))
    if not ok:
        raise HTTPException(status_code=400, detail="Validation failed: " + "; ".join(errs))
    rec = _db_mark_approved(body.tenant_id, admin.email)
    if rec is None:
        raise HTTPException(status_code=400, detail="Draft not in approvable state")
    _audit_append("policy.approve", admin, {"tenant_id": body.tenant_id, "decision": "approve", "resource": draft.get("bundle_id")}, version=draft.get("version"))
    return {"draft": _bundle_from_record(rec), "status": "approved"}

@router.post("/publish")
def publish(body: PublishRequest, admin: AdminUser = Depends(require_l5)):
    if _is_prod() and not _db_enabled():
        raise HTTPException(status_code=500, detail="DB required in production (fail-closed)")
    rec = _db_publish(body.tenant_id, admin.email)
    _audit_append("policy.publish", admin, {"tenant_id": body.tenant_id, "decision": "publish", "resource": rec.get("bundle_id")}, version=rec.get("version"))
    return {"published": _bundle_from_record(rec), "active_version": rec.get("version")}

@router.post("/rollback")
def rollback(body: RollbackRequest, admin: AdminUser = Depends(require_l5)):
    if _is_prod() and not _db_enabled():
        raise HTTPException(status_code=500, detail="DB required in production (fail-closed)")
    rec = _db_rollback(body.target_version, body.tenant_id, admin.email, allow_remove_mandatory=body.allow_remove_mandatory)
    _audit_append("policy.rollback", admin, {"tenant_id": body.tenant_id, "decision": "rollback", "resource": body.target_version}, version=rec.get("version"))
    return {"published": _bundle_from_record(rec), "rolled_back_from": body.target_version, "active_version": rec.get("version")}

# Helpers for app.py to get active published without circular import

def get_active_published_bundle(tenant_id: str = "default") -> Optional[dict]:
    try:
        return _db_get_active_published(tenant_id)
    except Exception:
        return None

def get_draft_bundle(tenant_id: str = "default") -> Optional[dict]:
    try:
        return _db_get_draft(tenant_id)
    except Exception:
        return None
