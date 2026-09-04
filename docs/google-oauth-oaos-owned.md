# OAOS-owned Google OAuth

## 경계

- Google 사용자 동의와 callback은 OAOS Control Plane이 소유합니다.
- Hermes의 `/home/hermes/.hermes/google_token.json`을 읽거나 OAOS 계정에 공유하지 않습니다.
- client secret은 OAOS systemd EnvironmentFile에만 두고 API 응답·로그·DB에 반환하지 않습니다.
- access/refresh token bundle은 Credential Vault에 저장하고 DB에는 `secret_ref`와 delegation/binding 메타데이터만 남깁니다.

## 사용자 흐름

1. 인증된 사용자가 `POST /v1/google/oauth/authorize`를 호출합니다.
2. Control Plane은 tenant/user/agent/session을 검증하고 PKCE S256 + 1회성 만료 state를 발급합니다.
3. 사용자는 반환된 `authorization_url`로 Google 동의를 진행합니다.
4. Google은 `GET /v1/google/oauth/callback?code=...&state=...`로 돌아옵니다. callback은 state만으로 소유자를 복원하며 Authorization header가 필요하지 않습니다.
5. OAOS는 서버 측에서 코드를 교환하고 userinfo email을 검증한 뒤 Vault에 저장하고 delegation/credential binding을 생성합니다.
6. 이후 Execution Gateway의 GoogleConnector는 delegation의 active `secret_ref`를 찾고 Vault에서 소유자 검증 후 token을 읽습니다. 만료 시 Google token endpoint로 갱신하고 새 Vault reference로 회전합니다.

호환 경로로 `/v1/oauth/google/start`, `/v1/oauth/google/callback`, `/v1/oauth/google/status`, `/v1/oauth/google/revoke`도 제공하지만 구현은 canonical `/v1/google/oauth/*`에 위임합니다. 기존 Admin Console `/v1/oauth/config`는 client secret을 저장하지 않는 설정/상태 경로로 유지합니다.

## 운영 설정

`/etc/oaos/oaos.env`에 아래 키를 설정합니다. 실제 값은 출력·커밋하지 않습니다.

```text
GOOGLE_CLIENT_ID=<Google OAuth client id>
GOOGLE_CLIENT_SECRET=<server-side secret>
GOOGLE_REDIRECT_URI=http://localhost:49697/callback
OAOS_GOOGLE_OAUTH_STATE_TTL=600

현재 Google Cloud Console 등록 URI가 localhost인 경우, 사용자 PC에서 OAOS 서버로 다음 SSH 터널을 유지합니다.

```bash
ssh -N -L 49697:127.0.0.1:8100 openit@openit-oaos
```

브라우저의 `http://localhost:49697/callback` 요청은 Control Plane의 `/callback` alias에서 canonical OAuth callback으로 처리됩니다.
REDIS_URL=<production Redis URL>
OAOS_REDIS_URL=<production Redis URL, REDIS_URL 대체명>
VAULT_BACKEND=encrypted_postgres

현재 openit-oaos는 기존 OAOS PostgreSQL 암호화 Vault를 사용합니다. 외부 HashiCorp/AWS Vault 전환은 별도 승인·구성·read-back이 필요한 후속 운영 변경이며, Hermes 토큰 파일을 공유하는 대안이 아닙니다.
```

Production에서는 Redis state store와 Credential Vault가 없으면 flow가 fail-closed되어야 합니다. OAuth callback URL은 Google Cloud Console의 redirect URI와 byte 단위로 일치해야 합니다.

## 검증

- 단위/통합: `tests/test_cp_google_oauth.py`, `tests/test_google_oauth_cp.py`, `tests/test_google_connector_resolver.py`, `tests/test_google_connector_token_resolver.py`
- 실행: `python3 -m py_compile ...` 및 위 OAuth/Google regression tests
- 실제 운영 적용 후에는 Control Plane/Execution Gateway의 PID, unit, effective `/proc/<pid>/environ`, `/openapi.json`, Redis/Vault 상태를 별도 read-back합니다.
- Google 실제 동의·토큰 교환은 사용자 계정의 외부 인증 행위이므로 테스트용 synthetic token과 구분합니다.
