"""JIT Approval Workflow — Section 12, 23-24.
- approval_id / request_hash / nonce / signature
- 4 decisions: DENIED / APPROVED_ONCE / APPROVED_USER_ALWAYS / APPROVED_GROUP_ALWAYS
- expiry check + signature + nonce + hash 검증
"""
from __future__ import annotations

import hashlib
import hmac
import uuid
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel


class ApprovalDecision(str, Enum):
    PENDING = "PENDING"
    APPROVED_ONCE = "APPROVED_ONCE"
    APPROVED_USER_ALWAYS = "APPROVED_USER_ALWAYS"
    APPROVED_GROUP_ALWAYS = "APPROVED_GROUP_ALWAYS"
    DENIED = "DENIED"


class ApprovalRequest(BaseModel):
    approval_id: str
    user_id: str
    agent_id: str
    resource: str
    action: str
    risk: str
    request_hash: str
    nonce: str
    expires_at: datetime
    signature: str | None = None
    # 결정 관련
    decision: ApprovalDecision = ApprovalDecision.PENDING
    decided_at: datetime | None = None
    decided_by: str | None = None


class ApprovalStore:
    """In-memory approval lifecycle 관리."""

    def __init__(self, signing_key: str) -> None:
        self.signing_key = signing_key
        self._requests: dict[str, ApprovalRequest] = {}
        self._seen_nonces: set[str] = set()
        # persistent grants (user-always / group-always)
        self._user_grants: set[tuple[str, str, str]] = set()  # (user_id, action, resource_pattern)
        self._group_grants: set[tuple[str, str, str]] = set()  # (group_id, action, resource_pattern)

    # ── 생성 ───────────────────────────────────────────────────
    def create(
        self,
        user_id: str,
        agent_id: str,
        action: str,
        resource: str,
        risk: str = "HIGH",
        ttl_minutes: int = 60,
    ) -> ApprovalRequest:
        nonce = uuid.uuid4().hex
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
        raw = f"{user_id}|{agent_id}|{action}|{resource}|{nonce}|{expires_at.isoformat()}"
        request_hash = hashlib.sha256(raw.encode()).hexdigest()
        sig = hmac.new(self.signing_key.encode(), request_hash.encode(), hashlib.sha256).hexdigest()
        req = ApprovalRequest(
            approval_id=f"apr_{uuid.uuid4().hex[:12]}",
            user_id=user_id,
            agent_id=agent_id,
            resource=resource,
            action=action,
            risk=risk,
            request_hash=request_hash,
            nonce=nonce,
            expires_at=expires_at,
            signature=sig,
        )
        self._requests[req.approval_id] = req
        return req

    def get(self, approval_id: str) -> ApprovalRequest | None:
        return self._requests.get(approval_id)

    # ── 검증 ───────────────────────────────────────────────────
    def verify(self, req: ApprovalRequest) -> bool:
        """signature + nonce + expiry + hash 검증."""
        # expiry
        if req.expires_at < datetime.now(timezone.utc):
            return False
        # nonce replay
        # (생성 시에는 아직 seen 에 없으므로 검증 시에만 체크)
        # signature
        expected_sig = hmac.new(
            self.signing_key.encode(), req.request_hash.encode(), hashlib.sha256
        ).hexdigest()
        if req.signature != expected_sig:
            return False
        # request_hash 재계산 검증
        raw = f"{req.user_id}|{req.agent_id}|{req.action}|{req.resource}|{req.nonce}|{req.expires_at.isoformat()}"
        expected_hash = hashlib.sha256(raw.encode()).hexdigest()
        if req.request_hash != expected_hash:
            return False
        return True

    def is_expired(self, approval_id: str) -> bool:
        req = self._requests.get(approval_id)
        if req is None:
            return True
        return req.expires_at < datetime.now(timezone.utc)

    # ── 결정 (4 decisions) ─────────────────────────────────────
    def decide(
        self,
        approval_id: str,
        decision: ApprovalDecision,
        decided_by: str,
        group_id: str | None = None,
    ) -> ApprovalRequest:
        req = self._requests.get(approval_id)
        if req is None:
            raise KeyError(f"approval not found: {approval_id}")
        if req.expires_at < datetime.now(timezone.utc):
            raise ValueError("approval expired")
        if req.decision != ApprovalDecision.PENDING:
            raise ValueError(f"already decided: {req.decision}")
        # nonce replay 방지: 결정 시 nonce 를 seen 에 기록
        if req.nonce in self._seen_nonces:
            raise ValueError("nonce replay detected")
        self._seen_nonces.add(req.nonce)

        req.decision = decision
        req.decided_at = datetime.now(timezone.utc)
        req.decided_by = decided_by

        # persistent grant 기록
        if decision == ApprovalDecision.APPROVED_USER_ALWAYS:
            self._user_grants.add((req.user_id, req.action, req.resource))
        elif decision == ApprovalDecision.APPROVED_GROUP_ALWAYS:
            if not group_id:
                raise ValueError("group_id required for group-always")
            self._group_grants.add((group_id, req.action, req.resource))

        return req

    def is_approved(self, approval_id: str) -> bool:
        req = self._requests.get(approval_id)
        if req is None:
            return False
        return req.decision in (
            ApprovalDecision.APPROVED_ONCE,
            ApprovalDecision.APPROVED_USER_ALWAYS,
            ApprovalDecision.APPROVED_GROUP_ALWAYS,
        )

    def has_user_grant(self, user_id: str, action: str, resource: str) -> bool:
        import fnmatch

        for (u, a, pattern) in self._user_grants:
            if u == user_id and a == action and fnmatch.fnmatch(resource, pattern):
                return True
        return False

    def has_group_grant(self, group_id: str, action: str, resource: str) -> bool:
        import fnmatch

        for (g, a, pattern) in self._group_grants:
            if g == group_id and a == action and fnmatch.fnmatch(resource, pattern):
                return True
        return False


# ── 모듈 레벨 헬퍼 (기존 import 호환) ───────────────────────────


def create_approval_request(
    signing_key: str,
    user_id: str,
    agent_id: str,
    action: str,
    resource: str,
    risk: str = "HIGH",
    ttl_minutes: int = 60,
) -> ApprovalRequest:
    """Stateless helper — ApprovalStore 없이 단건 생성."""
    nonce = uuid.uuid4().hex
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
    raw = f"{user_id}|{agent_id}|{action}|{resource}|{nonce}|{expires_at.isoformat()}"
    request_hash = hashlib.sha256(raw.encode()).hexdigest()
    sig = hmac.new(signing_key.encode(), request_hash.encode(), hashlib.sha256).hexdigest()
    return ApprovalRequest(
        approval_id=f"apr_{uuid.uuid4().hex[:12]}",
        user_id=user_id,
        agent_id=agent_id,
        resource=resource,
        action=action,
        risk=risk,
        request_hash=request_hash,
        nonce=nonce,
        expires_at=expires_at,
        signature=sig,
    )


def verify_approval_request(signing_key: str, req: ApprovalRequest) -> bool:
    """Stateless 검증 helper."""
    if req.expires_at < datetime.now(timezone.utc):
        return False
    expected_sig = hmac.new(
        signing_key.encode(), req.request_hash.encode(), hashlib.sha256
    ).hexdigest()
    if req.signature != expected_sig:
        return False
    raw = f"{req.user_id}|{req.agent_id}|{req.action}|{req.resource}|{req.nonce}|{req.expires_at.isoformat()}"
    expected_hash = hashlib.sha256(raw.encode()).hexdigest()
    return req.request_hash == expected_hash
