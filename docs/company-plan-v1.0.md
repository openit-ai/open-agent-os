# OAOS Company(P2) 제품화 계획 v1.0

> 상태: 계획. 이 문서의 Phase와 게이트는 구현·실설치 완료 증거가 아니다. 제품 방향의 정본은 [아키텍처 v2.0](architecture-v2.0.md), 현재 공개 안내는 [Company README](../editions/company/README.md)다.

## 1. 목표와 완료 상태

Company는 5~50인 중소기업을 위한 **Project + 거버넌스 레이어**다. [Project](../editions/project/README.md)의 팀 채팅·위키·메일·서버 설치 흐름 위에 구성원별 개인비서, 개인 자격의 안전한 연동, 정책·승인·감사·권한 인식 검색·관리 콘솔을 얹는다. [Personal](../editions/personal/README.md) → Project → Company의 적층 경로와 저장소 URL에서 시작하는 자동 설치 경험을 유지한다.

**P2 완료 판정(계획):** 이미 확보된 Project 호스트의 분리된 검증 인스턴스에서 Project 선행 조건을 충족한 뒤 Company 설치기가 Docker 없이 systemd 서비스로 재실행·재개 가능하게 설치하고, G10을 거쳐 구성원 두 명 이상을 각각 별도 개인비서·자격 범위로 등록한다. 각 Phase도 실제 설치와 read-back 출력이 있어야 완료된다. 정책 거부·승인, 교차 사용자 자격 및 검색 결과 차단, 감사 기록, 백업·복구와 재부팅 후 서비스를 실제 명령·응답으로 확인한다. 선택한 G11·G12·G13 연동은 각 서비스의 실제 왕복과 해제까지 별도 확인하며, 선택하지 않은 서비스는 `SKIP`으로 기록한다. 자동 검증 실패 또는 필수 수동 확인 미완료는 완료로 보고하지 않는다. 5~50인 규모의 성능·운영 판정은 부하·복구 실측 후에만 확정한다.

**범위 경계:** Google Workspace와 Microsoft 365는 조직이 이미 사용하는 것 중 하나를 선택하는 옵션이다. Slack·Notion도 기존 사용 조직에서만 켠다. 이 계획은 외부 SaaS를 재구축하거나 새 구독을 요구하지 않는다. 기본 배포는 **무Docker·systemd**이며, 서버와 LLM 정액 이외의 Company 신규 구독을 전제하지 않는다. 도메인과 Project 기반 운영은 선행 조건으로 승계한다. 신규 서버 프로비저닝이나 신규 비용이 필요한 검증 환경·서비스는 **승인 필요**이며 임의로 진행하지 않는다. 추가 모델의 사용량 과금 가능성은 G13 도입 전 확인 필요이며, 정액 원칙에 맞지 않으면 기본 제품 범위에서 활성화하지 않는다.

## 2. 현행 자산 실측과 흡수 원칙

2026-09-27 작업 트리에서 `ls`, `wc -l`, `rg --files`로 확인한 **파일 존재·규모**다. 파일 수는 `rg --files`의 열거 결과이며 기능 동작 수나 품질 점수가 아니다. 별도 플랫폼 자산은 읽기 전용 참고이며, 이 계획의 제품 구조·배포·완료 기준을 결정하는 정본으로 삼지 않는다.

| 구분 | 실측 근거와 수치 | 제품화 해석 |
|---|---|---|
| 공개 설치기 | [Personal 설치기](../editions/personal/install.sh) 320줄, [Project 설치기](../editions/project/install.sh) 533줄; `editions/personal` 파일 2개, `editions/project` 파일 8개 | 단계·체크포인트·재개 방식과 systemd 운영 경로를 승계할 후보. Company용 설치기는 아직 없음 |
| 공개 검증·회귀 | [Personal 검증기](../bootstrap/verify/personal-verify.sh) 170줄, [Project 검증기](../bootstrap/verify/project-verify.sh) 242줄; `bootstrap/verify` 파일 2개, `bootstrap/tests` 파일 2개 | Company 검증 항목을 추가할 기반. 공개 Company 검증기는 아직 없음 |
| 공개 오케스트레이션 | [부트스트랩 스킬](../skills/oaos-bootstrap/SKILL.md) 195줄, [게이트 레퍼런스](../skills/oaos-bootstrap/references/gates.md) 89줄; `skills/oaos-bootstrap` 파일 3개 | G10~G13 안내·실패 재개 계약을 붙일 기반 |
| 공개 Company | `ls editions/company`에서 [README](../editions/company/README.md) 1개(73줄)만 확인 | Company 설치·거버넌스·연동 코드는 공개 트리에 아직 없음 |
| 별도 플랫폼: 연동 | Slack·Notion·Google·Microsoft·Mattermost·IAM 어댑터 묶음은 각각 2개 파일(안내 1, 구현 1); `wc -l`로 확인한 Slack·Notion·Google·Microsoft·IAM 구현 파일은 순서대로 191·144·830·195·892줄 | 계약·실패 처리·권한 경계를 검토해 필요한 최소 부분만 선택 흡수. 외부 서비스 실연동 완료를 뜻하지 않음 |
| 별도 플랫폼: 거버넌스 | 제어 계층 43개, 실행 게이트 27개, 지식 인덱스 21개, 메모리 3개, 관리 콘솔 208개, 보안 묶음 35개, 개인 위키 묶음 8개 파일 | 개인비서 매핑, 정책·승인·감사·Vault, ACL 검색, 관리 UI의 재사용 후보. 제품에 통째로 이식하지 않음 |

별도 플랫폼의 상태 문서에는 로컬 테스트와 일부 운영 리드백이 기록되지만, Slack·Notion 및 Google Workspace·Microsoft 365의 **다중 사용자 실제 OAuth·권한·회수 왕복**은 완료 증거로 취급하지 않는다. 문서에 있는 과거 테스트 건수나 운영 수치는 이 P2 릴리스의 증거가 아니다. 어댑터·콘솔의 라이선스, 의존성, 공개 적합성도 흡수 전에 확인 필요다.

공개 트리의 재현 출력 요약: `ls editions/company` → `README.md`만 1개; `rg --files bootstrap/verify` → 2개; `rg --files bootstrap/tests` → 2개; `wc -l editions/company/README.md` → 73줄. 별도 플랫폼에서 동일한 `rg --files <구성요소> | wc -l`와 `wc -l <구현 파일>`로 위 표의 수치를 확인했다. 수치는 향후 파일 이동·추가에 따라 달라질 수 있다.

## 3. 제품 갭과 수용 기준

| 갭 | 필요한 최소 제품 동작 | 확인할 실패·경계 |
|---|---|---|
| Slack 연동 제품화 | 기존 Slack 조직이 G12에서 앱을 연결하고, 이벤트의 발신자를 등록된 구성원에게 매핑해 개인비서로 전달 | 서명 불일치·중복 이벤트 거부, 미등록 사용자 차단, 다른 구성원 출력·자격 유출 차단, 토큰 해제 |
| Notion 연동 제품화 | 기존 Notion 조직이 G12에서 연결·공유한 페이지를 원본으로 읽고, 허용된 사용자만 검색 | 공유 해제·삭제·권한 변경 시 파생 인덱스 무효화, 출처 ACL을 검색 후보 생성 전에 적용 |
| 구성원별 개인비서 등록 | G10 관리자 매핑 후 각 구성원이 본인 비서를 등록·해제하고 개인 지식과 실행 이력을 분리 | `owner == credential == provider == output`, 퇴사·정지 시 실행·토큰·검색 즉시 차단 |
| Google Workspace / Microsoft 365 보안 등록 | G11에서 조직 앱 설정 후 **구성원 본인이** 위임 OAuth로 본인 계정을 연결; 메일·문서·일정은 승인된 범위만 | 관리자 공용 자격으로 개인 데이터 대리 접근 금지, 토큰 범위·소유자 결속, 재동의·회수·만료 검증 |
| 정책·감사·관리 콘솔 | 기본 거부, 위험 실행의 JIT 승인, 변경·실행 감사, 관리자 최소 화면(구성원·연동·정책·상태) | 승인 우회·감사 누락·관리자 권한 오용 거부, 민감 값 비노출, 백업·복구 |
| 선택형 멀티 LLM | G13이 켜진 경우 작업별 모델 선택·실패 대체·예산 경계를 운영자가 확인 | 개인 컨텍스트 격리, 사용량/정액 조건 확인, 기본 모델만 쓰는 설치도 통과 |

개인 자격과 조직 앱 설정을 구분한다. 조직 관리자가 OAuth 클라이언트를 등록해도 개인 계정의 위임은 해당 구성원이 직접 수행한다. 원본 문서의 권한이 검색 인덱스보다 우선하며, 권한을 확정할 수 없으면 검색·실행을 거부한다.

## 4. 구현 Phase — 하루 단위 수직 절편

각 Phase의 예상 소요는 **선행 계약·검증 환경이 준비된 뒤 구현·대상 테스트·실설치·read-back까지 1 개발일 이내**다. 외부 조직 승인, OAuth 심사, 실제 서비스 계정 준비, 성능·복구 관찰 대기 시간은 포함하지 않는다. 하루 안에 설치 출력과 확인 명령 결과가 나오지 않으면 해당 Phase를 미완료로 남기고 원인을 기록한다. **dry-run·문서·단위 테스트만으로 완료 판정하지 않는다.** 아래 산출물과 명령은 향후 구현할 계획이며 현재 제공되는 Company 명령으로 오인하지 않는다.

실설치 대상 환경의 `E`는 **이미 확보된 Project 호스트의 분리된 Company 검증 인스턴스**다. 실행 전 여유 자원·권한·백업·원복 경로를 확인하고 기존 Project 서비스·데이터와 설정을 분리한다. `S`는 E에서만 사용하는 기존 조직의 Slack·Notion·Workspace·365 검증 계정이다. `S` 접근권이 없으면 해당 선택 연동 Phase는 미완료로 남긴다. E의 분리가 불가능하여 새 서버나 새 구독이 필요하면 **승인 필요**로 표시하고 승인 전에는 진행하지 않는다. 아래 `install.sh --stage cNN`과 `company-verify.sh --phase cNN --read-back`은 상세설계에서 구현할 **계획 명령 계약**이다. 이 명령은 실제 설치를 수행하고 설치 상태·서비스·API·DB의 관측값을 반환해야 하며, Phase별 추가 확인 명령도 실행한다. 비밀 값은 명령행 인수·출력에 넣지 않는다.

| Phase | 목표 / 산출물 | 실설치 대상 환경 | 실설치 절차·확인 명령과 필수 read-back 게이트 | 선행 의존성 | 예상 소요 |
|---|---|---|---|---|---|
| C01 | Project→Company 계약; owner 필수 스키마·설치 가능한 마이그레이션 | E | `bash editions/company/install.sh --stage c01` → `bash bootstrap/verify/company-verify.sh --phase c01 --read-back`; 실제 스키마 버전·owner 제약 조회, 두 사용자 교차 접근 거부 출력 | Project 설치·검증 계약 | 1일 |
| C02 | Docker 없는 배포 골격; 단계·체크포인트·systemd 단위 | E | `bash editions/company/install.sh --stage c02` → `systemctl --user is-active oaos-company` → `bash bootstrap/verify/company-verify.sh --phase c02 --read-back`; 재실행·재부팅 후 활성 상태 출력 | C01 | 1일 |
| C03 | 구성원 식별·매핑; 관리자·외부 계정 매핑 | E | `bash editions/company/install.sh --stage c03` → `bash bootstrap/verify/company-verify.sh --phase c03 --read-back`; 실제 매핑 조회와 중복·미등록·퇴사 계정 거부 응답 | C01·C02 | 1일 |
| C04 | 개인비서 등록; 구성원별 비서·지식 공간 생성·해제 | E | `bash editions/company/install.sh --stage c04` → `bash bootstrap/verify/company-verify.sh --phase c04 --read-back`; 두 사용자 비서 목록·지식/출력 격리 응답 | C03 | 1일 |
| C05 | 개인 자격 보관; owner 결속·회전·철회 | E | `bash editions/company/install.sh --stage c05` → `bash bootstrap/verify/company-verify.sh --phase c05 --read-back`; 비밀 원문 없이 보관 권한·타인 조회 거부·철회 후 실패 출력 | C04 | 1일 |
| C06 | 정책·JIT 승인; 최소 정책 묶음·일회 승인 | E | `bash editions/company/install.sh --stage c06` → `bash bootstrap/verify/company-verify.sh --phase c06 --read-back`; 실제 실행 요청의 기본 거부·승인 후 허용·만료/재사용 거부 응답 | C03·C05 | 1일 |
| C07 | 감사; 등록·승인·실행·철회 이벤트 조회 | E | `bash editions/company/install.sh --stage c07` → `bash bootstrap/verify/company-verify.sh --phase c07 --read-back`; 성공·거부 사건의 감사 조회 및 비밀 원문 부재 출력 | C06 | 1일 |
| C08 | ACL 검색; 원본과 개인 공간의 권한 선검사 | E | `bash editions/company/install.sh --stage c08` → `bash bootstrap/verify/company-verify.sh --phase c08 --read-back`; 실제 색인 후 허용 검색·타인 검색 거부·권한 변경/삭제 반영 응답 | C04·C06 | 1일 |
| C09 | 최소 관리 콘솔; 구성원·정책·감사 상태 화면 | E | `bash editions/company/install.sh --stage c09` → `systemctl --user is-active oaos-company` → `bash bootstrap/verify/company-verify.sh --phase c09 --read-back`; 관리자/일반 사용자 화면 권한·비밀 마스킹 응답 | C03·C07 | 1일 |
| C10 | G10 설치 연결; 관리자 초기 설정·재개 | E | `bash editions/company/install.sh --stage c10` → 실제 콘솔에서 G10 입력 → `bash bootstrap/verify/company-verify.sh --phase c10 --read-back`; 로그인·매핑 저장·중단 재개 상태 출력 | C02·C09 | 1일 |
| C11 | Slack 선택 연동; 이벤트 수신·응답·해제 | E+S(Slack) | `bash editions/company/install.sh --stage c11` → 기존 조직의 테스트 이벤트 송신 → `bash bootstrap/verify/company-verify.sh --phase c11 --read-back`; 실제 응답·서명 거부·owner 라우팅·해제 출력 | C04·C06·C07 | 1일 |
| C12 | Notion 선택 연동; 공유 원본 동기화·삭제·해제 | E+S(Notion) | `bash editions/company/install.sh --stage c12` → 기존 조직의 공유 페이지 변경 → `bash bootstrap/verify/company-verify.sh --phase c12 --read-back`; 실제 원본/색인·ACL 변경·삭제 반영 출력 | C08 | 1일 |
| C13 | Google Workspace 선택 연동; 개인 위임·철회 | E+S(Google Workspace) | `bash editions/company/install.sh --stage c13` → 두 사용자가 본인 동의 → `bash bootstrap/verify/company-verify.sh --phase c13 --read-back`; 각자 읽기·타인 차단·토큰 철회 후 실패 출력 | C05·C06 | 1일 |
| C14 | Microsoft 365 선택 연동; 개인 위임·철회 | E+S(Microsoft 365) | `bash editions/company/install.sh --stage c14` → 두 사용자가 본인 동의 → `bash bootstrap/verify/company-verify.sh --phase c14 --read-back`; 각자 읽기·타인 차단·토큰 철회 후 실패 출력 | C05·C06 | 1일 |
| C15 | G11·G12 설치 연결; 공식 링크·값 검증·옵션 SKIP | E+선택한 S | `bash editions/company/install.sh --stage c15` → 실제 선택 게이트 완료 또는 미사용 선택 → `bash bootstrap/verify/company-verify.sh --phase c15 --read-back`; 잘못된 값 재요청·선택 서비스 왕복·미선택 무자격 상태 출력 | C10·C11~C14 | 1일 |
| C16 | G13과 선택 라우팅; 승인된 정액 모델·실패 대체 | E+기존 LLM 플랜 | `bash editions/company/install.sh --stage c16` → `bash bootstrap/verify/company-verify.sh --phase c16 --read-back`; 실제 기본/선택 모델 응답·실패 대체·owner·비용 설정 출력 | C05·C07 | 1일 |
| C17 | Company 최종 검증·운영 인계; 백업·복구·회수 절차 | E+선택한 S | `bash editions/company/install.sh --stage c17` → `bash bootstrap/verify/company-verify.sh --phase c17 --read-back`; 설치 상태·systemd·백업 복구·재부팅 생존·외부 왕복의 실제 출력 수집 | C10·C15·C16 | 1일 |

Phase별 대상 테스트와 하위 시스템 회귀를 먼저 돌리고, P2 통합 판정에서는 [아키텍처 v2.0 로드맵](architecture-v2.0.md)의 전체 회귀·런타임 리드백 요구를 별도 수행한다. 설치·유지보수는 Project의 apt/시스템 패키지, 단일 서버와 systemd 운영 패턴을 우선 재사용한다. 별도 플랫폼의 Docker·Kubernetes 배포 경로는 흡수하지 않는다. 아키텍처 v2.0 §5.2의 과거 배포 예시 중 Docker 호출 문구는 이번 Company의 **무Docker·systemd** 결정과 다르며, 상세설계 때 정본 문구를 정리해야 한다.

모든 Phase의 완료 기록에는 **대상 환경(E/S), 설치 명령의 종료 코드·상태 출력, 추가 확인 명령의 응답, 실행 시각, 원복 결과**를 남긴다. S가 필요한 Phase에서 실제 서비스 접근이 준비되지 않으면 단위 테스트가 통과해도 `미완료/확인 필요`다. 이미 운영 중인 Project 호스트를 쓰더라도 기존 서비스 중단·데이터 변경이 필요한 경우에는 해당 작업의 영향과 원복을 먼저 검토한다. 새 서버·추가 구독·새 비용이 발생하는 대안은 **승인 필요**다.

## 5. 설치·개입 게이트 G10~G13

[아키텍처 v2.0 §2.3](architecture-v2.0.md)의 순서인 **① 클릭 가능한 링크 → ② 필요한 이유 한 줄 → ③ 붙여넣을 값의 정확한 지정**을 화면·채팅에 그대로 적용한다. 모든 링크는 실행 시 실제 조직의 HTTPS 주소 또는 아래 공식 콘솔 주소로 제공한다. 제공 값은 즉시 검증하고 조직 앱·API 키는 `.env`(600)에만 보관하며 채팅·로그·문서에는 재출력하지 않는다. 개인 OAuth 토큰은 owner 결속 Secret Vault에 저장하는 상세설계가 필요하다. 실패 시 실패 이유와 같은 재시도 링크를 제공한다. 게이트는 선택 또는 제공뿐이고 파일 편집·명령 실행은 설치기가 맡는다.

| 게이트 | ① 클릭 링크 | ② 왜 필요한지(표시할 한 줄) | ③ 정확히 받을 값·행동 | 검증·재개 |
|---|---|---|---|---|
| **G10 필수** | 설치기가 생성한 **실제 관리 콘솔 HTTPS 주소**를 ‘관리 콘솔 열기’ 클릭 링크로 렌더링 | “초기 관리자와 구성원별 비서 소유자를 연결해야 합니다.” | 브라우저에서 관리자 계정 생성 후 **관리자 계정 ID**, 구성원별 **내부 사용자 ID ↔ Project 계정 ID**를 입력·확정 | 관리자 로그인과 각 매핑의 유일성 확인; 미완료면 G10에서 재개 |
| **G11 선택** | [Google OAuth 클라이언트 안내](https://developers.google.com/workspace/guides/create-credentials) 또는 [Microsoft Entra 앱 등록](https://learn.microsoft.com/en-us/entra/identity-platform/quickstart-register-app); 실행 시 해당 관리 콘솔 직접 링크와 OAOS 콜백 URI도 제시 | “기존 스위트의 메일·문서·일정을 구성원 본인 권한으로 연결합니다.” | **Google Workspace 또는 Microsoft 365 선택**, 조직 앱의 **tenant ID(365만)**·**OAuth client ID**·**client secret**, 설치기가 보여준 **redirect URI 등록 확인**; 이후 각 구성원은 본인 로그인·동의 | OAuth 콜백의 state·owner·범위 및 본인 read-back 확인; 미사용은 SKIP, 실패 시 재동의 |
| **G12 선택** | [Slack 앱 만들기](https://api.slack.com/apps) 또는 [Notion 연동 만들기](https://www.notion.com/help/create-integrations-with-the-notion-api); 실행 시 선택한 서비스의 실제 앱 설정 링크 제시 | “이미 쓰는 협업·문서 도구만 연결합니다.” | 서비스별 선택: Slack은 **bot token·signing secret·연결 workspace ID**와 이벤트 URL 등록 확인, Notion은 **integration token·연결 workspace ID** 및 공유할 페이지 선택 | Slack 서명·인증 확인/Notion 접근 가능한 페이지 read-back; 미사용은 SKIP, 실패 시 앱 설정 링크 재제공 |
| **G13 선택** | 선택 모델 공급자의 **공식 키 발급 링크**를 설치기가 제공(예: [OpenAI API 키](https://platform.openai.com/api-keys)) | “추가 모델을 쓸 때만 호출 경로와 비용 상한을 정합니다.” | **공급자 ID·모델 ID·API 키·승인된 정액 플랜/월 상한**을 정확히 요청 | 키 유효성·모델 접근·비용 조건 확인; 미사용은 SKIP하고 기본 모델 유지 |

G11의 `redirect URI`는 설치기가 콘솔에 복사할 **정확한 실제 값**으로 생성한다. G12의 Slack 이벤트 URL도 같은 방식으로 제공한다. G10 링크 표기는 문서상 형식 예시이며 실제 주소는 설치 후에만 알 수 있다. G13의 키가 사용량 과금만 가능하면 비용 원칙에 따라 옵션을 보류하고 “확인 필요”를 남긴다. 각 구성원의 개인 OAuth 동의·철회는 G11 아래의 본인 등록 흐름이며 관리자에게 개인 토큰을 붙여넣게 하지 않는다.

## 6. 라이선스·공개 경계

Company 고유 코드·설정·문서는 [LICENSE-COMPANY](../LICENSE-COMPANY)의 **BSL 1.1** 경계로 게시하고, Personal·Project의 [Apache 2.0](../LICENSE) 자산과 파일·라이선스 표시를 구분한다. 공개는 소스 열람·평가·개발·테스트와 라이선스가 허용한 비상업적 프로덕션 사용을 뜻한다. 그 범위를 넘는 프로덕션 사용, 제3자 호스팅·관리 서비스 제공, 상업적 재배포에는 별도 상업 라이선스가 필요하다. 현재 Change Date **2030-08-27은 버전 0.1.1에 적용**되고 이후 버전은 각 버전의 Change Date를 별도 명시하며 최초 제공 후 최소 4년보다 빠를 수 없다. 해당 날짜 이후 해당 버전은 Apache License 2.0으로 전환된다.

재사용 후보의 제3자 의존성·저작권·배포 권한은 흡수 전 확인 필요다. 공개 문서·테스트·예제에는 실제 자격, 조직 주소, 개인 환경 경로, 운영 데이터, 별도 플랫폼의 내부 이력과 식별 정보를 싣지 않는다.

## 7. 위험과 미검증 항목

| 항목 | 현재 판정 | 다음 확인 |
|---|---|---|
| 5~50인 단일 서버 성능·장애 복구 | 확인 필요 | 동시 사용자 부하, 큐 적체, 백업 복구·재부팅 실측 |
| 확보된 Project 호스트의 격리 검증 수용 능력 | 확인 필요 | 자원·권한·백업·원복 확인; 새 서버가 필요하면 승인 필요 |
| Slack·Notion 실서비스 이벤트·권한 변경 | 확인 필요 | 실제 조직에서 설치·해제·권한 변경 왕복 |
| Google Workspace·Microsoft 365 두 사용자 위임 | 확인 필요 | 실제 조직/테넌트의 동의 정책, 만료·철회, 교차 사용자 차단 |
| 외부 OAuth 심사·관리자 권한·앱 배포 정책 | 확인 필요 | 공식 절차와 고객 조직 정책 확인; 대기 기간은 1일 Phase 산정 밖 |
| 기존 플랫폼 코드 공개·이식 적합성 | 확인 필요 | 라이선스·의존성·테스트·런타임 경계별 검토 |
| 모델 공급자 정액·월 상한 | 확인 필요 | G13 공급자별 실제 요금·한도 확인 후 활성화 |
| 운영 보안·감사 보존 기간·삭제 의무 | 확인 필요 | 고객 요구와 법률 검토를 상세설계에서 확정 |
| 아키텍처 문서의 과거 Docker 배포 문구 | 확인 필요 | Company systemd 단일 경로로 정본·README 문구 정합성 정리 |

## 8. 다음 단계 — 6단계 Company p0 상세설계에서 확정

1. C01 계약: 조직·구성원·개인비서·외부 계정 ID, 등록·정지·퇴사 상태와 owner 불변식.
2. 단일 서버·systemd 단위, 권한 사용자, 포트·TLS·비밀 저장·백업·복구·업그레이드 경로와 Project 이전 방식.
3. 정책 기본 묶음, JIT 승인 주체·만료·재사용 방지, 감사 이벤트 스키마·보존·조회 권한.
4. Slack 이벤트와 Notion 페이지 동기화의 최소 권한·삭제·ACL 변경·해제 계약.
5. Google Workspace/Microsoft 365의 조직 앱 설정과 개인 OAuth 동의 범위, 콜백·토큰 회수·관리자 동의 경계.
6. G10~G13 화면 문구·실제 링크 생성·정확한 입력 필드·실패 재개, 선택 옵션의 SKIP 판정.
7. 선택 모델의 정액·예산 조건, 호출·실패 대체·감사 및 기본 모델 단독 운영 기준.
8. Company BSL 파일 경계와 재사용 코드 공개 적합성, E/S 실설치 환경·명령 계약·read-back 증거 형식; 새 서버·신규 비용은 승인 필요로 분리.
