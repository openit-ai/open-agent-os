# OAOS 웹관리자(Admin Console) UX 재설계 v1.0

- 문서 상태: 구현 전 상위 설계안
- 기준일: 2026-09-15
- 대상: `admin-console/` (Next.js + shadcn/ui 계열 컴포넌트, 운영 도메인 `admin.oaos.cloud`)
- 계승 문서: `docs/admin-ia-control-plane-redefinition.md`의 6개 아키텍처 그룹과 additive alias 원칙

## 기준

- CWD: `/home/mykim/.paseo/worktrees/1ili70rg/feat-admin-ux-redesign`
- 브랜치: `feat/admin-ux-redesign`
- HEAD: `920723f Merge pull request #8 from openit-ai/release/v0.1.6`
- 기동 확인 결과 CWD는 지정된 Paseo worktree의 `feat-admin-ux-redesign` 경로와 일치한다. 이 문서는 해당 기준점의 정적 분석 결과만 사용한다.

## 1. 개요

### 목적과 성공 기준

이 설계의 목적은 기능을 더 노출하는 것이 아니라, 처음 접한 관리자가 **무엇을 먼저 해야 하는지**, **현재 연결이 실제로 동작하는지**, **목록에서 어떤 동작을 어디서 해야 하는지**를 별도 운영 지식 없이 판단할 수 있게 하는 것이다. 다음 세 기준을 모든 화면의 수용 조건으로 삼는다.

1. 초기 사용자가 안내된 순서대로 설정하고, 중단하더라도 완료 지점부터 재개할 수 있다.
2. 필수 연결의 누락·장애·저장 후 미반영 상태를 한눈에 알고, 일반 경로에서는 IP·Port·API 키를 직접 입력하지 않고 연결 및 테스트할 수 있다.
3. 이력·조회·수정·삭제·목록이 동일한 상태 모델, 컴포넌트, 피드백 및 접근성 규칙을 따른다.

제품 수준 성공 지표는 첫 로그인부터 필수 설정 완료까지의 완료율, 필수 연결 실패를 인지하고 조치 링크를 여는 비율, 저장 후 실제 반영 여부를 잘못 판단한 세션 수, 목록 작업의 오류·취소율로 측정한다. 구현 Phase마다 이벤트 이름과 개인정보 비수집 원칙을 별도로 확정하며, 이 문서 자체는 분석 도구나 운영 추적을 추가하지 않는다.

### 범위

- `admin-console/app`, `components`, `lib`, `lib/i18n`의 IA, 온보딩, 상태 표현, 공통 UX 킷과 데이터 조회 상태 모델을 설계한다.
- 자동 탐지·준비 상태·연결 테스트·적용 상태를 지원하는 데 필요한 `admin-console/backend`의 **추가형 HTTP 계약**을 설계한다.
- 기존 ACP/MCP/Policy/Knowledge 경계를 유지한 채, 동일 기능으로 들어가는 메뉴와 URL을 정리한다.

### 비범위

- Control Plane(CP), Execution Gateway(EG), agent runtime의 라우팅·실행·정책 판단·MCP 호출·Knowledge 처리 내부 로직은 변경하지 않는다.
- 배포, 서비스 재시작, systemd/docker 조작, DB 스키마 또는 운영 데이터 변경은 이 설계 작업의 범위가 아니다.
- 외부 서비스의 인증 체계를 우회하거나 비밀값을 브라우저에 노출하는 자동 연결은 하지 않는다. 자동 탐지로 해결되지 않는 외부 인증은 제공자 OAuth/승인 흐름 또는 접힌 고급 설정으로 처리한다.

## 2. 실측 진단

### 측정 방식과 결과

모든 수치는 위 기준 HEAD의 파일을 `find`, `wc`, `rg`로 다시 측정했다. `node_modules`와 빌드 산출물은 Git ignore 규칙 및 명시한 소스 경로로 제외했다. “화면”처럼 해석이 달라질 수 있는 항목은 집계 범위를 함께 쓴다.

| 항목 | 재측정 결과 | 근거/해석 |
|---|---:|---|
| 대시보드 `page.tsx` | 24개, 5,387줄 | `find 'admin-console/app/(dashboard)' -type f -name 'page.tsx'`; 그 결과에 `wc -l` |
| 전체 앱 `page.tsx` | 25개, 5,446줄 | 위 24개에 `app/login/page.tsx` 59줄 포함. 브리프의 5,446줄은 이 범위에서 재현됨 |
| 백엔드 HTTP 라우트 데코레이터 | 143개 | `rg -n '^@(app|router)\.(get|post|put|patch|delete)\(' admin-console/backend -g '*.py'`의 줄 수 |
| 백엔드 Python 파일 | 29개 | `find admin-console/backend -maxdepth 1 -type f -name '*.py'` |
| 사이드바 | 23개 평면 항목 | `layout.tsx`의 `navItems` 객체 항목 직접 계수. 데스크톱/모바일 모두 같은 평면 배열을 반복 렌더링 |
| UI 원시 컴포넌트 | 7개 | `components/ui/{badge,button,card,input,label,table,tabs}.tsx` |
| 공통 상태/복합 컴포넌트 | 0개 | Dialog/Toast/Skeleton/EmptyState/ErrorState/ConfirmDialog/DataTable/FormField/StepWizard 파일 없음 |
| 파괴 동작 확인 | 전역 `confirm()` 11회 | `rg -n '\bconfirm\s*\(' admin-console -g '*.tsx' -g '*.ts'`; 브리프의 “`window.confirm` 11곳”은 호출 수는 같지만 실제 표기는 모두 bare global 호출 |
| 경고 팝업 | `alert()` 9회, 3개 파일 | providers 2회, users 5회, infra 2회. `rg -n '\balert\s*\(' admin-console/app -g '*.tsx'` |
| Dialog 사용 | 0회 | `@/components/ui/dialog` import 또는 `<Dialog>` JSX 검색 결과 0 |
| 목록 검색 UI | 0개 화면 | 대시보드 TSX에서 `search` 식별자/라벨 검색 결과 0. 브리프의 6개는 현재 소스에서 재현되지 않음 |
| 페이지네이션 | UI 0개, 메타데이터 1개 파일 | `lib/api.ts`에 `page/page_size/total` 정규화는 있으나 대시보드의 페이지 이동 컨트롤은 없음 |
| 정렬 | 대시보드 3개 파일, 전체 app 5개 파일 | audit/policy/runtime-config에 정렬. 나머지 2개는 중복된 version API route로 목록 UX가 아님 |
| 상태 필터 | 1개 화면 | `llm-usage/page.tsx`의 성공/오류 필터. 검색과 동일시하지 않음 |
| 빈 상태 분기 | 14개 화면 모듈 | 대시보드 TSX에서 `length === 0` 계열의 명시 분기가 있는 파일. 화면 모듈 33개(24 route + 9 panel/section, layout 제외) 중 14개이며 공통 EmptyState 사용은 0 |
| `/infra` | 691줄, 11개 탭 | services/live/unified/setup/mcp/mm/ol/notion/slack/oauth/smtp의 `TabsTrigger` 직접 계수 |
| 별칭/껍데기 화면 | 4개 | `/control/acp` 13줄, `/control/runtime` 14줄, `/execution/mcp` 13줄, `/setup` 12줄 |
| `useEffect` 사용 | UI 소스 32개 파일, 38회 | `admin-console/app`+`components`의 TS/TSX 기준. `lib/i18n/index.tsx`의 provider 구현까지 포함하면 33개 파일 |
| i18n | ko/en 각 949줄 | `wc -l admin-console/lib/i18n/{ko,en}.json` |

위 표는 “검색 6개, 빈 상태 9개”라는 이전 관찰을 현재 소스에 억지로 맞추지 않는다. 재현 가능한 현재값을 기준선으로 사용하며, 특히 페이지네이션 메타데이터와 실제 조작 UI, 문자열 검색과 상태 필터를 분리했다. `lib/api.ts`에 인증을 포함한 `apiFetch`는 있으나 조회 캐시, 중복 요청 제거, mutation 후 invalidation, 표준 loading/error lifecycle은 없어 각 화면이 `useEffect`와 로컬 상태로 이를 다시 구현한다.

핵심 집계는 다음 명령과 출력으로 재현한다. `dashboard_pages` 변수에는 `find`가 반환한 24개 파일만 들어가며, 전체 앱 합계는 별도 명령으로 로그인 화면까지 포함한다.

```bash
dashboard_pages=$(find 'admin-console/app/(dashboard)' -type f -name 'page.tsx' | sort)
printf '%s\n' "$dashboard_pages" | wc -l                   # 24
wc -l $dashboard_pages | tail -1                            # 5387 total
find admin-console/app -type f -name 'page.tsx' -print0 | xargs -0 wc -l | tail -1  # 5446 total

rg -n '^@(app|router)\.(get|post|put|patch|delete)\(' admin-console/backend -g '*.py' | wc -l  # 143
sed -n '/const navItems = \[/,/^  \];/p' 'admin-console/app/(dashboard)/layout.tsx' | rg -c 'href:'  # 23
find admin-console/components/ui -maxdepth 1 -type f | wc -l  # 7
rg -o '(^|[^.[:alnum:]_])confirm\s*\(' admin-console/app -g '*.tsx' | wc -l  # 11
rg -o '(^|[^[:alnum:]_])alert\s*\(' admin-console/app -g '*.tsx' | wc -l    # 9
rg -l '\buseEffect\b' admin-console/app admin-console/components -g '*.tsx' -g '*.ts' | wc -l  # 32
wc -l admin-console/lib/i18n/ko.json admin-console/lib/i18n/en.json  # 949, 949
```

### 왜 초기 사용자가 막히는가

**온보딩 흐름.** 첫 설정 URL인 `/setup`은 12줄짜리 client redirect로 `/infra`를 열고, 사용자는 11개 탭 중 `setup`의 위치와 의미를 스스로 찾아야 한다. 현재 SetupTab과 backend는 DB/Redis/Hermes의 환경변수·기본값 탐지와 3개 검사를 일부 제공하지만, 화면에는 URL 고급 입력이 전면 노출되고 완료 표시는 2단 구성에 머문다. ACP·실행 경로·인그레스·정책을 포함한 실제 사용 가능 상태를 단계적으로 안내하거나 판정하지 못한다.

**연결 흐름.** MCP는 `/infra`의 다섯 번째 탭인 동시에 `/execution/mcp` 별칭 화면이고, ACP는 Providers 내부 섹션과 `/control/acp` 양쪽에 있다. 개별 연결 화면은 URL·토큰·헤더 등을 직접 입력하게 하고, ACP/MCP 및 6개 connector에 테스트 endpoint가 있어도 응답·timeout·오류 표현은 서로 다르다. 백엔드에는 ACP/Mattermost/Outline/Notion/Slack/SMTP 저장 응답이 `applied=false`와 재시작 필요 note를 반환하는 경우가 있지만, “저장됨”과 “실제 적용됨”이 독립된 상태라는 설명과 후속 행동이 일관되지 않다.

**목록 흐름.** 23개 메뉴가 사용자 과업이 아니라 구현 단위로 평면 나열되고, 목록은 검색 0, 페이지 이동 0, 화면 정렬 3개에 머문다. API가 페이지 메타데이터를 일부 정규화해도 화면이 사용하지 않으며, 선택·대량 동작·행 메뉴·상세 진입·URL에 보존되는 조회 조건의 공통 규칙이 없다. 데이터가 많아지면 원하는 행을 찾을 수 없고, 적을 때도 빈 상태에서 다음 행동을 알기 어렵다.

**오류와 파괴 동작 흐름.** 로딩 문구, 인라인 오류, 성공 문구, `alert()`, `confirm()`이 혼재한다. 팝업은 맥락·대상·영향·진행 상태를 표현하기 어렵고 키보드 포커스 복귀나 비동기 실패 복구도 표준화하지 못한다. 같은 장애가 대시보드, 연결 카드, 상세 화면에서 서로 다른 용어와 색으로 보일 가능성이 크다.

## 3. 정보구조(IA) 재설계

### 원칙과 목표 구조

기존 IA 문서의 아키텍처 경계를 그대로 계승하되, 사용자가 보는 순서는 “시작 → 연결 → 설정 → 실행 → 지식 → 모니터링 → 관리” 과업 순서로 만든다. 사이드바 라벨은 마스터 확정(2026-09-15)에 따라 아키텍처 용어가 아니라 **초보자 친화 과업 용어**를 쓴다(제어 평면 → 설정, 운영 → 모니터링). 아키텍처 용어와의 대응은 §11 표에 기록한다. `/` 대시보드와 `/setup` 시작하기는 그룹 바깥의 고정 진입점이며, 아래 6개 그룹은 접을 수 있는 사이드바 섹션이다. 모바일에서도 단순 평면 복제가 아니라 동일 그룹과 현재 위치를 유지한다.

| 그룹 | 목표 화면 | 한 줄 책임 |
|---|---|---|
| 고정 | 대시보드 `/` | 필수 연결, 미완료 설정, 승인·장애·최근 변경을 우선순위 순으로 요약한다. |
| 고정 | 시작하기 `/setup` | 자동 점검 기반 온보딩을 실행·재개하고 완료 조건을 설명한다. |
| 연결 | 연결 개요 `/connections` | 모든 채널 연결의 상태, 발견 후보, 미적용 변경을 한 목록으로 보여준다. |
| 연결 | Mattermost `/connections/mattermost` | Mattermost 봇과 브리지의 인증·연결·적용 상태를 관리한다. |
| 연결 | Slack `/connections/slack` | Slack 연결과 전달 테스트를 관리한다. |
| 연결 | Notion `/connections/notion` | Notion 인그레스 인증과 접근 검사를 관리한다. |
| 연결 | OAuth `/connections/oauth` | 외부 인증 제공자 활성 상태와 승인 흐름을 관리한다. |
| 연결 | SMTP `/connections/smtp` | 알림 메일 전송 연결과 비발송 연결 검사를 관리한다. |
| 설정 | 제어 개요 `/control` | CP 준비 상태와 정책·승인·감사의 조치 필요 항목을 모은다. |
| 설정 | ACP `/control/acp` | Hermes ACP 어댑터 설정·연결·적용 상태를 독립 화면에서 관리한다. |
| 설정 | 런타임 구성 `/control/runtime` | 스냅샷 생성·발행·적용 확인·롤백 이력을 관리한다. |
| 설정 | 정책 `/control/policy` | 정책 draft 검증·승인·발행·rollback을 관리한다. |
| 설정 | 승인 `/control/approvals` | 대기 승인 조회와 허용·거절 결정을 처리한다. |
| 설정 | 감사 `/control/audit` | 감사 이벤트와 체인 무결성·checkpoint를 조회한다. |
| 실행 | 실행 개요 `/execution` | 모델 실행 경로, EG, MCP, quota의 가용성을 요약한다. |
| 실행 | MCP `/execution/mcp` | MCP 서버 등록·수정·삭제·도구 발견 테스트를 관리한다. |
| 실행 | 모델 제공자 `/execution/providers` | LLM 제공자와 모델, credential reference, 연결 테스트를 관리한다. |
| 실행 | Fallback `/execution/fallback` | 제공자 fallback 순서와 활성화를 관리한다. |
| 실행 | 사용량 `/execution/usage` | 호출·비용·latency·오류 이력을 검색·필터·페이지 조회한다. |
| 실행 | Quota `/execution/quota` | tenant별 한도와 현재 사용량을 조회·수정한다. |
| 지식 | 지식 개요 `/knowledge` | 지식 소스, 인덱스, 메모리 준비 상태를 요약한다. |
| 지식 | Outline `/knowledge/outline` | Outline 연결과 collection 접근을 관리한다. |
| 지식 | Embedding `/knowledge/embedding` | 임베딩 제공자·모델·차원 구성을 관리한다. |
| 지식 | 동기화 `/knowledge/operations` | connector별 동기화·dry-run 결과를 관리한다. |
| 모니터링 | 상태 `/operations/health` | services/live/unified 관측을 하나의 상태 모델로 제공한다. |
| 모니터링 | 인프라 등록 `/operations/services` | 관리 대상 서비스 레지스트리와 probe를 관리한다. |
| 모니터링 | 백업 `/operations/backup` | 백업 상태·생성·보존 이력을 관리한다. |
| 모니터링 | 보안 업데이트 `/operations/security-updates` | 업데이트 및 CVE 상태와 조치 안내를 제공한다. |
| 모니터링 | 라이선스 `/operations/license` | 라이선스 검증과 만료·기능 상태를 제공한다. |
| 관리 | 사용자 `/management/users` | 관리자 계정과 사용자 매핑 CRUD를 관리한다. |
| 관리 | 자격증명 `/management/credentials` | 발급·폐기·만료 credential 현황과 이력을 조회한다. |
| 관리 | 비밀값 `/management/secrets` | 비밀값 설정 여부와 rotation 절차만 노출한다. |
| 관리 | 기능 플래그 `/management/feature-flags` | 플래그 상태·변경·감사 링크를 관리한다. |
| 관리 | 프로필 작업 `/management/profile-operations` | 사용자 프로필 backfill/reset 운영 작업을 수행한다. |

### 묻힘과 중복 해소

- ACP는 Providers 내부 탭에서 제거하고 `/control/acp`를 단일 canonical 화면으로 삼는다. 모델 제공자 화면에는 “ACP 실행 경로 보기” 링크와 요약 상태만 둔다.
- MCP는 `/infra` 탭에서 제거하고 이미 존재하는 `/execution/mcp`를 canonical로 삼는다. Infra에서 같은 React panel을 재수출하지 않는다.
- `/control/runtime`는 `/runtime-config`로 client redirect하는 14줄 별칭을 끝내고 실제 canonical 화면을 소유한다. `/runtime-config`가 새 URL로 redirect한다.
- `/infra` 11개 탭은 setup→`/setup`, mcp→`/execution/mcp`, mm/slack/notion/oauth/smtp→`/connections/*`, ol→`/knowledge/outline`, services/live/unified→`/operations/services|health`로 분해한다.
- 공통 기능은 route page가 아니라 feature component에서 소유한다. 과도기 alias가 필요해도 두 화면에서 별도 state/fetch를 만들지 않고 같은 feature entry를 사용한다.

### URL 호환 규칙

기존 URL은 **관찰 기간 동안 보존**한다(마스터 확정 2026-09-15). 새 URL이 안정화된 뒤 단순 경로는 서버 측 permanent redirect(308), 상태를 포함한 링크는 temporary redirect(307) 또는 compatibility resolver로 전환하며 query string은 그대로 전달한다. URL fragment는 서버에 전달되지 않으므로 `/infra#mcp` 같은 북마크는 `/infra`의 얇은 client compatibility resolver가 fragment를 읽어 목표 URL로 `replace`한다.

**정리 정책(마스터 확정)**: 관찰 기간 후 미사용 URL은 정리한다. 정리 조건은 추측이 아니라 계측으로 판정한다.

| 단계 | 기간/조건 | 동작 |
|---|---|---|
| 진입 | Phase 6 배포 직후 | 모든 legacy URL에 307(임시) redirect + 접근 계측 시작 |
| 관찰 | 최소 1회 릴리스 주기 이상, 그리고 마지막 접근 후 30일 경과 | `/var/log/nginx` 접근 로그에서 legacy 경로 request count 집계 |
| 판정 | 관찰 기간 중 합계 요청 0건 | 정리 후보로 등록(자동 삭제 금지) |
| 정리 | 마스터 승인 후 | redirect 제거. 제거 전 1회 더 로그 확인, 롤백용 nginx/route 변경 이력 보존 |

요청이 1건이라도 있는 URL은 정리하지 않고 다음 관찰 주기로 넘긴다. 인증·북마크 영향이 큰 `/setup`·`/control/acp`·`/execution/mcp`는 정리 대상에서 제외하고 canonical로 계속 유지한다. 정리 시점·대상은 문서가 아니라 접근 로그 실측으로만 결정한다.

| 기존 URL | 목표 canonical URL | 호환 처리 |
|---|---|---|
| `/providers` | `/execution/providers` | query 보존 308; ACP fragment가 있으면 `/control/acp`로 client resolve |
| `/runtime-config` | `/control/runtime` | query 보존 308 |
| `/control/acp` | 동일 | 기존 별칭을 실제 화면으로 승격 |
| `/execution/mcp` | 동일 | 기존 별칭을 실제 화면으로 승격 |
| `/fallback`, `/llm-usage`, `/quota` | `/execution/fallback`, `/execution/usage`, `/execution/quota` | query 보존 308 |
| `/policy`, `/approvals`, `/audit` | `/control/policy`, `/control/approvals`, `/control/audit` | query 보존 308 |
| `/embedding`, `/knowledge-ops` | `/knowledge/embedding`, `/knowledge/operations` | query 보존 308 |
| `/users`, `/credentials`, `/secrets`, `/feature-flags`, `/profile-ops` | 대응 `/management/*` | query 보존 308 |
| `/backup`, `/security-updates`, `/license` | 대응 `/operations/*` | query 보존 308 |
| `/setup` | 동일 | redirect 껍데기를 제거하고 온보딩 canonical로 승격 |
| `/infra` 및 알려진 tab/hash | 위 분해 대상 | tab/hash mapping 후 `replace`; 알 수 없는 값은 `/operations/health` |

API URL은 기존 `/v1/acp/*`, `/v1/mcp/*` 등을 삭제하거나 이름만 바꾸지 않는다. 새 집계 API만 추가하고 기존 도메인별 API를 내부에서 호출한다. 이는 화면 IA를 아키텍처 경계 변경으로 오해하지 않게 하고 기존 클라이언트 호환성을 지킨다.

## 4. 온보딩 설계 (기준 1)

### 진입, 저장, 재개

로그인 성공 후 `GET /v1/setup/progress`를 조회한다. `required_complete=false`인 최초 세션은 `/setup`으로 `replace`하며, 이후 로그인에는 대시보드 상단에 “설정 이어하기” 배너를 유지한다. 사용자가 “나중에”를 선택하면 현재 세션에서는 대시보드로 갈 수 있지만 필수 단계는 skipped로 완료 처리하지 않는다.

진행 상태는 브라우저 localStorage가 아니라 서버의 tenant별 상태로 저장한다. 다만 각 단계의 `complete`는 사용자 체크가 아니라 최신 자동 검사 결과에서 계산한다. 응답은 `schema_version`, `current_step`, 각 단계의 `status`, `checked_at`, `blocking_checks`, `optional_skipped`, `percent`를 포함하고 비밀값은 포함하지 않는다.

재진입하면 첫 미완료 필수 단계로 이동한다. 완료한 단계도 다시 열 수 있고, 구성 변경이나 점검 만료로 완료 조건이 깨지면 `complete`에서 `needs_attention`으로 되돌아간다. 진행률은 필수 체크의 완료 비율을 기본값으로 계산하고 선택 단계는 별도 “선택 2/3”로 표시해 건너뛰기가 전체 완료율을 왜곡하지 않게 한다.

### 단계와 자동 완료 판정

| Step | 구분 | 사용자 과업 | 자동 완료 판정 | 건너뛰기/후속 |
|---:|---|---|---|---|
| 1. 환경 확인 | 필수 | 발견된 배포 환경과 핵심 서비스 확인 | Admin backend, DB, CP가 `healthy`; 검사 시각이 정책 TTL 이내 | 건너뛸 수 없음; 실패 항목별 운영 상태 링크 |
| 2. 실행 경로 | 필수 | 추천 실행 경로를 선택하고 연결 테스트 | ACP/Hermes 또는 외부 LLM 중 현재 runtime mode에 맞는 경로가 저장·적용·test passed | 건너뛸 수 없음; `/control/acp` 또는 `/execution/providers` |
| 3. 사용자 진입 채널 | 필수 | 자동 발견된 채널 또는 OAuth 승인으로 하나 연결 | Mattermost/Slack 등 지원 인그레스 중 최소 1개가 configured+applied+healthy | 건너뛸 수 없음 — **마스터 확정(2026-09-15): 채널 연결 1개 이상을 필수로 둔다** |
| 4. 정책과 관리자 | 필수 | 관리자 권한·기본 정책 검토 | L5 관리자 존재, active policy version 존재, policy validate 통과 | 건너뛸 수 없음; 사용자/정책 화면 링크 |
| 5. 도구 연결(MCP) | 선택 | 발견된 MCP 서버를 검토·연결 | 선택한 서버마다 저장 및 test passed; 아무것도 선택하지 않으면 incomplete가 아니라 skipped | 건너뛰기 가능, 언제든 재개 |
| 6. 지식 연결 | 선택 | Outline/Notion/embedding을 연결 | 선택한 source와 embedding의 구성·접근 test passed | 건너뛰기 가능, 지식 기능 사용 전 경고 |
| 7. 운영 알림 | 선택 | SMTP/Slack 알림 경로 확인 | 적어도 하나의 알림 목적지 test passed | 건너뛰기 가능 |
| 8. 최종 검증 | 필수 | 요약을 확인하고 설정 완료 | Step 1~4가 모두 complete이고 최신 readiness snapshot에 blocking failure 0 | 완료 후 `/` 이동; 결과 및 미선택 기능 표시 |

Step 3의 “콘솔 전용” 모드는 **지원하지 않는다(마스터 확정 2026-09-15)** — 최소 한 채널의 성공이 완료 필수 조건이다. Step 8(최종 검증)은 채널 연결 없이는 완료될 수 없다. 단, Step 3의 *발견·연결 시도* 자체는 관리자에게 실패 원인과 조치 링크를 제공해야 하며, 자격증명이 아직 없는 상태는 실패가 아니라 `AUTH_REQUIRED`와 승인 안내로 표현한다.

### 화면 동작

`StepWizard`는 좌측/상단 단계 목록, 현재 단계 본문, 하단의 이전·다음·나중에 버튼, 전체 진행률로 구성한다. 다음 버튼은 해당 필수 검사 성공 시 활성화되지만 사용자가 실패 원인과 조치 링크는 언제든 볼 수 있다. 브라우저 뒤로가기는 단계 상태를 URL `/setup?step=runtime`에 반영해 예측 가능하게 동작한다.

자동 검사는 단계 진입 시 실행하되 명시적 “다시 검사”가 있고, 진행 중에는 skeleton과 `aria-live=polite` 상태를 제공한다. 실패는 “연결 실패”만 말하지 않고 `AUTH_REQUIRED`, `UNREACHABLE`, `TIMEOUT`, `NOT_APPLIED`, `PERMISSION_DENIED`, `MISCONFIGURED` 같은 안정된 코드와 관리자 문장, 다음 조치 링크를 함께 표시한다.

완료 후에도 `/setup`은 사라지지 않고 “설정 상태” 화면으로 바뀐다. 필수 상태가 깨지면 마지막 성공 시각과 현재 실패를 비교하며, 다시 완료 버튼을 누르게 하지 않고 자동으로 `needs_attention`을 반영한다.

## 5. 연결·장애 인지 설계 (기준 2)

### 전역 상태와 연결 카드

대시보드 상단에는 접을 수 없는 `RequiredConnectionsSummary`를 배치한다. “필수 4개 중 정상 3 / 조치 필요 1”, 마지막 전체 점검 시각, 가장 심각한 1~3개 문제, “문제 해결” 링크, “모두 다시 검사”를 보여준다. 모든 정상이면 높이를 줄이되 마지막 점검 시각과 상세 열기 링크는 유지한다.

요약은 `GET /v1/admin/readiness` 한 번으로 가져오며 페이지별 요청을 합산하지 않는다. 응답은 CP, execution path, ingress, policy 같은 **논리적 필수조건**과 그 조건을 뒷받침하는 기존 도메인 점검 결과를 포함한다. 집계기는 기존 API/서비스의 상태를 읽을 뿐 CP·EG·Policy의 책임을 옮기지 않는다.

각 연결은 동일한 카드로 표시한다. 제목·설명·상태 배지·현재 사용 중인 비밀값의 “설정됨” 여부·source·last checked·latency를 보여주되 값 자체는 반환하지 않는다. 상태는 다음 세 가지이며 아이콘과 텍스트를 색과 함께 사용한다.

| 상태 | 판정 | 카드 문장 예 | 기본 다음 조치 |
|---|---|---|---|
| 정상 | configured, applied, 최근 test passed | “정상 · 18ms · 2분 전 확인” | 상세 보기/다시 테스트 |
| 주의 | 미구성 선택 기능, 점검 만료, 저장됐으나 미적용, degraded | “저장됐지만 서비스에 아직 반영되지 않음” | 적용 안내/검사/설정 계속 |
| 실패 | 필수 누락, 인증 실패, unreachable, timeout | “인증이 거부됨 · 권한 승인이 필요함” | OAuth 승인/발견 후보 선택/운영 상태 |

### 세부정보 입력 없는 연결

일반 흐름은 “발견 → 추천 후보 선택 → 연결 → 테스트” 네 동작이다. `GET /v1/admin/connections/discovery?kind=mattermost`는 아래 우선순위로 후보를 찾고, 브라우저에는 마스킹한 표시값과 근거만 반환한다.

1. 현재 프로세스에 이미 주입된 환경변수와 secret reference를 상속한다. 값은 서버 내부에서만 사용한다.
2. 동일 배포 manifest/Compose network/서비스 레지스트리의 서비스명과 health metadata를 읽는다. Docker socket을 새로 개방하는 설계는 하지 않는다.
3. 동일 호스트의 제품 기본 포트와 loopback 후보를 제한된 allowlist로 probe한다. 임의 포트 스캔은 하지 않는다.
4. 기존 저장 구성과 마지막 정상 후보를 재사용하되 `source`, `last_seen_at`, `confidence`, `requires_restart`를 표시한다.
5. 외부 SaaS는 “제공자에서 승인” OAuth 흐름을 우선한다. OAuth가 없는 경우 이미 배포된 secret reference를 선택하며 원문 API 키 입력은 접힌 L5 고급 설정에서만 허용한다.

후보 응답 예시는 `candidate_id`, `kind`, `display_target`, `source: env|manifest|registry|default|saved`, `confidence: high|medium|low`, `credential_state: available|authorization_required|missing`, `applied`, `requires_restart`다. `candidate_id`는 짧은 TTL의 서버측 참조이며 URL·token·header 원문을 인코딩하지 않는다. 기본값 프리필은 발견값을 읽기 전용 요약으로 보여주고, 낮은 신뢰도일 때만 고급 필드를 연다.

“자동 연결”은 외부 서비스의 동의나 자격증명을 만들어낸다는 뜻이 아니다. 환경에 안전하게 제공된 값을 재사용하거나 제공자 승인 화면으로 보내는 뜻이며, 인증이 없는 경우 “승인이 필요합니다”를 실패 원인이 아닌 다음 단계로 정확히 표현한다.

### 연결 테스트 표준 계약

모든 화면은 `TestConnectionButton`을 사용하고, backend adapter는 아래 envelope을 반환한다. 기존 `/v1/acp/test`, `/v1/mcp/servers/{name}/test` 등은 단계적으로 이 envelope에 맞추되 경로는 유지한다.

```ts
type TestConnectionRequest = {
  candidate_id?: string;       // 서버측 발견 후보, 비밀값 비포함
  config_revision?: string;    // 테스트할 저장 revision
  mode?: "safe" | "write_probe"; // 기본 safe, 부작용 없는 점검
};

type TestConnectionResult = {
  ok: boolean;
  status: "healthy" | "warning" | "failed";
  code: "OK" | "AUTH_REQUIRED" | "UNREACHABLE" | "TIMEOUT" |
        "NOT_APPLIED" | "PERMISSION_DENIED" | "MISCONFIGURED" | "UNKNOWN";
  summary: string;             // 번역 가능한 message_key 병행
  message_key: string;
  checked_at: string;
  latency_ms?: number;
  target_display?: string;     // 마스킹된 host/service 이름
  applied: boolean;
  requires_restart: boolean;
  next_action?: { label_key: string; href: string };
  correlation_id: string;      // 비밀 없는 서버 로그 상관키
};
```

버튼은 클릭 즉시 중복 클릭을 막고 spinner와 “연결 확인 중”을 표시한다. HTTP 계열 기본 서버 timeout은 8초, 내부 quick health는 5초, 클라이언트 abort는 서버 timeout+2초로 통일하며 adapter가 더 짧은 제한을 요구하면 응답 metadata로 알린다. 202 응답을 쓰는 장기 점검은 `check_id`와 상태 조회 URL을 반환한다.

에러는 원시 exception/응답 body를 그대로 사용자에게 노출하지 않는다. HTTP 401/403→인증 또는 권한, DNS/connection refused→도달 불가, timeout→시간 초과, 저장 revision과 effective revision 불일치→미적용으로 정규화한다. 상세 진단에는 상관키와 재시도만 제공하고 토큰, 비밀번호, Authorization header, 전체 URL query는 로그·응답·문서에 쓰지 않는다.

### 저장과 실제 반영의 분리

모든 구성 응답은 `persisted`, `applied`, `config_revision`, `effective_revision`, `requires_restart`, `apply_strategy`, `updated_at`, `applied_at`을 공통 필드로 반환한다. 저장 성공 Toast는 “저장됨”만 말하며 `applied=false`이면 성공색으로 종료하지 않고 주의 banner를 이어서 표시한다.

`apply_strategy`는 `immediate`, `reload`, `restart_required`, `external_action` 중 하나다. 재시작이 필요한 ACP/Mattermost/Outline/Notion/Slack/SMTP 등은 “저장됨 · 아직 미반영”과 영향받는 서비스 이름, 운영자가 해야 할 승인된 절차 링크를 표시한다. 콘솔이 임의로 서비스를 재시작하지 않으며 이 문서도 restart API를 제안하지 않는다.

적용 후에는 readiness가 `effective_revision === config_revision`인지 확인하고 자동 재검사한다. 적용 권한이나 자동화가 없으면 “운영 담당자에게 요청”용 복사 가능한 비밀 없는 요약을 제공한다. 저장값을 대상으로 한 테스트와 실제 적용값을 대상으로 한 테스트를 `tested_revision`으로 구분해, 저장 전 draft가 성공했다고 운영 반영까지 성공한 것으로 오해하지 않게 한다.

## 6. 공통 UX 킷 설계 (기준 3)

### 기반 계층

`components/ui`의 원시 요소 위에 `components/admin` 복합 컴포넌트를 둔다. 데이터 접근은 `lib/admin-api`의 typed client, query key factory, query/mutation hooks로 모아 요청 취소·dedupe·cache·retry·invalidation을 표준화한다. 구현 시 TanStack Query 도입 여부는 결정 항목이지만, 컴포넌트 계약은 특정 라이브러리에 종속시키지 않는다.

공통 상태 순서는 initial loading→Skeleton, failed/no stale data→ErrorState, success/empty→EmptyState, success/data→DataTable이다. background refresh 실패는 기존 데이터를 유지하고 Toast/상단 상태로 알린다. mutation은 버튼 단위 pending, 성공 Toast, 실패 인라인 오류+Toast, 성공 후 관련 query invalidation을 기본으로 한다.

### 컴포넌트와 props 계약

```ts
type ColumnDef<T> = {
  id: string;
  header: React.ReactNode;
  accessor?: (row: T) => unknown;
  cell?: (row: T) => React.ReactNode;
  sortable?: boolean;
  hideable?: boolean;
  width?: number | string;
  align?: "start" | "center" | "end";
};

type DataTableProps<T> = {
  rows: T[];
  columns: ColumnDef<T>[];
  rowKey: (row: T) => string;
  loading?: boolean;
  error?: AdminError;
  query: { search?: string; sort?: { id: string; direction: "asc" | "desc" }; page: number; pageSize: number };
  onQueryChange: (next: DataTableProps<T>["query"]) => void;
  totalRows: number;
  pageSizeOptions?: number[];       // 기본 [20, 50, 100]
  searchable?: boolean;
  searchPlaceholderKey?: string;
  selection?: { selected: Set<string>; onChange(ids: Set<string>): void };
  rowActions?: (row: T) => RowAction[];
  empty: EmptyStateProps;
  ariaLabel: string;
};

type ConfirmDialogProps = {
  open: boolean;
  title: string;
  description: React.ReactNode;
  targetLabel?: string;
  consequence?: string;
  confirmLabel: string;
  cancelLabel?: string;
  tone?: "default" | "danger";
  requireText?: string;             // 고위험 작업만 사용
  pending?: boolean;
  error?: string;
  onConfirm(): Promise<void> | void;
  onOpenChange(open: boolean): void;
};

type ToastInput = {
  id?: string;
  title: string;
  description?: string;
  variant: "success" | "warning" | "error" | "info";
  action?: { label: string; onClick(): void };
  durationMs?: number;              // error는 자동 닫힘 비활성 가능
};

type EmptyStateProps = {
  icon?: React.ComponentType;
  title: string;
  description: string;
  primaryAction?: ActionProps;
  secondaryAction?: ActionProps;
  filtered?: boolean;               // true면 “필터 초기화”를 기본 행동으로
};

type ErrorStateProps = {
  title: string;
  description: string;
  code?: string;
  correlationId?: string;
  retry?: () => void;
  helpHref?: string;
  compact?: boolean;
};

type SkeletonProps = {
  variant: "text" | "card" | "table" | "form";
  rows?: number;
  ariaLabel?: string;
};

type FormFieldProps = {
  id: string;
  label: string;
  required?: boolean;
  hint?: React.ReactNode;
  error?: string;
  children: React.ReactElement;      // id/aria-describedby/aria-invalid 연결
};

type StatusBadgeProps = {
  status: "healthy" | "warning" | "failed" | "unknown";
  label?: string;
  showIcon?: boolean;
  size?: "sm" | "md";
};

type TestConnectionButtonProps = {
  connectionId: string;
  candidateId?: string;
  configRevision?: string;
  disabled?: boolean;
  onResult?(result: TestConnectionResult): void;
};

type StepWizardProps = {
  steps: Array<{ id: string; label: string; required: boolean; status: "pending" | "checking" | "complete" | "needs_attention" | "skipped" }>;
  currentStep: string;
  progress: { requiredDone: number; requiredTotal: number; optionalDone: number; optionalTotal: number };
  onStepChange(id: string): void;
  onNext(): Promise<void> | void;
  onBack(): void;
  onSkip?(): void;
  canContinue: boolean;
  busy?: boolean;
  children: React.ReactNode;
};
```

`DataTable`의 검색·정렬·page·pageSize는 URL query와 동기화하고 서버 조회가 기본이다. 작은 고정 목록만 `mode="client"` 예외를 허용하며 전체 행 수가 threshold를 넘으면 경고한다. 페이지가 바뀌어도 검색·정렬을 보존하고, 새 검색은 page 1로 되돌린다. 컬럼 header는 버튼/`aria-sort`를 사용하고 행 전체를 클릭 영역으로 만들 때도 내부 action의 키보드 동작을 보장한다.

`ConfirmDialog`는 Radix/shadcn Dialog primitives로 focus trap, Escape 취소, 닫힌 후 trigger focus 복귀를 보장한다. 삭제 대상과 영향 범위를 문장으로 보여주며, 일반 삭제에 습관적인 텍스트 재입력을 요구하지 않는다. Toast는 `aria-live` 영역을 하나만 두고, 폼 검증 오류는 Toast만 띄우지 않고 해당 `FormField`에도 연결한다.

### 이력·조회·수정·삭제 표준 흐름

| 동작 | 표준 흐름 | 성공 | 실패/복구 |
|---|---|---|---|
| 이력 | 최신순 기본, 기간/행위자/상태 필터, URL query 보존, 상세 drawer/dialog | 선택 행 상세와 관련 구성 revision 링크 | 기존 결과 유지, 재조회와 correlation ID 제공 |
| 조회 | Skeleton→DataTable 또는 EmptyState, 검색 debounce 300ms, 서버 정렬·페이지 | 총 건수·조회 조건·갱신 시각 표시 | 첫 조회는 ErrorState, background 오류는 stale 표시 |
| 수정 | 행 action→drawer/dialog, 기존값 로드, dirty guard, 서버 검증 | 닫기→Toast→해당 query invalidation; 적용 상태 별도 표시 | 입력값 유지, 필드 오류 focus, 충돌 시 최신값 비교 |
| 삭제 | 행 action→ConfirmDialog, 대상·영향 명시, 확인 후 pending | 행 제거 또는 재조회, Undo 가능한 soft delete만 action 제공 | dialog 유지, 오류를 dialog 안과 Toast에 표시 |

파괴적이지 않은 enable/disable, probe, publish도 mutation 표준을 사용한다. 정책 publish/rollback, profile reset처럼 삭제보다 위험한 동작은 영향, 권한, 예상 version을 별도 확인하고 감사 이력으로 이동할 수 있어야 한다. 브라우저 native `alert/confirm`은 모두 제거한다.

### 페이지 적용 체크리스트

- [ ] 제목, 설명, breadcrumb, 주 action의 위치가 공통 PageHeader 규칙을 따른다.
- [ ] initial loading/Error/Empty/data/background refresh 상태가 서로 배타적으로 정의됐다.
- [ ] 목록은 검색·정렬·페이지네이션·총 건수와 URL query를 제공하거나 예외 사유가 문서화됐다.
- [ ] 행 action 순서는 상세→수정→테스트/특수 동작→삭제이며 권한 없는 action은 숨김보다 이유 있는 disabled를 우선한다.
- [ ] create/edit는 같은 schema와 FormField를 쓰고 서버 validation code를 필드에 매핑한다.
- [ ] 삭제·rollback·reset은 ConfirmDialog, 성공/실패는 Toast와 인라인 상태를 사용한다.
- [ ] mutation 후 query invalidation, focus 복귀, 중복 제출 방지, unsaved-change guard가 동작한다.
- [ ] StatusBadge 문구·아이콘·색, 날짜/시간대, 숫자, 빈 값 표기가 공통 formatter를 쓴다.
- [ ] 비밀값은 설정 여부만 표시하며 DOM, URL, Toast, 로그, correlation context에 원문이 없다.
- [ ] 키보드만으로 전체 기능을 수행하고 200% 확대 및 좁은 화면에서 정보/동작이 유실되지 않는다.

## 7. 접근성·시각 규칙

라이트 모드를 기본으로 하고 Financial Dashboard 의미색은 정상 `#22C55E`, 주의 `#F59E0B`, 실패 `#DC2626`으로 고정한다. 단 이 원색들을 작은 본문 글자에 그대로 쓰면 흰 배경에서 AA를 만족하지 못할 수 있으므로 배지 배경/차트/아이콘에 사용하고, 본문 semantic text는 대비를 검증한 더 어두운 토큰(예: success text `#15803D`, warning text `#92400E`)을 사용한다. 색만으로 상태를 구분하지 않고 CheckCircle/TriangleAlert/XCircle SVG 아이콘과 정상/주의/실패 문구를 병행한다.

본문과 입력 텍스트는 WCAG 2.2 AA 기준 일반 텍스트 4.5:1, 큰 텍스트 3:1, UI 경계·아이콘 3:1을 만족해야 한다. 모든 interactive element는 `:focus-visible`에서 최소 2px outline과 충분한 offset을 보이고, modal focus trap·초기 focus·trigger focus 복귀를 테스트한다. heading 순서, landmark, label, description, error association을 자동 검사와 키보드 수동 검사 양쪽으로 검증한다.

hover, drawer, accordion, Toast의 전환은 150~300ms로 제한하고 위치 이동보다 opacity/color 전환을 우선한다. `@media (prefers-reduced-motion: reduce)`에서는 비필수 animation과 chart transition을 끄며 spinner는 정적 “처리 중” 문구를 병행한다. loading animation으로 레이아웃이 흔들리지 않게 skeleton 크기를 최종 콘텐츠와 맞춘다.

아이콘은 lucide 또는 검토된 inline SVG만 사용하고 이모지는 메뉴·상태·버튼에 쓰지 않는다. 아이콘 단독 버튼에는 accessible name과 tooltip을 제공한다. 보라/핑크 그라데이션은 브랜드·배경·CTA 어디에도 사용하지 않으며, status surface는 저채도 단색 배경과 명확한 border를 사용한다.

표는 mobile에서 핵심 컬럼을 보존하고 나머지는 row detail로 접는다. 가로 스크롤만 제공할 때는 스크롤 가능성을 시각적으로 알리고 첫/마지막 컬럼 action을 sticky 처리할 수 있다. chart에는 동일 데이터를 표/요약으로 제공하고 SVG에 `role`, label 또는 장식용 `aria-hidden`을 정확히 지정한다.

## 8. 마이그레이션 계획

### 공통 킷 선행 순서

1. semantic token과 공통 error/test/config envelope 타입을 먼저 고정한다.
2. Dialog primitive, Toast provider, FormField, Skeleton, EmptyState, ErrorState, StatusBadge를 만든다.
3. typed API client와 query key/cache/mutation 정책을 만든다.
4. ConfirmDialog와 TestConnectionButton을 만든다.
5. DataTable과 URL query adapter를 만든다.
6. StepWizard, readiness summary, connection card를 만든다.

이 순서를 지켜야 후속 화면이 다시 임시 modal, 로컬 error card, 별도 table을 만들지 않는다. 각 단계는 Storybook 또는 격리 harness가 없다면 전용 component test route가 아닌 테스트 코드로 상태 조합을 검증하고, production route에 실험 요소를 남기지 않는다.

### Phase 계획

| Phase | 대상 화면/작업 | 산출물 | 검증 게이트 | 롤백 |
|---|---|---|---|---|
| 0. 계약·계측 | 전체, 기존 API 호환 조사 | 상태 사전, URL mapping, API schema, baseline 캡처 | schema 예제 contract test; 기존 24 URL smoke 기준 확정 | 문서/계약만 폐기, runtime 영향 없음 |
| 1. UX 기반 | layout 및 공통 컴포넌트 | 위 공통 킷, query 계층, semantic tokens, i18n namespace | component a11y/keyboard, typecheck, 변경 파일 lint, visual states | 기존 컴포넌트 import로 되돌리는 build-time flag |
| 2. 온보딩·준비상태 | `/`, `/setup`, backend 집계 API | StepWizard, readiness summary, progress/readiness/discovery 계약 | 자동 완료 판정 contract test, 첫/재로그인/skip/resume e2e, secret redaction | 기존 dashboard 유지, `/setup` compatibility entry로 전환 |
| 3. 연결·IA 분해 | `/infra`, ACP, MCP, mm/slack/notion/oauth/smtp/outline | `/connections/*`, canonical ACP/MCP, infra compatibility resolver, connection cards | 기존/신규 URL render, 발견→테스트, saved/not-applied 시나리오 | 새 nav feature flag off, 기존 route feature components 유지 |
| 4. 제어·실행 목록 | providers/fallback/runtime/policy/approvals/audit/usage/quota | DataTable·Dialog·Toast 적용, native popup 제거 | CRUD, 정렬/검색/page URL, publish/rollback 권한 및 오류 test | 화면 단위 flag로 legacy view 복귀, API 경로 유지 |
| 5. 지식·운영·관리 | knowledge, infra status, backup/security/license, users/credentials/secrets/flags/profile | 나머지 화면 공통 상태·CRUD 전환 | 화면별 targeted test, empty/error/loading, keyboard, 기존 URL smoke | 그룹/화면 단위 legacy view 복귀 |
| 6. 기본 IA 전환 | layout/nav/i18n, alias 정리 | 6그룹 sidebar 기본화, server redirects, 운영 runbook | build/typecheck/py_compile, redirect query/hash, 링크 crawler, AA audit | old-nav flag와 compatibility routes 유지 |

롤백은 DB나 서비스 재시작이 아니라 frontend 화면/네비게이션 feature flag와 additive API adapter 수준에서 가능해야 한다. 새 API가 실패하면 기존 도메인 API로 fallback할 수 있으나, 실패를 정상으로 가장하거나 stale 상태를 최신으로 표시하지 않는다. permanent redirect는 충분한 관찰 기간과 마스터 승인 전에는 307로 운용하는 선택지도 남긴다.

### 화면별 작업 목록

| 현재 화면 | 목표 | 주요 작업 |
|---|---|---|
| `/` | `/` | 필수 연결 요약, 설정 이어하기, 조치 우선 dashboard, 공통 상태 적용 |
| `/setup` | `/setup` | redirect 제거, 8단 StepWizard와 자동 판정·재개 구현 |
| `/infra` | compatibility resolver | 11탭 제거, tab/hash→새 canonical URL mapping |
| `/control/acp` + Providers ACP section | `/control/acp` | ACP 단일 소유, 자동 발견, 표준 테스트, persisted/applied 분리 |
| `/control/runtime` + `/runtime-config` | `/control/runtime` | redirect 방향 역전, snapshot 이력 DataTable, publish/rollback dialog |
| `/execution/mcp` + Infra MCP | `/execution/mcp` | MCP 단일 소유, 발견 후보, CRUD·테스트 표준화 |
| `/providers` | `/execution/providers` | ACP 제거, provider CRUD DataTable, secret ref/OAuth 우선 |
| `/fallback` | `/execution/fallback` | 순서 편집 접근성, 제거 ConfirmDialog, 저장/적용 상태 |
| `/llm-usage` | `/execution/usage` | 기간·tenant·provider 검색/필터, 서버 정렬·페이지, chart 대체 정보 |
| `/quota` | `/execution/quota` | tenant 조회, FormField 검증, 변경 이력 링크 |
| `/policy` | `/control/policy` | 탭 책임 정리, history DataTable, publish/rollback 고위험 확인 |
| `/approvals` | `/control/approvals` | 검색·위험 필터·페이지, decision pending/실패 표준화 |
| `/audit` | `/control/audit` | 서버 최신순·기간/행위자 검색·페이지, 무결성 ErrorState |
| Infra MM/Slack/Notion/OAuth/SMTP panels | `/connections/*` | 독립 route, 발견·승인·테스트·적용 카드, 고급 입력 접기 |
| Infra OL panel | `/knowledge/outline` | 단일 Outline 연결 화면, secret ref와 접근 테스트 |
| `/embedding` | `/knowledge/embedding` | 자동 후보·기본값, FormField와 적용 상태 |
| `/knowledge-ops` | `/knowledge/operations` | 동기화 이력 DataTable, dry-run/result 상태 표준화 |
| Infra services/live/unified | `/operations/services`, `/operations/health` | 등록 CRUD와 관측을 분리, 중복 row model 통합 |
| `/backup` | `/operations/backup` | 백업 이력 검색·정렬·페이지, trigger 결과·오류 표준화 |
| `/security-updates` | `/operations/security-updates` | severity filter, CVE 목록 DataTable, 외부 링크 접근성 |
| `/license` | `/operations/license` | 상태·만료 경고, 검증 FormField/Toast, 값 노출 방지 |
| `/users` | `/management/users` | 계정/매핑 책임 분리, DataTable, create/edit dialog, 삭제 확인 |
| `/credentials` | `/management/credentials` | 상태/최근 이력 DataTable, 검색·filter·pagination |
| `/secrets` | `/management/secrets` | 설정 여부 EmptyState, rotation guide, 원문 비노출 검증 |
| `/feature-flags` | `/management/feature-flags` | DataTable, toggle 확인 기준, 변경 이력/actor 표시 |
| `/profile-ops` | `/management/profile-operations` | backfill/reset 결과, 고위험 ConfirmDialog, 이력 링크 |
| `layout.tsx` | grouped shell | 23개 평면 nav를 6그룹+고정 2항목으로 교체, 모바일 동일 계층 |

## 9. 검증 게이트

### 구현 Phase 공통 명령

아래는 후속 구현의 merge gate이며 이 문서 작성만으로 production을 변경하거나 서비스를 기동하지 않는다. 전체 pytest와 `verify-evidence*`는 일반 worktree 개발 게이트로 사용하지 않고, 변경 범위의 frontend component/route test와 backend contract test를 우선한다.

```bash
cd admin-console && npm run build
cd admin-console && npx tsc --noEmit
python3 -m py_compile admin-console/backend/*.py
```

`npm run build`는 Next route와 client/server 경계를 포함한 production build 성공을 확인한다. `tsc --noEmit`은 별도 산출물 없이 props/API contract의 정적 타입을 확인한다. Python 변경이 있는 Phase만 `py_compile`을 실행하며, 관련 backend test가 존재하면 변경 router의 targeted test를 추가한다. 전체 lint는 baseline 해결 전 완료 조건으로 삼지 않고 변경 파일/디렉터리만 검사한다.

### URL·동작·접근성 게이트

- 기존 24개 대시보드 URL과 `/login`이 404/500 없이 렌더되거나 명세된 redirect로 도달해야 한다. 인증 필요 redirect도 무한 loop가 없어야 한다.
- `/infra`의 11개 legacy tab/hash, query string, `/providers` ACP 링크, 세 alias URL을 표로 만든 smoke test로 확인한다.
- 연결은 발견 없음/하나/복수, 인증 필요, timeout, 저장 성공+미적용, 적용 revision 일치의 contract test를 모두 통과해야 한다.
- 모든 목록은 loading/error/empty/data/filtered-empty/background-error를 검증하고 CRUD mutation 후 올바른 query만 invalidate해야 한다.
- axe 계열 자동 검사에 더해 Tab/Shift+Tab/Enter/Space/Escape, modal focus 복귀, 200% 확대, reduced motion을 수동 확인한다.
- API 응답 snapshot과 브라우저 DOM/URL/console에 secret 원문이 없는지 redaction test로 확인한다.

### i18n 키 규칙

새 키는 `admin.<feature>.<component>.<meaning>` 구조로 ko/en에 같은 변경에서 동시에 추가한다. 예를 들어 `admin.connections.status.notApplied`, `admin.common.table.nextPage`, `admin.setup.steps.runtime.title`처럼 위치가 아니라 의미를 이름에 담는다. 기존 `nav.*` 키는 compatibility 기간에 삭제하거나 뜻을 바꾸지 않는다.

문장 조합을 코드에서 하지 않고 ICU 또는 현 i18n 계층이 지원하는 named parameter로 완전한 문장을 번역한다. 상태 code는 API에서 안정된 영문 enum으로 유지하고, 사용자 문구는 `message_key`로 번역한다. CI에는 두 JSON의 key set 동일성, JSON parse, 사용되지 않는 신규 키와 누락 키 검사를 추가한다.

## 10. 결정 필요 항목 (3건 확정 반영, 나머지 미해결)

1. ~~기존 URL 307→308 전환·제거 정책~~ → **확정(2026-09-15): 관찰 기간 후 미사용 URL 정리는 접근 로그 실측으로 판정한다.** §3 “URL 호환 규칙”의 정리 정책 표를 따른다.
2. ~~사이드바 라벨 용어~~ → **확정(2026-09-15): 초보자 친화 과업 용어를 쓴다.** 대응 표는 §11.
3. ~~Step 3 인그레스 필수 여부~~ → **확정(2026-09-15): 채널 연결 1개 이상을 필수로 두고, “콘솔 전용” 모드는 지원하지 않는다.** §4 참조.
4. 자동 발견이 읽을 수 있는 배포 metadata의 공식 source와 권한 경계를 정해야 한다. Docker socket 직접 접근은 기본안에서 제외한다.
5. OAuth를 지원하지 않는 연결에서 수동 API 키 입력을 계속 허용할지, secret manager reference만 허용할지와 L5 break-glass 정책이 필요하다.
6. `applied=false` 구성의 적용을 담당할 운영 시스템과 runbook URL, readiness가 effective revision을 확인하는 표준 방법을 정해야 한다.
7. 공통 cache 계층에 TanStack Query를 추가할지 경량 사내 wrapper를 구현할지, bundle/운영 표준을 기준으로 선택해야 한다.
8. 목록의 기본 page size, 서버 검색 API를 먼저 추가할 화면 우선순위, 감사/사용량 데이터의 보존 기간과 최대 조회 범위를 정해야 한다.
9. 새 nav와 화면별 legacy rollback flag의 소유자, 종료 조건, flag 제거 시점을 정해야 한다.
10. Financial palette의 dark mode 확장 여부가 필요하다. v1.0은 라이트 기본만 의무화하고 dark mode는 대비 토큰이 승인될 때까지 비범위로 둔다.

이 결정들은 UX 구현 방식에는 영향을 주지만 ACP/MCP/Policy/Knowledge의 실행 책임이나 데이터 소유권을 바꾸지 않는다. 승인 전에는 additive API, 기존 URL 유지, 비밀값 비노출, 서비스 수동 재시작 금지라는 보수적 기본값을 적용한다.

## 11. 마스터 확정 사항 (2026-09-15)

| # | 항목 | 확정 내용 | 반영 위치 |
|---:|---|---|---|
| 1 | 사이드바 용어 | **초보자 친화 과업 용어**를 쓴다(아키텍처 용어는 내부·문서용) | §3, 본 절 표 |
| 2 | 기존 URL 처리 | 보존 → 관찰 기간 → **접근 로그 실측으로 미사용 URL 정리**(자동 삭제 금지, 마스터 승인 후) | §3 “정리 정책” |
| 3 | 온보딩 필수 조건 | **채널 연결 1개 이상 필수**, “콘솔 전용” 운영 모드 미지원 | §4 Step 3·Step 8 |

### 사이드바 라벨 대응표

| 그룹(사용자 라벨) | 아키텍처 용어 | 포함 화면 |
|---|---|---|
| 대시보드(고정) | — | `/` |
| 시작하기(고정) | — | `/setup` |
| 연결 | Ingress | `/connections/*` (Mattermost·Slack·Notion·OAuth·SMTP) |
| 설정 | Control Plane | `/control/*` (ACP·런타임 구성·정책·승인·감사) |
| 실행 | Execution | `/execution/*` (MCP·모델 제공자·Fallback·사용량·Quota) |
| 지식 | Knowledge | `/knowledge/*` (Outline·Embedding·동기화) |
| 모니터링 | Operations | `/operations/*` (상태·서비스 등록·백업·보안 업데이트·라이선스) |
| 관리 | Management | `/management/*` (사용자·자격증명·비밀값·기능 플래그·프로필 작업) |

라벨 근거: “제어”·“운영”은 아키텍처 내부 용어로 초기 관리자에게 의미가 즉시 전달되지 않고, “설정”·“모니터링”이 실제 과업을 더 정확히 가리킨다.
내부 코드·API·문서의 아키텍처 용어(Inress/Control Plane/Execution/Knowledge/Operations/Management)는 유지한다 — 라벨 변경을 경계 변경으로 오해하지 않는다.

### 남은 결정 필요 항목

§10의 4~10번(자동 발견 metadata source·권한 경계, OAuth 미지원 연결의 수동 키 정책, `applied=false` 적용 담당·runbook, cache 계층 구현 방식, 목록 기본 page size·검색 API 우선순위, flag 소유자·종료 조건, dark mode 확장)은 구현 Phase 진행 중 해당 Phase 착수 전에 확정한다.
