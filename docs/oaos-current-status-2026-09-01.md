# OAOS Current Development Status

- Date: 2026-09-01 (KST)
- Product: Open Agent OS
- Architecture baseline: v1.7.2
- Repository branch: `release/v0.1.3-remediation`
- Status: **PARTIAL — P0 implementation verified; P1 live infrastructure and P2 production work remain**

## P0 — Implemented and locally verified

- Adaptive Profile Evidence Worker now serializes persistence operations per verified `(tenant_id, user_id)` while allowing different owners to proceed concurrently.
- Knowledge Index persistent sync now preserves source ACL metadata in the chunk boundary, writes tenant-scoped entries, propagates explicit source deletion IDs to the persistent repository, and fails closed for missing production credentials/providers. The persistent checkpoint store is still an explicit follow-up boundary; live source/embedding/database verification remains unclaimed.
- Microsoft Graph connector now performs real `httpx` transport for planned Graph operations when an owner token is supplied and fails closed on missing production tokens. Live Microsoft tenant/OAuth verification remains unclaimed.
- Personal credential leakage placeholders were replaced with deterministic test coverage for Vault owner isolation, cross-user Gmail access, delegation revoke cascade, explicit export denial, and prompt-injection resistance.
- Control Plane and Execution Gateway environment gates now import the single canonical `agent_runtime.env_gate` implementation.
- Knowledge Index source and package mirror files remain identical.

### P0 residuals — not complete

- Persistent sync checkpoint storage is still `InMemoryCheckpointStore`; it must be replaced or backed by a durable tenant/source checkpoint before P0 can be called complete.
- Production admin persistence still contains `Base.metadata.create_all()` compatibility paths; Alembic-only runtime schema enforcement and production read-back remain pending.
- Live external connector, embedding provider, ACL corpus, and multi-user OAuth verification remain pending.

### P0 evidence

- Focused P0/Profile tests: `57 passed, 7 warnings`.
- Credential and existing security regression selection: `9 passed`.
- Full-suite evidence from the current candidate run: `1276 passed, 5 skipped, 0 failed, 88 warnings in 361.17s (0:06:01)`.
- `py_compile`: passed for changed Python files.
- `git diff --check`: passed.

## P1 — Local checks passed; live verification blocked

- Static and unit verification: `99 passed, 2 skipped`.
- Redis TCP protocol probe: `PONG`.
- Docker access is unavailable to the current operator: `permission denied while trying to connect to /var/run/docker.sock`.
- No usable kind cluster/API server is available; CNI/Hubble enforcement evidence is unavailable.
- Therefore kind two-replica, k6 concurrency, Redis multi-replica, and Cilium/Calico NetworkPolicy flow evidence remain **BLOCKED**.
- Live Outline/Notion/Mattermost/Slack/LLM Gateway corpus and external round-trip evidence are not claimed without provider read-back.

## P2 — Not complete

- **Secret Vault policy:** OAOS will use the existing `encrypted_postgres` backend as the default Secret Vault for the current deployment. HashiCorp Vault/AWS Secrets Manager are optional future enterprise hardening, not a required P2 completion item.
- `/readyz` currently reports external Secret Vault health as skipped when no external backend is configured. This is not evidence that the configured `encrypted_postgres` Secret Vault is absent; the readiness wording/health check should be aligned with the selected backend in a follow-up.
- `env_gate` still has mirrored implementations; single-source consolidation remains pending.
- Profile UI and complete operational Profile E2E remain pending.
- Production readiness policy and live distributed/external evidence remain pending.

## Latest Mattermost E2E read-back (2026-09-01 13:47 KST)

- Source post: `4r4i5mapu7ykzmtick3pefugyw`
- Source author: Mattermost user `c4m5yxidpinxtewrzefq7x19rr` (`mykim`)
- Source create time: `1788238030515`
- Bot reply: `fhw16au5tpy95xii67pyr9jnwo`
- Bot author: `bmhbteup4p8bmb8rfh151y6w1e`
- Reply root: `4r4i5mapu7ykzmtick3pefugyw`
- Reply create time: `1788238044151`
- Observed result: `oaos-mm-bridge` received the user message, Control Plane created `sess_20e`, and posted one bot reply to the same Mattermost thread. The source→reply elapsed time was approximately 13.6 seconds based on Mattermost `create_at` values.
- This verifies the observed Mattermost user round trip after the credential/configuration fix. It does not by itself prove cross-user Google Calendar/Gmail external verification.

## Control Plane 오류 메시지 계약 — 적용 완료

- Control Plane 오류를 HTTP 상태와 안전한 사유로 분류해 Mattermost 사용자 메시지를 차등화한다.
- `400/401/403/404/405/409/422` 영구 오류는 동일 Post를 `seen`에 기록하고 안내 1회 후 종료하며 LLM을 호출하지 않는다.
- `403`은 등록 필요와 권한 거부를 구분한다. 내부 traceback·credential·경로는 사용자에게 노출하지 않는다.
- `408/429/5xx`는 bounded retry 대상이며 무한 재시도하지 않는다.
- 구현 커밋: `8b2d744463494f43b191d41af6f2b2219083fc0a`; 회귀 검증: `91 passed, 1 warning`.
- 상세 계약: `docs/architecture-v1.7.2.md` §16.11 및 `docs/oaos-user-registration-guide-v1.0.md` §6.

## Release and deployment boundaries

- This document records repository evidence only. It does not claim production deployment, distributed PASS, or external PASS.
- The current deployment uses `encrypted_postgres` as the accepted/default Secret Vault; external Secret Vault migration is optional future hardening, not a release blocker.
- The final commit hash and deployment read-back are recorded in Git history and the deployment verification document.
- No production database migration, tag, or GitHub Release is included in this status update.
