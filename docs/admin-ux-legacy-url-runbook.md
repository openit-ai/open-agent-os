# Admin Console legacy URL 관찰·정리 runbook

## 목적과 원칙

이 문서는 Admin Console Phase 6 이후 legacy URL의 접근량을 관찰하고 정리 후보를 판정하는 운영 절차다. 프론트엔드가 별도 계측 API를 호출하지 않으며, 판정 근거는 `/var/log/nginx`의 실제 요청 로그로 한정한다.

- legacy route 파일과 redirect는 자동으로 삭제하지 않는다.
- query string은 canonical URL까지 보존한다.
- URL fragment는 HTTP 요청에 포함되지 않으므로 nginx나 Next.js 서버에서 판정하지 않는다.
- 요청이 1건이라도 확인된 URL은 정리하지 않고 다음 관찰 주기로 넘긴다.
- `/setup`, `/control/acp`, `/execution/mcp`는 legacy URL이 아니라 계속 유지할 canonical URL이며 정리 대상에서 제외한다.

## 호환 처리 대상

서버는 다음 단순 legacy URL을 `307 Temporary Redirect`로 canonical URL에 연결한다.

| Legacy URL | Canonical URL |
|---|---|
| `/runtime-config` | `/control/runtime` |
| `/fallback` | `/execution/fallback` |
| `/llm-usage` | `/execution/usage` |
| `/quota` | `/execution/quota` |
| `/policy` | `/control/policy` |
| `/approvals` | `/control/approvals` |
| `/audit` | `/control/audit` |
| `/embedding` | `/knowledge/embedding` |
| `/knowledge-ops` | `/knowledge/operations` |
| `/users` | `/management/users` |
| `/credentials` | `/management/credentials` |
| `/secrets` | `/management/secrets` |
| `/feature-flags` | `/management/feature-flags` |
| `/profile-ops` | `/management/profile-operations` |
| `/backup` | `/operations/backup` |
| `/security-updates` | `/operations/security-updates` |
| `/license` | `/operations/license` |

다음 URL은 fragment 호환 때문에 client resolver를 유지한다.

| Legacy URL | Client 처리 |
|---|---|
| `/infra` | `tab` 또는 fragment를 연결·실행·지식·모니터링 canonical URL로 해석한다. 알 수 없는 값은 `/operations/health`로 보낸다. |
| `/providers` | `#acp`는 `/control/acp`로 보내고, 그 외 요청은 `/execution/providers`로 보낸다. query string은 보존한다. |

서버는 fragment를 수신할 수 없으므로 위 두 경로에 일괄 서버 redirect를 추가하면 안 된다. nginx 로그에는 `/infra` 또는 `/providers`의 base path와 query까지만 남으며 fragment는 남지 않는다.

## 단계별 운영 절차

| 단계 | 기간 또는 조건 | 운영 동작 |
|---|---|---|
| 진입 | Phase 6 배포 직후 | 17개 단순 경로의 307 redirect와 2개 client resolver를 활성화하고 nginx 접근 로그 집계를 시작한다. |
| 관찰 | 최소 1회 릴리스 주기 이상이며, 각 URL의 마지막 접근 후 30일이 경과 | legacy base path별 request count와 마지막 접근 시각을 집계한다. |
| 판정 | 위 관찰 조건을 충족하고 관찰 기간 합계가 0건 | 해당 URL을 정리 후보 목록에 등록한다. 자동 삭제나 redirect 제거는 금지한다. |
| 정리 | 마스터 승인 후 | 제거 직전에 로그를 다시 확인한다. 여전히 0건인 승인 대상만 redirect 또는 resolver 정리 변경에 포함한다. |

마지막 접근 이후 30일 조건은 URL별로 따로 계산한다. 한 URL의 무접근 상태가 다른 URL의 관찰 기간을 단축하지 않는다.

## nginx 접근 로그 집계

1. 배포 시각, 릴리스 버전, 집계 시작 시각과 nginx 로그 보존·rotation 설정을 기록한다.
2. `/var/log/nginx/access.log`와 관찰 기간에 해당하는 rotated log를 모두 포함한다. 압축 로그를 누락하지 않는다.
3. request field에서 query를 제거한 base path를 기준으로 legacy URL별 요청 수와 마지막 접근 시각을 산출한다. `GET`뿐 아니라 `HEAD` 등 모든 method를 합계에 포함한다.
4. 내부 health check나 검증 crawler가 legacy URL을 호출했다면 별도 user agent 또는 source IP로 구분해 원시 합계와 운영 검증 제외 합계를 모두 보존한다. 제외 기준은 집계 전에 문서화한다.
5. 결과 파일에 집계 명령, 대상 로그 파일 목록, 로그 범위, timezone, URL별 합계와 마지막 접근을 함께 보존한다.

예시 필터는 다음과 같다. 실제 access log format의 request field 위치가 다르면 운영 환경 형식에 맞춰 조정하고, 조정 내용을 결과에 기록한다.

```bash
sudo zgrep -hE '"(GET|HEAD|POST|PUT|PATCH|DELETE|OPTIONS) /(runtime-config|fallback|llm-usage|quota|policy|approvals|audit|embedding|knowledge-ops|users|credentials|secrets|feature-flags|profile-ops|backup|security-updates|license|infra|providers)([? ]|$)' /var/log/nginx/access.log*
```

필터 결과는 base path별로 집계한다. 예를 들어 request field가 `$7`인 combined log라면 query를 제거한 뒤 count할 수 있다.

```bash
sudo zgrep -hE '"(GET|HEAD|POST|PUT|PATCH|DELETE|OPTIONS) /(runtime-config|fallback|llm-usage|quota|policy|approvals|audit|embedding|knowledge-ops|users|credentials|secrets|feature-flags|profile-ops|backup|security-updates|license|infra|providers)([? ]|$)' /var/log/nginx/access.log* \
  | awk '{ split($7, request_path, "?"); count[request_path]++ } END { for (path in count) print path, count[path] }' \
  | sort
```

`#fragment`는 로그에 나타나지 않는다. `/infra#smtp`와 `/providers#acp` 접근은 각각 `/infra`, `/providers` base path의 요청으로만 집계한다.

## 판정과 승인

정리 후보 등록에는 다음 근거가 모두 필요하다.

- Phase 6 배포 후 최소 1회 릴리스 주기를 완료했다.
- 마지막 접근 이후 30일이 지났다.
- 관찰 기간의 해당 legacy URL 요청 합계가 0건이다.
- 로그 rotation과 보존 기간이 전체 관찰 구간을 덮는지 확인했다.
- `/setup`, `/control/acp`, `/execution/mcp`가 후보에 포함되지 않았다.

요청이 1건이라도 있거나 로그 구간이 불완전하면 후보로 등록하지 않고 다음 관찰 주기로 이월한다. 합계 0건은 정리 승인 자체가 아니라 후보 등록 조건일 뿐이다.

## 승인 후 정리와 롤백 준비

1. URL별 집계 근거와 영향 범위를 첨부해 마스터 승인을 받는다.
2. 제거 직전에 최신 `/var/log/nginx` 로그를 같은 기준으로 한 번 더 집계한다.
3. 새 요청이 확인되면 해당 URL만 정리 대상에서 제외하고 다음 주기로 넘긴다.
4. 승인되고 재확인도 0건인 URL만 redirect 또는 client resolver 제거 변경에 포함한다.
5. route와 redirect 변경 이력, 승인 기록, 최종 집계 결과를 릴리스 기록에 보존한다.
6. 배포 후 404 증가와 관련 문의를 감시한다. 회귀가 있으면 보존한 변경 이력으로 해당 redirect 또는 resolver를 복구한다.

route 파일 삭제가 필요해지는 경우에도 이 절차와 별도의 코드 리뷰를 거쳐야 한다. 후보 등록이나 redirect 제거가 route 파일 자동 삭제를 허가하지 않는다.
