# OAOS Company(P2) p0 상세설계 v1.0

> 상태: 구현 착수용 설계. 이 문서의 명령·스키마·수치는 계약과 목표이며 설치·실측 증거가 아니다. 제품 방향과 전체 Phase는 [Company 계획 v1.0](company-plan-v1.0.md), 에디션 구조와 게이트 규칙은 [아키텍처 v2.0](architecture-v2.0.md)을 따른다.

## 1. 범위와 완료 경계

Company는 이미 설치된 [Project](../editions/project/README.md)에 구성원별 비서와 최소 거버넌스를 더하는 단일 서버 수직 절편이다. p0는 **C01~C10**의 핵심 경로를 구현 대상으로 확정한다. 각 C Phase는 선행 계약·격리된 검증 환경이 준비된 뒤 대상 테스트, 실제 설치, read-back까지 **1 개발일 이내**를 목표로 한다. 외부 승인·조직 계정 준비·관찰 대기는 이 산정 밖이다. 하루 안에 증거가 없으면 해당 Phase는 미완료다.

| 구분 | Phase | p0 결정과 경계 |
|---|---|---|
| 포함 | C01~C03 | Project 선행 확인, 소유자 스키마, 무Docker 서비스, 구성원·외부 ID 매핑 |
| 포함 | C04~C05 | 두 구성원 비서와 개인 공간, 최소 Secret Vault, 자격 결속·철회 |
| 포함 | C06~C08 | 기본 거부·일회 JIT 승인·감사·권한 선검사 검색 |
| 포함 | C09~C10 | 구성원·연동·정책·상태의 최소 콘솔과 필수 G10 초기 설정·재개 |
| 제외 | C11~C14 | Slack·Notion·Google Workspace·Microsoft 365 실제 연동은 조직의 S 계정과 동의가 필요하다. p0에서는 §7의 계약만 고정한다. |
| 제외 | C15~C16 | G11·G12·G13 설치 연결과 선택 라우팅은 옵션의 실제 설치 Phase다. p0 기본 모델만 사용하며 게이트 화면·상태 계약은 §8에 고정한다. |
| 제외 | C17 | 전체 Company 최종 검증·운영 인계는 C15·C16 의존성을 유지한다. p0 종료는 C01~C10의 별도 증거로 판정하며 P2 완료로 부르지 않는다. |

Project의 채팅·위키·메일·nginx·TLS·PostgreSQL·Redis와 [Personal](../editions/personal/README.md)의 런타임을 승계한다. 추가 서버, 컨테이너 오케스트레이션, 외부 SaaS 재구축, 대규모 코드 재배치는 범위 밖이다. 신규 서버·신규 비용·사용량 과금 경로는 **승인 필요**다. Company 파일은 [Company 라이선스](../LICENSE-COMPANY) 경계를 따르고 공통 코드 재사용 시 [기존 라이선스](../LICENSE)와 제3자 권리를 확인한다. 별도 플랫폼은 동작·실패 사례를 참고하는 대상일 뿐 설계·완료 판정의 기준이 아니다.

## 2. 식별·데이터 계약 (C01·C03)

PostgreSQL의 별도 `company` 스키마를 기존 Project DB에 둔다. UUID는 내부의 불변 기본 키이고 외부 ID는 공급자별 문자열이다. 모든 조회·수정은 서버가 확인한 `org_id`와 `member_id`로 범위를 제한한다. 외부 입력의 `owner_id`를 신뢰하지 않는다. DB에 원문 토큰·문서 본문·프롬프트를 감사 자료로 저장하지 않는다.

| 테이블 | 필수 열·제약 | 의미 |
|---|---|---|
| `organizations` | `id UUID PK`, `slug UNIQUE NOT NULL`, `state CHECK(active,suspended)`, `created_at` | p0에서는 설치당 조직 1개. 추후 다중 조직을 위한 서버 증설은 하지 않는다. |
| `members` | `id UUID PK`, `org_id FK NOT NULL`, `project_user_id TEXT NOT NULL`, `role CHECK(admin,member)`, `state CHECK(invited,active,suspended,departed)`, `created_at`, `updated_at`, `UNIQUE(org_id,id)`; Project ID는 상태 조건부 부분 UNIQUE 인덱스 | Project 계정과 1:1 결속. 정지·퇴사 시 세션과 실행 권한 즉시 무효화. 관리자도 구성원이다. |
| `external_accounts` | `id UUID PK`, `org_id NOT NULL`, `member_id NOT NULL`, `provider`, `workspace_id`, `subject_id`, `state CHECK(pending,active,revoked)`, `(org_id,member_id) FK members`; 외부 주체는 상태 조건부 부분 UNIQUE 인덱스 | Slack 등 발신자·개인 OAuth 주체 매핑. 조직 앱 자격과 개인 위임 계정은 분리한다. |
| `assistants` | `id UUID PK`, `org_id`, `owner_id`, `state CHECK(active,suspended,revoked)`, `knowledge_namespace UUID UNIQUE`, `(org_id,owner_id) FK members`; owner는 상태 조건부 부분 UNIQUE 인덱스 | p0는 활성 구성원당 개인비서 1개. owner 변경 금지. 교체는 해제 후 새 ID 생성. |
| `secrets` | `id UUID PK`, `org_id`, `owner_id NULL`, `provider`, `purpose`, `ciphertext`, `nonce`, `key_version`, `state CHECK(active,revoked)`; owner 유무별 부분 UNIQUE 인덱스 | `owner_id=NULL`은 조직 앱 설정만 허용. 개인 자격은 owner 필수. 원문은 API·로그에서 읽지 못한다. |
| `policies` | `id UUID PK`, `org_id`, `action`, `risk`, `decision CHECK(deny,allow,approval)`, `version`, `UNIQUE(org_id,action)` | 명시 규칙이 없으면 deny. 관리자 변경은 버전 증가와 감사 기록을 한 트랜잭션에 묶는다. |
| `approvals` | `id UUID PK`, `org_id`, `requester_id`, `approver_id`, `action`, `resource_digest`, `context_digest`, `policy_version`, `expires_at`, `consumed_at NULL`, `state CHECK(pending,approved,denied,expired,consumed)` | 요청자와 승인자 분리; 일회 소비는 조건부 UPDATE로 원자 처리. |
| `audit_events` | `id UUID PK`, `org_id`, `at`, `actor_id NULL`, `owner_id NULL`, `event_type`, `action`, `resource_ref`, `decision`, `reason_code`, `request_id`, `metadata JSONB` | 추가 전용. 비밀·본문·검색 스니펫을 넣지 않는다. |
| `documents` / `chunks` | 문서: `id UUID PK`, `org_id`, `source`, `source_id`, `owner_id NULL`, `source_acl_version`, `deleted_at NULL`, `updated_at`, `UNIQUE(org_id,source,source_id)`; chunk: `id UUID PK`, `document_id FK`, `ordinal`, `body`, `UNIQUE(document_id,ordinal)` | 검색 후보는 문서 권한 검사 후에만 만든다. 개인 공간은 owner 필수. |

`org_id`가 있는 자식 테이블은 복합 FK로 조직 간 참조를 막는다. 부분 UNIQUE 인덱스는 `members(org_id,project_user_id) WHERE state <> 'departed'`, `assistants(org_id,owner_id) WHERE state <> 'revoked'`, `external_accounts(org_id,provider,workspace_id,subject_id) WHERE state <> 'revoked'`로 둔다. `secrets`는 조직 앱용 `(org_id,provider,purpose) WHERE owner_id IS NULL AND state = 'active'`와 개인용 `(org_id,owner_id,provider,purpose) WHERE owner_id IS NOT NULL AND state = 'active'`로 둔다. `assistants.owner_id`, 개인 `secrets.owner_id`, 문서의 개인 `owner_id`는 생성 후 UPDATE 금지 트리거로 고정한다. `members`의 `departed`는 되돌리지 않고 새 고용은 같은 Project ID라도 새 `id`로 등록하며, 퇴사 때 기존 외부 계정은 `revoked`로 바꾼다. 해제된 비서·외부 계정은 이력을 남기고 새 ID로 등록한다. 활성 외부 계정의 재동의는 같은 owner 행의 자격만 회전하고, 해제 후 재연결은 새 행을 만들며 다른 owner에게 연결하려면 기존 행을 먼저 철회한다. 같은 외부 주체를 두 owner에게 동시에 연결할 수 없다. 관리자 계정은 G10에서 Project ID의 실제 존재를 확인하고 처음 한 명만 만든다. 최소 한 명의 활성 관리자를 유지하며 마지막 관리자 정지·퇴사는 거부한다.

마이그레이션은 `company.schema_migrations(version, checksum, applied_at)`의 단조 버전과 트랜잭션 SQL 파일 한 개씩으로 관리한다. `checksum`은 적용한 SQL 파일의 SHA-256 값이다. C01은 빈 스키마 생성, FK·UNIQUE·CHECK·owner 트리거, 버전 read-back까지 맡는다. 재실행은 이미 적용된 동일 버전의 체크섬 확인 후 건너뛰고 불일치는 중단한다. 기존 Project 테이블은 변경하지 않는다. 다운그레이드는 자동 삭제하지 않고 설치 전 백업으로 복원한다.

## 3. 단일 서버 배포·이전 (C02)

기존 Project 호스트의 별도 검증 인스턴스 **E**에 한정해 설치한다. E는 운영 Project의 DB·서비스를 건드리지 않는 분리된 Project 기반과 Company 스키마·상태·포트를 사용한다. 설치 전 자원·권한·백업·원복 경로를 확인한다. **S**는 E에서 쓰는 기존 조직의 선택 연동 검증 계정이다. E 분리에 새 서버나 구독이 필요하면 승인 전 설치하지 않는다. 우선 [Project 설치기](../editions/project/install.sh)의 apt·체크포인트·단계 재개와 [공통 설치 라이브러리](../bootstrap/lib/common.sh)를 승계한다.

| 프로세스·유닛 | 실행 주체·바인딩 | 책임 |
|---|---|---|
| 기존 `postgresql`, `redis-server`, `mattermost`, `oaos-outline`, `nginx`, `certbot.timer` | Project 운영 구성 유지 | Company가 새 DB 엔진·프록시를 설치하지 않음 |
| `oaos-company.service` (`systemctl --user`) | 전용 비권한 운영 계정, `127.0.0.1:8765` | 콘솔·API·정책·감사·검색·비서 라우팅을 한 프로세스에서 제공. `Restart=on-failure`, `UMask=0077`, 기존 게이트웨이와 독립 재시작 |
| `oaos-company-backup.timer` + `.service` (`systemctl --user`) | 같은 계정, 외부 포트 없음 | 매일 DB 덤프와 상태 백업, 성공 여부 기록 |

nginx의 기존 TLS 설정에 Company 경로 `/company/`와 OAuth 콜백 경로만 추가한다. 외부 허용 포트는 기존 80/443뿐이고 DB·Redis·Company 포트는 loopback 전용이다. 설치기는 nginx 설정 검증 후 reload하고 인증서 만료·갱신 타이머를 read-back한다. 브라우저에는 HTTPS·Secure/HttpOnly/SameSite 세션 쿠키, CSRF 토큰, 관리자/구성원 권한 검사를 적용한다. 비서 API는 Project에서 확인한 발신자 ID를 서버 간 인증된 요청으로만 받는다. 프롬프트나 브라우저가 owner를 지정할 수 없다. 서비스용 `loginctl enable-linger`와 enable 상태를 확인한다.

조직 앱 자격과 암호화 마스터 키는 Company 전용 `.env`(소유 계정만 읽기, `0600`, 디렉터리 `0700`)에 둔다. 개인 위임 토큰은 PostgreSQL `secrets`의 AES-256-GCM 암호문과 owner 메타데이터로 이루어진 최소 Secret Vault에 둔다. 암호화의 추가 인증 데이터는 `org_id·owner_id·provider·purpose`이며 레코드마다 nonce를 새로 만든다. 키는 DB 밖 `.env`에 두고 버전별 회전 후 재암호화한다. `systemctl show`, 상태 JSON, 채팅, 감사, 백업 보고에는 원문을 싣지 않는다. 조직 앱 자격을 개인 토큰으로 재사용하지 않는다.

매일 DB 일관 덤프와 Company 상태를 접근 제한된 기존 백업 위치에 저장하고, 암호화 키의 별도 접근 제한 백업 존재를 확인한다. 설치·업그레이드 전 동일 백업을 하나 더 만든다. 복구는 E의 분리된 빈 스키마에 덤프와 키를 되돌려 비서 두 명의 권한·감사·검색을 확인한 뒤 서비스 재개한다. 실패 시 이전 앱 버전과 DB 백업을 복구하며 자동 역마이그레이션은 하지 않는다. 목표는 **RPO 24시간, RTO 4시간**이며 C17에서 실제 복구로 판정한다. 보존 초안은 일일 백업 7개와 주간 백업 4개다.

Project→Company 이전은 기존 Project 데이터의 제자리 재작성 없이 Project 사용자 ID를 `members`에 매핑하는 비파괴 방식이다. 기존 공용 위키는 출처 ACL이 확인된 문서만 색인하고, 개인 위키는 본인 선택·소유 확인 뒤 개인 namespace로 복사한다. `.env`와 자격은 복사하지 않고 게이트에서 다시 등록한다. 마이그레이션 전후 Project 검증 출력과 복구 지점을 남긴다. [아키텍처 v2.0 §5.2](architecture-v2.0.md)의 과거 Docker 배포 문구는 계획서 요구대로 **C01 구현 착수 전에 별도 문서 작업으로 무Docker·systemd 정합 수정**해야 한다. 이 설계 작업은 기존 파일을 수정하지 않는다.

## 4. 개인비서·정책·감사 (C04~C07)

등록 흐름은 `invited → active` 구성원의 Project ID 확인, 본인 인증, 비서·지식 namespace 생성, 개인 자격 등록 순서다. 각 비서 요청에서 `session.member_id = assistant.owner_id = secret.owner_id = external_account.member_id = output.owner_id`를 서버가 검사한다. 공급자는 요청에 허용된 자격만 가져가며 모델 입력·도구 응답·검색 스니펫·이력에 같은 owner가 적용된다. 타인의 비서·자격·출력 ID를 넣으면 존재 여부를 구별하지 않는 거부 응답을 낸다. 정지·퇴사·비서 해제는 세션과 큐를 막고 토큰을 철회·삭제하며 개인 색인을 격리한다. 감사 보존 대상 외의 개인 내용은 30일 내 삭제하는 목표를 둔다.

기본 정책은 미등록 사용자, 비활성 owner, 출처 ACL 불명, owner 불일치, 무인 실행, 미등록 도구·외부 전송을 **deny**한다. 본인 채팅·허용된 읽기 검색만 명시적 allow다. 메일 발송·문서 수정·외부 전송·관리자 정책 변경은 승인 필요다. 승인 주체는 같은 조직의 활성 관리자이며 자기 요청의 자기 승인은 금지한다. 요청은 action·resource·owner·정책 버전·context 해시로 묶고 **10분** 만료, 단일 사용, 최대 1회다. 정책이나 리소스가 변하면 새 승인을 요구한다. DB의 `consumed_at IS NULL AND expires_at > now()` 조건부 갱신과 실행 기록을 같은 트랜잭션으로 묶는다. 시간 초과·재사용·거부는 실행하지 않는다.

감사 이벤트는 등록/정지/퇴사, 매핑·자격 변경, 정책 변경, 승인 요청/판정/소비, 실행 허용/거부, 검색 접근/거부, 연동 해제, 백업/복구를 포함한다. 각 이벤트는 §2의 스키마에 `request_id`와 결과 코드로 서로 연결된다. 허용·거부 모두 기록 실패 시 위험 실행을 거부한다. 감사 행 UPDATE/DELETE는 앱 계정에 금지하고, 보존 목표는 **180일**이다. 전용 유지보수 role만 180일 초과 행을 주기적으로 삭제하고 파기 시각·건수를 새 감사 사건으로 남긴다. 관리자만 조직 범위의 필터·기간 조회가 가능하고 구성원은 자기 사건만 본다. 외부 반출은 관리자 실행과 감사 사건을 필요로 한다. 퇴사자의 이름·메일은 보존 사건에서 제거하거나 가명화하고 법률·조직 정책에 따른 보존 기간은 도입 전 재확인한다.

## 5. ACL 검색 (C08)

검색 순서는 **인증된 주체 확정 → 활성 owner 검사 → 원본 ACL 조회 → 허용 문서 ID 목록 구성 → 그 집합 안에서만 후보·스니펫 생성 → 반환 직전 재검사**다. 개인 공간은 owner 일치가 출처 ACL이다. Project 공용 문서와 선택 Notion 문서는 원본 API 또는 동기화된 ACL 버전이 확정될 때만 후보가 된다. 캐시가 만료되거나 원본이 응답하지 않으면 deny하고 공개 스니펫을 만들지 않는다. 벡터 검색을 p0에 추가하지 않고 기존 PostgreSQL 전문 검색을 사용한다.

원본 변경·공유 해제·삭제 이벤트는 해당 문서의 ACL 버전을 올리고 후보를 즉시 무효화한다. 이벤트 수신이 불가능한 소스는 요청 시 원본 권한을 재확인한다. 동기화 주기는 최대 5분 목표이며 누락 감지를 위해 주기적 재조회한다. 삭제 후 `deleted_at`을 먼저 찍어 검색을 즉시 차단하고 파생 chunk를 제거한다. 권한 회수 전파 목표는 **60초**, 삭제 반영은 **60초**다. 이 수치는 C08의 E 실측 및 선택 연동의 S 실측으로 판정한다. 원본 ACL을 얻을 수 없는 데이터는 색인해도 검색 불가다.

## 6. 관리 콘솔 (C09~C10)

콘솔은 기존 nginx 아래 한 화면의 네 탭으로 시작한다. 새 프런트엔드 서비스·상태 저장소는 만들지 않는다.

| 탭 | 관리자 최소 기능 | 구성원에게 보이는 범위 |
|---|---|---|
| 구성원 | Project ID 매핑, 초대·정지·퇴사, 비서 상태 | 본인 비서 등록·해제와 상태 |
| 연동 | 조직 앱 연결 상태, 공급자별 선택/SKIP, 철회 상태 | 본인 위임 시작·재동의·철회만 |
| 정책 | 기본 규칙 목록, 승인 대기·판정, 정책 버전 | 본인 요청·결과만 |
| 상태 | 서비스·백업·색인·감사 최근 결과, 실패 재개 링크 | 본인 연결·비서 상태만 |

처음 관리자 생성은 설치기가 만든 짧은 수명의 설정 페이지에서 한다. 입력된 `project_user_id`가 Project 관리자임을 Project API로 확인한 뒤 그 계정의 Project DM으로 10분짜리 일회 코드를 보내고, 브라우저에 코드를 입력받아 관리자 계정을 확정한다. 이후 로그인도 등록된 Project 계정에 보낸 일회 코드로 확인하고 세션은 8시간 후 만료한다. 코드는 해시만 저장하고 원자 소비하며, 계정당 10분 내 실패 5회면 15분 잠근다. URL에는 비밀이나 외부 토큰을 싣지 않으며 로그인·관리자 판정과 CSRF를 각각 검증한다. 관리자 화면에도 토큰은 `설정됨/없음/철회됨`만 표시한다.

## 7. 선택 연동의 최소 계약 (C11~C14)

이 절은 p0 구현 범위 밖인 옵션의 착수 조건을 고정한다. S는 조직이 이미 사용하는 검증 계정이며 실제 왕복이 없으면 해당 Phase는 미완료다. G11에서 스위트는 Google Workspace **또는** Microsoft 365 하나를 선택한다. G12에서는 Slack·Notion을 각각 선택하거나 SKIP한다. 조직 앱 설정은 관리자, 개인 위임 동의·철회는 구성원 본인이 한다.

| Phase·서비스 | 최소 권한·주체 | 삭제·권한 변화·해제 확인 |
|---|---|---|
| C11 Slack | 봇 메시지 수신·응답에 필요한 채널/DM 범위만. 요청의 서명·시각·중복 ID를 검증하고 workspace/user ID를 활성 `external_accounts`에 매핑. | 미등록·타인 발신자 차단, 중복 이벤트 단일 처리. 해제 시 토큰 철회·이벤트 수신 차단·연결 행 revoked; 두 사용자 응답 분리 실측. |
| C12 Notion | 관리자 통합 토큰은 명시 공유된 페이지의 읽기만. 공유 사실만으로 구성원 읽기 권한을 추정하지 않고 원본 ACL을 확인. | 공유 해제·권한 변경·원본 삭제 시 색인 즉시 차단·파생 chunk 제거. 해제 후 API 접근과 검색 모두 실패 확인. |
| C13 Google Workspace | 조직 OAuth 앱은 필요한 read-only 메일·문서·일정 범위만 요청. 각 구성원이 본인 계정으로 동의, `subject_id`와 owner 결속. 쓰기는 별도 정책·승인 없이는 제외. | 두 사용자 각자 읽기와 교차 접근 거부, 범위 변경 재동의, 연결 해제 시 공급자 토큰 철회·Vault 삭제·색인 차단. |
| C14 Microsoft 365 | 조직 Entra 앱 설정 후 위임된 최소 read-only 메일·파일·일정 범위. 애플리케이션 전체 사서함 권한 금지. 개인 로그인·동의는 본인. | tenant·subject·owner 검증, 두 사용자 분리, 만료·철회 후 요청 실패와 파생 검색 차단. |

공급자가 원본 ACL을 사용자별로 제공하지 않는 경우 그 콘텐츠는 구성원 검색에 넣지 않는다. 토큰·웹훅 원문은 로그·증거에 싣지 않는다. 선택 서비스의 승인·심사·계정 준비 기간은 하루 구현 시간 밖이며 서비스 정책이 요구하는 추가 범위는 별도 검토한다.

## 8. G10~G13 화면·상태 계약

모든 게이트는 **① 클릭 링크 → ② 필요한 이유 한 줄 → ③ 정확한 선택·입력 필드** 순서로 렌더링한다. 조직의 실제 URL은 설치 시 검증된 HTTPS 호스트에서 만들고, 외부 링크는 공식 안내 주소를 사용한다. 붙여넣은 값은 서버에서 즉시 검증하고 원문 재출력 없이 `.env`(600)에 저장한다. 개인 OAuth 토큰은 owner 결속 Vault에만 저장한다. 실패에는 이유 코드, 같은 단계의 재시도 링크, 이미 검증된 비밀을 제외한 미완료 필드를 보여준다. 파일 편집·명령 실행은 설치기가 맡는다.

| 게이트 | 화면의 링크 → 한 줄 문구 | 정확한 선택·입력과 read-back·재개 |
|---|---|---|
| G10 **제공·필수** | 설치기가 생성한 실제 `https://<조직 호스트>/company/setup`의 **관리 콘솔 열기** → “초기 관리자와 구성원별 비서 소유자를 연결해야 합니다.” | 관리자 계정 ID, Project DM으로 받은 일회 코드, 구성원별 **내부 사용자 ID ↔ Project 계정 ID**. Project API 존재·유일성·관리자 로그인·두 사용자 비서 소유자 매핑 확인. 실패는 G10 대기, 완료 항목 유지. SKIP 불가. |
| G11 **선택** | [Google OAuth 클라이언트 안내](https://developers.google.com/workspace/guides/create-credentials) 또는 [Microsoft Entra 앱 등록](https://learn.microsoft.com/en-us/entra/identity-platform/quickstart-register-app) 및 실행 시 실제 앱 설정 링크 → “기존 스위트의 메일·문서·일정을 구성원 본인 권한으로 연결합니다.” | `Google Workspace` 또는 `Microsoft 365` 또는 `SKIP`; 365만 tenant ID, OAuth client ID·client secret, 설치기가 보여준 정확한 redirect URI 등록 확인. 이후 각 구성원이 본인 동의. state·owner·범위·본인 read-back 실패는 G11에서 재동의. |
| G12 **선택** | [Slack 앱 만들기](https://api.slack.com/apps), [Notion 연동 만들기](https://www.notion.com/help/create-integrations-with-the-notion-api)와 실행 시 선택 앱 설정 링크 → “이미 쓰는 협업·문서 도구만 연결합니다.” | 서비스별 `선택/SKIP`; Slack은 bot token·signing secret·workspace ID·정확한 이벤트 URL 등록 확인, Notion은 integration token·workspace ID·공유 페이지 선택. 서명/접근 read-back 실패는 해당 서비스 설정 링크로 재개. |
| G13 **선택** | 선택 공급자의 공식 키 발급 링크(예: [OpenAI API 키](https://platform.openai.com/api-keys)) → “추가 모델을 쓸 때만 호출 경로와 비용 상한을 정합니다.” | `선택/SKIP`; 공급자 ID·모델 ID·API 키·승인된 정액 플랜/월 상한. 실제 모델 응답·소유자 분리·실패 대체·한도 read-back 후 완료. 사용량 과금만 가능한 경로는 **승인 필요**, 승인 전 보류. |

선택하지 않은 게이트/서비스는 상태 `SKIP(reason=not_selected)`로 기록하며 자격·웹훅·서비스 호출이 **없음**을 read-back해야 한다. 선택했으나 접근권·동의·왕복이 없으면 SKIP이 아니라 `BLOCKED`다. G11의 공급자 한쪽 선택은 다른 쪽의 명시적 SKIP이고 G12의 두 서비스는 독립 상태다. G13 SKIP이면 Project 기본 모델만 사용한다. G13 선택 모델이 실패하면 기본 모델로 대체하되 동일 owner 문맥과 감사 사건을 유지한다. 비용 상한 확인이 불가능하면 선택 호출을 중단한다.

## 9. 설치·검증·증거 계약

아래 명령은 **향후 구현할 계약**이며 현재 Company 설치기·검증기가 존재한다는 뜻이 아니다. [Personal 설치기](../editions/personal/install.sh), [Project 설치기](../editions/project/install.sh)의 `--stage`, `--status`, 멱등 체크포인트와 [Project 검증기](../bootstrap/verify/project-verify.sh)의 항목별 결과 형식을 승계한다. `--dry-run`은 계획 출력만 하며 완료 증거가 아니다.

```text
bash editions/company/install.sh --stage cNN
bash editions/company/install.sh --status
bash bootstrap/verify/company-verify.sh --phase cNN --read-back
```

`--stage`는 `c01`~`c17` 중 하나를 받아 명시 의존성을 확인하고 실제 변경을 수행한다. 재실행은 이미 적용된 리소스를 조회한 뒤 부족한 것만 보충한다. 설치기 종료 코드 `0=선택한 설치 단계 모두 적용(applied; 검증 완료 아님)`, `3=게이트·선행 조건 차단`, `1=오류`다. 검증기 종료 코드 `0=필수 read-back·수동 확인 완료`, `3=필수 확인 대기`, `1=검사 실패`다. `--status`는 phase별 `pending/applied/verified/blocked/failed/SKIP`과 검증 시각을 읽기 전용으로 출력한다. Company 구현 시 공통 `stage_mark`의 허용 상태에 `applied/verified/SKIP`을 추가하고 단계 이름 검증에 `cNN`을 허용한다. `stage_done`은 Personal·Project의 `done`과 Company의 `verified`를 완료로 해석하며 선택 옵션의 `SKIP`은 별도 종료 상태로 기록한다. 기존 `done`을 Company에 기록하지 않고 실제 read-back PASS 후에만 `verified`로 승격한다. `applied`는 설치 성공일 뿐 완료가 아니다. 검증기는 `--phase cNN --read-back`으로 E의 DB·서비스·API를 직접 읽고 선택 S의 실서비스 왕복을 확인한다. read-back 실패나 필수 MANUAL 미완료는 `verified` 금지다. 비밀 원문은 명령 인수·표준 출력·로그·상태에 넣지 않는다.

| Phase | 반드시 남길 실제 read-back 항목 |
|---|---|
| C01 | E의 격리 DB에 조직 A(관리자·스키마 테스트 owner)와 조직 B(테스트 owner)의 임시 픽스처를 만들고 스키마 버전·FK/UNIQUE/owner 불변식, 타 조직·타 owner 조회 거부를 확인한 뒤 픽스처 제거 |
| C02 | Company 유닛 enabled/active, loopback 포트·HTTPS, 재실행과 안전한 재부팅 뒤 활성 상태 |
| C03 | 관리자·두 구성원 매핑 조회, 중복·미등록·정지·퇴사 거부 |
| C04 | 두 비서와 별도 namespace, 교차 출력·지식 거부, 해제 후 실행 거부 |
| C05 | `.env` 600·Vault 암호문, 자기 자격 사용·타인 거부·회전·철회 후 실패 |
| C06 | 기본 거부, 승인 후 1회 허용, 만료·재사용·자기 승인 거부 |
| C07 | 허용·거부·철회 사건 조회와 원문 비밀 부재, 관리자/구성원 조회 경계 |
| C08 | 실제 색인 후 허용·타인 검색, 원본 ACL 변경·삭제·원본 장애 시 거부 |
| C09 | 네 탭 관리자 화면, 구성원 화면 제한, 비밀 마스킹과 서비스 상태 |
| C10 | 실제 HTTPS G10 링크, 관리자 로그인, 두 매핑 저장, 실패 중단 후 재개 |
| C11~C14 | 선택한 S의 실제 이벤트/읽기·두 owner 분리·철회/삭제·해제; 미선택은 SKIP 무자격 증거 |
| C15 | G11·G12 선택별 입력 검증·공식 링크·재개, 선택 서비스 왕복 또는 명시 SKIP |
| C16 | G13 선택 모델·실패 대체·비용 한도 실측 또는 기본 모델 단독 SKIP |
| C17 | 전 Phase 상태, 재부팅, 백업 복구, 선택 S 왕복, 성능 실측과 운영 인계 |

각 Phase의 완료 증거는 E/S 식별자(가명), UTC 시각, 설치·확인 명령, 종료 코드, 자동 검사 `PASS/FAIL/MANUAL/SKIP`과 항목별 기대/관측값, 수동 확인 담당 역할과 근거, 설치 상태 read-back, 실패·재시도·원복 결과를 한 JSON 문서에 담는다. 예시는 값의 **형식**만 보여준다.

```json
{"phase":"c04","environment":"E","service":"none","started_at":"YYYY-MM-DDTHH:MM:SSZ","install":{"command":"bash editions/company/install.sh --stage c04","exit_code":0,"state":"applied"},"checks":[{"name":"two_owner_isolation","status":"PASS","expected":"foreign access denied","observed":"denied","evidence_ref":"redacted-response-id"}],"manual":[],"rollback":{"performed":false,"result":"not_required"},"final_state":"verified"}
```

증거 참조는 비식별 요청 ID만 사용하고 URL 쿼리·토큰·개인 이름·문서 본문을 저장하지 않는다. 실제 확인 명령의 표준 출력은 비밀 제거 후 원본 파일에 보관하고 JSON에 파일명·체크섬을 연결한다. `MANUAL`은 담당자의 실제 확인 시각·방법·결과가 있어야 PASS에 합산한다. 자동 검사 실패 또는 필수 수동 확인 미완료는 `미완료/확인 필요`다. S가 필요한 선택 Phase에서 S 접근권이 없으면 미완료이고 단위 테스트 통과나 dry-run을 대신 제출하지 않는다.

성능 목표 초안은 E에서 활성 구성원 **5/25/50명**을 차례로 시험해 각 부하 구간의 인증+ACL 검색 로컬 처리 p95 **0.75/1.0/1.5초 이하**, 큐 대기 p95 **10초 이하**, 큐 상한 **100건**을 확인하는 것이다. 외부 LLM 응답은 공급자 지연과 분리해 기록하고 통제된 공급자 응답에서 채팅 전체 p95 **30초 이하**를 목표로 한다. 오류율 목표는 부하 구간별 **1% 미만**, 교차 owner 유출은 **0건**이다. 백업 RPO 24시간·복구 RTO 4시간, 재부팅 후 서비스·로그인 복귀 **10분 이내**를 C17에서 실측한다. 이 수치는 목표이지 현재 성능 주장이나 통과 증거가 아니다.

## 10. 착수 순서와 남은 확인

| 개발일 | 착수 작업 | 그날 완료 정의 |
|---|---|---|
| 선행 | E 자원·권한·백업·원복 확인, 아키텍처의 Docker 문구 정합 수정 | Project 검증 결과, 별도 문서 정합, E 분리 증거 |
| 1~3 | C01 → C02 → C03 | 각 일의 설치 0, 위 표 read-back PASS, 체크포인트 재실행 확인 |
| 4~5 | C04 → C05 | 두 구성원 비서·자격 격리와 철회 실측 |
| 6~8 | C06 → C07 → C08 | 승인 재사용 거부, 감사 누락 없음, ACL 변경·삭제 반영 실측 |
| 9~10 | C09 → C10 | 최소 화면 권한과 G10 중단 재개·두 사용자 매핑 실측; p0 증거 묶음 작성 |
| 후속 | C11~C14 → C15 → C16 → C17 | 선택 S별 실연동·SKIP, 기본 모델/선택 모델, 최종 복구·성능 실측 후 P2 판정 |

구현 중에는 변경 범위의 대상 테스트와 하위 시스템 회귀를 먼저 실행한다. 이 문서 작성 단계에는 구현·실설치가 없으므로 C Phase의 `verified` 상태를 만들 수 없다. P2 통합 판정은 계획서의 실제 설치·read-back과 [아키텍처 로드맵](architecture-v2.0.md)의 전체 회귀·런타임 확인을 별도로 요구한다.

**미해결·확인 필요:** E의 CPU·메모리·포트·TLS 경로와 무중단 분리 가능 여부; Project 계정 확인 API와 관리 콘솔의 일회 링크 전달 경로; 조직별 감사 180일·개인정보 30일 삭제의 법률/내부 정책 적합성; 백업 키의 별도 보관 위치·복구 담당 역할; 원본별 ACL API 제공 범위와 60초 전파 가능성; S의 실제 계정·OAuth 심사·관리자 동의·Slack/Notion 권한 변경 이벤트; 5/25/50명 부하의 공급자 한도; G13 정액 플랜·월 상한; 재사용 후보의 라이선스·의존성·공개 적합성. 이 항목이 필요한 Phase는 답과 실측이 나오기 전까지 미완료로 기록한다.
