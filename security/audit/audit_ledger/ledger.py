"""Hash-chain Audit Ledger — Section 31.
- hash(previous_hash + canonical_payload)
- checkpoint sign with HMAC (Section 31 주기적 서명)
- verify_chain (무결성 검증)
"""
from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import datetime, timezone
from typing import Optional

from audit_model import AuditCheckpoint, AuditEvent


class AuditLedger:
    """In-memory hash-chain ledger.

    - append: previous_hash 체이닝 + event_hash 계산
    - verify_chain: 전체 체인 무결성 검증
    - checkpoint: chain_head_hash 를 HMAC-SHA256 으로 서명하여 외부 보관
    - verify_checkpoint: checkpoint 서명 검증
    """

    def __init__(self, signing_key: str | None = None) -> None:
        self._head: str | None = None
        self._events: list[AuditEvent] = []
        self._signing_key = signing_key or "default-audit-signing-key"

    def append(self, event: AuditEvent) -> AuditEvent:
        event.previous_hash = self._head
        event.event_hash = event.compute_hash()
        self._head = event.event_hash
        self._events.append(event)
        return event

    @property
    def head(self) -> str | None:
        return self._head

    @property
    def events(self) -> list[AuditEvent]:
        return list(self._events)

    @property
    def count(self) -> int:
        return len(self._events)

    def verify_chain(self) -> bool:
        """전체 체인 무결성 검증 — 하나라도 변조되면 False."""
        prev: str | None = None
        for e in self._events:
            if e.previous_hash != prev:
                return False
            if e.compute_hash() != e.event_hash:
                return False
            prev = e.event_hash
        return True

    # ── Checkpoint (Section 31 — 주기적 외부 서명 보관) ─────────
    def checkpoint(self, signing_key: str | None = None) -> AuditCheckpoint:
        """현재 chain head 를 HMAC-SHA256 으로 서명한 checkpoint 생성."""
        key = signing_key or self._signing_key
        head = self._head or ""
        sig = hmac.new(key.encode(), head.encode(), hashlib.sha256).hexdigest()
        return AuditCheckpoint(
            chain_head_hash=head,
            event_count=len(self._events),
            created_at=datetime.now(timezone.utc),
            signature=sig,
        )

    def verify_checkpoint(
        self, checkpoint: AuditCheckpoint, signing_key: str | None = None
    ) -> bool:
        """checkpoint 서명 검증."""
        key = signing_key or self._signing_key
        expected = hmac.new(
            key.encode(), checkpoint.chain_head_hash.encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, checkpoint.signature):
            return False
        # head 일치 여부도 확인 (체인 진행 후 checkpoint head 가 현재 head 이하인지)
        # 엄격: checkpoint head 가 현재 체인에 존재해야 함
        # 간단 체크: checkpoint head 가 과거 head 중 하나거나 현재 head
        if checkpoint.event_count > len(self._events):
            return False
        # checkpoint 시점 head 가 현재 체인에 포함되는지 확인
        # event_count 만큼의 prefix head 와 비교
        if checkpoint.event_count == 0:
            return checkpoint.chain_head_hash == ""
        # prefix chain head 재계산 없이, 이벤트 count 기반으로 검증
        # 실제로는 event_count 번째 이벤트의 hash 가 checkpoint head 여야 함
        if checkpoint.event_count <= len(self._events):
            expected_head = self._events[checkpoint.event_count - 1].event_hash
            if checkpoint.chain_head_hash != expected_head and checkpoint.chain_head_hash != self._head:
                # checkpoint 가 최신 head 를 가리키는 경우도 허용
                # but if mismatch, fail
                if checkpoint.chain_head_hash not in [e.event_hash for e in self._events]:
                    return False
        return True

    def tamper_event(self, index: int, **kwargs) -> None:
        """테스트용 변조 — 절대 프로덕션에서 사용 금지."""
        if 0 <= index < len(self._events):
            for k, v in kwargs.items():
                setattr(self._events[index], k, v)
