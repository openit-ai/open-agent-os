# Personal Wiki 설계서 — 첨부파일 + Tool 결과 아카이빙

> **상태:** Implemented (partial) — Vault/Extractor 구현 완료 (Phase 1-2, `packages/personal-wiki`), Consolidate/Embed 진행 중 | **대상:** `openit-ai/open-agent-os` v0.1.1 (BSL 1.1)  
> **최종 갱신:** 2026-08-28 | **의존:** `docs/architecture-v1.6.md` §§5, 10, 16H, 27, 40 / `memory_service/app.py` / `security/models/orm.py` / `execution_gateway/`  
> **원칙:** Clean-room 설계 — Argo 코드 복사 없음. Hermes `ocr-and-documents` / `nano-pdf` / `docx` / `xlsx` / `pdf` 스킬 로직은 **참조**만 하고, 구현은 OAOS 파이썬 스택으로 재작성.

---

## 0. 요약 및 다이어그램

Personal Wiki는 **개인 에이전트에게 전달된 모든 첨부파일**과 **모든 Tool 실행 결과**를 자동으로 아카이빙·임베딩·검색 가능하게 만드는 개인 지식 저장소이다. 모든 쓰기는 Execution Gateway를 경유하며, pgvector 기반 `memory_service`로 검색한다.

```
┌──────────────┐  파일 업로드   ┌─────────────────────┐  추출/OCR   ┌──────────────────────────┐
│  Mattermost  │ ───────────► │  Execution Gateway   │ ─────────► │   Personal Vault (FS)    │
│  (또는 Slack)│  tool call   │  - 파일 수신/검증    │  journal   │ /var/lib/oaos/vault/     │
│              │ ◄─────────── │  - python 추출 파이프│  + embed   │  {tenant}/{agent}/       │
└──────────────┘  결과 반환   │  - tool 결과 캡처    │            │   journal/ notes/        │
                              │  - trace_id 부여     │            │   projects/ files/       │
                              └─────────┬───────────┘            │   attachments/           │
                                        │ vector embed            └────────────┬─────────────┘
                                        ▼                                      │
                              ┌─────────────────────┐                          │
                              │  memory_service     │ ◄────────────────────────┘
                              │  - pgvector(1536)   │  검색 (tenant/agent ACL)
                              │  - POST /v1/memories│
                              │  - POST /v1/memories/search │
                              └─────────────────────┘
```

---

## 1. Vault 레이아웃

### 1.1 루트 및 격리

```
VAULT_ROOT = /var/lib/oaos/vault
구조: ${VAULT_ROOT}/{tenant_id}/{agent_id}/
  ├── journal/        # 일자별 저널 (tool 결과·첨부 요약 자동 append)
  │   └── YYYY/MM/YYYY-MM-DD.md
  ├── notes/          # 검색/리포트/회의 통합 노트 (수동·자동 병합)
  │   ├── search/
  │   ├── reports/
  │   └── meetings/
  ├── projects/       # 프로젝트별 위키 (선택)
  │   └── {project_slug}/
  ├── files/          # 추출된 텍스트 캐시 (md 변환본)
  │   └── {YYYY}/{trace_id}__{sanitized_name}.md
  └── attachments/    # 원본 파일 보관 (바이너리)
      └── {YYYY}/{trace_id}__{sanitized_name}.{ext}
```

- `tenant_id` / `agent_id`는 Control Plane의 `tenant` / `agent` 식별자를 그대로 사용. **owner isolation**: 어떤 에이전트도 다른 `tenant/agent` 경로를 읽거나 쓸 수 없음 (Execution Gateway에서 경로 검증).
- `VAULT_ROOT`는 호스트 바인드 마운트 또는 PVC. 백업 시 전체 트리를 스냅샷하되, BSL 라이선스 고지 포함.

### 1.2 파일 네이밍 규칙

- `sanitized_name`: 원본 파일명에서 `[^a-zA-Z0-9._-]` → `_`, 길이 80자 제한, 충돌 시 `__{short_hash}` suffix.
- `trace_id`: Execution Gateway가 부여한 `trace_id` (UUIDv4, 8자 prefix로도 식별 가능).
- 저널 파일은 일자 단위 append-only. 동시 쓰기는 Gateway가 직렬화.

### 1.3 메타데이터

각 `files/*.md` 및 `attachments/*` 상단에 YAML frontmatter:

```yaml
---
trace_id: "a1b2c3d4-..."
tenant_id: "acme"
agent_id: "agent-42"
source: "mattermost:file_upload" # 또는 "tool:web_search" 등
original_name: "계약서.pdf"
mime: "application/pdf"
bytes: 1234567
extracted_at: "2026-08-28T07:00:00Z"
extractor: "pdfminer+ocr_fallback"
---
```

---

## 2. 첨부파일 추출 플로우

### 2.1 전체 흐름

```
Mattermost 파일 업로드
  → Control Plane이 Execution Gateway에 위임 (file bytes + metadata + trace_id)
  → Gateway: ① 바이러스/크기/타입 검증 → ② attachments/ 에 원본 저장
           → ③ 추출 파이프라인 실행 → ④ files/*.md 생성 + journal append + memory_service embed
  → 사용자에게 결과 요약 반환 (Mattermost 메시지 + 저널 링크)
```

### 2.2 추출 파이프라인 (Python, Clean-room)

**의존 라이브러리 (신규 추가, Hermes 스킬 로직 참조하되 직접 구현):**

| 타입 | 1차 추출 | 라이브러리 | 스킬 참조 |
|------|---------|-----------|----------|
| PDF (텍스트 레이어 있음) | 텍스트 레이어 추출 | `pdfminer.six` | `nano-pdf`, `pdf` |
| PDF (스캔/이미지) | OCR fallback | `pytesseract` / `easyocr` + `pdf2image` | `ocr-and-documents` |
| DOCX | 문단·표·헤더 추출 | `python-docx` | `docx` |
| XLSX | 시트별 셀 텍스트 | `openpyxl` | `xlsx` |
| PPTX | 슬라이드 텍스트 | `python-pptx` | `pdf` 스킬 내 pptx 처리 참조 |
| 이미지 (png/jpg/webp) | OCR | `easyocr` 또는 `pytesseract` | `ocr-and-documents` |
| 기타 | mime 기반 거부 + 원본만 보관 | — | — |

**파이프라인 단계 (Gateway 내부 `vault_extractor.py` — 신규 모듈):**

1. **MIME 판별** — `python-magic` 또는 확장자 기반. 허용 목록 외는 추출 스킵.
2. **1차 추출 시도** — 해당 파서로 텍스트 추출. 결과가 `MIN_TEXT_LEN`(예: 50자) 미만이면 실패로 간주.
3. **OCR fallback** — 텍스트가 없거나 부족하면 페이지를 이미지로 렌더링(`pdf2image`/`pymupdf`) 후 `easyocr` 또는 `tesseract`로 재추출.
4. **정규화** — 연속 공백/개행 정리, 페이지 구분자 `--- page N ---` 삽입, 표는 마크다운 테이블로 변환.
5. **Markdown 생성** — frontmatter + `# {original_name}` + 추출 텍스트. `files/{YYYY}/{trace_id}__{name}.md` 로 저장.
6. **저널 append** — `journal/YYYY/MM/YYYY-MM-DD.md` 에 `## {HH:MM} 첨부: {name} ({mime}, {bytes} bytes, trace {trace_id})` 섹션 추가 + 요약 3줄 + `files/...md` 링크.
7. **Vector embed** — 청킹(512 tokens, overlap 64) 후 `memory_service` `POST /v1/memories` 로 임베딩. `source_type=attachment`, `source_ref=trace_id` 로 추적.

### 2.3 실패 처리

- 추출 실패 시에도 원본은 `attachments/` 에 보존하고, 저널에는 `> ⚠️ 추출 실패: {reason} — 원본만 보관됨` 기록.
- OCR 실패는 재시도 1회 후 포기. 로그는 Gateway 구조화 로그로 남김.

---

## 3. Tool 결과 자동 아카이빙

### 3.1 원칙

> **Execution Gateway를 통과하는 모든 tool 호출 결과는 예외 없이 저널에 append 된다.** (No bypass)

이는 `docs/architecture-v1.6.md` §16H (Execution Gateway Tool Policy) 및 §40 (Audit) 의 Zero-Bypass 원칙을 따른다.

### 3.2 대상 Tool

- `web_search`, `web_extract`, `report_generate`, `meeting_transcribe`, `file_analyze`, `code_exec`, 및 향후 추가되는 모든 Gateway tool.

### 3.3 아카이빙 포맷

Gateway가 tool 실행 직후 수행 (tool 응답 반환 전):

```markdown
## {HH:MM} tool:{tool_name} trace:{trace_id}
- **호출자:** {agent_id} / {user_id}
- **입력 요약:** {truncated_input_json (200자)}
- **결과 요약:** {auto_summary (300자, LLM 없이 텍스트 앞부분 절삭)}
- **전체 결과:** [files/{YYYY}/{trace_id}__tool-{tool_name}.md](files/...)
- **state:** success | error ({error_code})
```

- 전체 결과는 `files/{YYYY}/{trace_id}__tool-{tool_name}.md` 에 원문 저장 (JSON pretty-print 또는 markdown).
- `trace_id`는 Gateway가 호출 시점에 생성하여 tool 요청/응답/저널을 연결하는 correlation ID.

### 3.4 멱등성 및 순서

- 동일 `trace_id` 중복 append 방지: 저널 append 전 `trace_id` 존재 여부 체크.
- 쓰기 순서는 Gateway가 보장 (단일 writer per agent/day).

---

## 4. 검색/리포트/회의 → 노트 통합

### 4.1 통합 규칙

| 소스 | 저널 (자동) | 노트 (통합) |
|------|------------|------------|
| `web_search` / `web_extract` | ✅ 항상 | 동일 주제 3회 이상 시 `notes/search/{topic}.md` 로 병합 (Gateway 배치 또는 에이전트 요청) |
| 리포트 생성 (`report_generate`) | ✅ 항상 | `notes/reports/{YYYY-MM-DD}-{title}.md` |
| 회의록/트랜스크립트 | ✅ 항상 | `notes/meetings/{YYYY-MM-DD}-{meeting_id}.md` |

### 4.2 병합(Consolidation) 전략

- **트리거:** (a) 에이전트가 `consolidate` tool 호출, (b) 일일 배치 잡이 `journal/YYYY/MM/DD.md` 를 스캔하여 동일 키워드 클러스터 탐지.
- **병합 로직:** 관련 `files/*.md`들을 읽어 중복 제거·시간순 정렬·헤더 정리 후 `notes/{category}/{slug}.md` 생성. 원본 `files/*.md`는 유지 (삭제 없음).
- **링크 유지:** 통합 노트 상단에 `Sources: trace_id ...` 목록, 저널에도 `→ notes/...` 역링크 추가.

---

## 5. 검색(Retrieval) — memory_service 연동

### 5.1 임베딩

- 모든 `files/*.md` 청크는 `memory_service` (`memory_service/app.py`) 로 전송.
  - `POST /v1/memories` — `tenant_id`, `agent_id`, `content`, `source_type`, `source_ref(trace_id)`, `embedding(Vector 1536)` 포함.
  - 임베딩 모델: `text-embedding-3-small` (1536차원) — `alembic/versions/002_persistent_memory.py` 및 `007_pgvector_upgrade.py` 의 `Vector(1536)` 스키마와 일치. SQLite 테스트에서는 `Text` fallback.
- 청킹 실패 시에도 원문 전체를 단일 메모리로 저장 (검색 가능성은 낮아지나 유실 방지).

### 5.2 검색

- `POST /v1/memories/search` — `tenant_id` / `agent_id` ACL 필터 + pgvector 거리 + substring LIKE fallback.
- Personal Wiki 검색은 **항상** `tenant_id` + `agent_id` 스코프로 제한 (owner isolation).
- 결과는 `source_ref`로 `files/*.md` 및 `attachments/*` 원본 경로를 역추적 가능.

### 5.3 조회 경로

```
사용자 질의 → Personal Agent → memory_service /v1/memories/search (ACL 필터)
           → 상위 K개 chunk + source_ref → files/*.md 렌더링 → 답변 생성
```

- 크로스-테넌트/크로스-에이전트 검색은 Gateway에서 거부 (403).

---

## 6. 권한 모델

### 6.1 Owner Isolation

- Vault 경로는 `{tenant}/{agent}` 로 물리 분리. Gateway는 요청 `agent_id`와 경로 `agent_id`가 일치하지 않으면 거부.
- `memory_service` 검색도 동일 ACL 강제 (`security/models/orm.py` `MemoryORM`의 `tenant_id`/`owner_agent_id` 컬럼 기반).

### 6.2 역할

| 역할 | 권한 |
|------|------|
| **Personal Agent (owner)** | 자신의 vault 전체 R/W, memory 검색/임베딩 |
| **Admin (tenant)** | 감사(audit) 로그 열람만 가능, vault 원문 열람 불가 (프라이버시) |
| **System (Gateway/Control Plane)** | 쓰기 전용 (append/embed), 읽기 불가 — 디버깅 시에도 owner 토큰 필요 |

### 6.3 감사(Audit)

- 모든 vault 쓰기(첨부 저장, 추출, tool 아카이빙, embed)는 `audit_log`에 `trace_id`, `tenant_id`, `agent_id`, `action`, `result` 기록 (`docs/architecture-v1.6.md` §40).
- 감사 로그는 `oaos` DB에 저장되며, vault 파일 자체와 분리.

### 6.4 라이선스

- 본 설계 및 구현은 **Business Source License 1.1**을 따른다. Vault 파일 헤더/저널 템플릿에 라이선스 고지 주석 포함.

---

## 7. Obsidian 마이그레이션

### 7.1 대상

기존 Obsidian vault (`*.md` + 첨부)를 Personal Wiki로 일괄 이전.

### 7.2 절차

1. **Export** — Obsidian vault 디렉터리를 tar/zip으로 수집 (`.obsidian/` 설정 제외).
2. **Import CLI** — `python -m tools.obsidian_import --tenant {t} --agent {a} --src /path/to/obsidian --vault-root /var/lib/oaos/vault`
   - 각 `*.md`를 `notes/imported/{relative_path}` 로 복사.
   - `![[attachment]]` 형태의 Obsidian 링크를 표준 마크다운 `![alt](attachments/...)` 로 변환.
   - 첨부 파일은 `attachments/imported/` 로 복사.
3. **재임베딩** — 모든 `notes/imported/**/*.md`를 청킹하여 `memory_service`에 재등록 (`source_type=obsidian_import`).
4. **검증** — `search`로 샘플 질의하여 recall 확인. 실패 시 재청킹.

### 7.3 비파괴 원칙

- 기존 Obsidian vault 원본은 변경하지 않음. Personal Wiki에만 복사본 생성.

---

## 8. 구현 단계

| Phase | 범위 | 산출물 | 의존 |
|-------|------|--------|------|
| **Phase 1 — Vault FS + 저널** | `vault/` 디렉터리 생성, `journal/YYYY/MM/DD.md` append 유틸, 권한 체크 | `execution_gateway/vault/store.py`, `execution_gateway/vault/journal.py` | §5 DB 격리 |
| **Phase 2 — 첨부 추출 파이프라인** | MIME 판별, `pdfminer`/`docx`/`openpyxl`/`pptx` 추출, 저장, 저널 append | `execution_gateway/vault/extractor.py` | Phase 1, `ocr-and-documents` 스킬 참조 |
| **Phase 3 — OCR Fallback** | `pdf2image` + `easyocr`/`tesseract` 연동, 실패 처리 | `execution_gateway/vault/ocr.py` | Phase 2 |
| **Phase 4 — Tool 결과 아카이빙** | Gateway tool 래퍼에 `trace_id` 부여 및 저널/파일 append 훅 | `execution_gateway/tools/wrapper.py` | Phase 1 |
| **Phase 5 — memory_service 연동** | 청킹 + `POST /v1/memories` embed, `POST /v1/memories/search` 검색 | `execution_gateway/vault/embed.py` | Phase 2-4, `memory_service/app.py` |
| **Phase 6 — 노트 통합** | `notes/search|reports|meetings` 병합 로직, 배치 잡 | `execution_gateway/vault/consolidate.py` | Phase 5 |
| **Phase 7 — Obsidian Import** | CLI import 도구, 링크 변환, 재임베딩 | `tools/obsidian_import.py` | Phase 5 |
| **Phase 8 — 감사/권한/테스트** | ACL 단위테스트, audit 로그, BSL 헤더, E2E 테스트 | `tests/test_vault_*.py` | 전체 |

### 8.1 테스트 전략

- **단위:** 추출 파이프라인 (샘플 pdf/docx/xlsx/pptx/이미지), 저널 append 멱등성, 경로 ACL.
- **통합:** Mattermost 파일 업로드 → 추출 → embed → search E2E (Docker Compose).
- **회귀:** 기존 `pytest` 스위트 통과 — 새 의존은 `optional`로 추가하여 기존 테스트 미파괴.

### 8.2 비목표 (Out of Scope)

- Argo 코드 복사 — 모든 추출 로직은 OAOS 파이썬 스택으로 clean-room 재작성.
- 공용 위키 — Personal Wiki는 개인 에이전트 전용. 공용 위키는 별도 설계.

---

*본 문서는 `docs/architecture-v1.6.md` §§5, 10, 16H, 27, 40 을 준수하며, BSL 1.1 라이선스 하에 제공된다.*
