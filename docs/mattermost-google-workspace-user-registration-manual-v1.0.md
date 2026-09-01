# OAOS 사용자 등록 표준 가이드 v1.0

> 부제: Mattermost 기반 직원 확인·개인화·세션 분리·Google Workspace 연동 절차

## 1. 목적

Mattermost의 본인 계정을 OAOS Personal Agent에 등록하고, 직원 확인·상호 호칭 정의·최초 성향 파악·세션 분리를 거친 뒤 본인 Google Workspace 계정을 선택적으로 연결한다. 연결된 Calendar·Gmail·Drive는 사용자별로 분리하며, 다른 직원의 계정·토큰·업무 데이터로 fallback하지 않는다.

## 2. 표준 원칙

```text
본인 Mattermost 계정
  → @agent 개인 DM
  → verified Mattermost username/user_id
  → canonical internal user_id
  → 전용 OAuth state/session
  → Google profile email 확인
  → 해당 user_id 전용 credential
```

필수 불변식:

- Mattermost 사용자 식별과 OAuth 요청 소유자는 동일해야 한다.
- Mattermost 등록 이메일과 Google Gmail `users.getProfile(userId=me)`의 `emailAddress`가 일치해야 한다.
- `~/.hermes/google_token.json` 같은 전역 토큰 fallback을 사용하지 않는다.
- 다른 직원의 token directory, refresh token, OAuth state를 사용하지 않는다.
- 전용 토큰이 없거나 계정 이메일이 불일치하면 저장·조회·브리핑을 fail-closed한다.
- 비밀번호·OTP·client secret·access token·refresh token·OAuth JSON 전체를 Mattermost에 입력하지 않는다.
- 발신자는 항상 `@agent`; 직원은 본인 계정으로 `@agent` DM에 회신한다.

## 3. 직원 등록 표준 순서

Google OAuth는 첫 대화에서 바로 시작하지 않는다. **직원 확인 → 인사말·기초 대화 → 세션 분리 확인 → Google OAuth** 순서를 반드시 지킨다.

### 3.1 1단계 — 직원 리스트와 본인 계정 확인

1. @agent가 서버의 승인된 직원 매핑에서 Mattermost username/user_id를 조회한다.
2. 표시명만으로 신원을 확정하지 않고, Mattermost의 안정적인 user ID와 등록된 직원 프로필을 대조한다.
3. 내부 `employee:{user_id}`와 `agent:assistant:{user_id}`를 결정한다.
4. 매핑이 없거나 직원 정보가 불일치하면 OAuth를 시작하지 않고 운영자 확인으로 보낸다.

### 3.2 2단계 — 인사말·호칭 정의·최초 성향 파악

여기서 말하는 **기초 데이터**는 인사정보를 대량 수집하는 것이 아닙니다. 다음 두 범위의 최소 대화 데이터만 수집합니다.

- **상호 호칭 정의:** 직원이 원하는 호칭과 @agent가 사용할 호칭
- **최초 성향 파악:** 답변 길이·말투·업무 응답 방식 등을 파악하기 위한 간단한 대화 질문

진행 예시:

1. @agent가 본인 Mattermost DM으로 정중한 인사말을 보낸다.
2. “어떻게 불러드리면 될까요?”라고 질문해 원하는 호칭을 확인한다.
3. “답변은 간단하게 드릴까요, 자세히 드릴까요?” 또는 “업무 요청 시 결론부터 드릴까요?”처럼 짧은 선택형 질문을 1~3개 진행한다.
4. 확인된 호칭·응답 선호만 해당 사용자의 Profile/Preferences에 저장한다.
5. 비밀번호·OTP·Google token·민감 개인정보·불필요한 인사정보는 수집하지 않는다.
6. 직원의 회신이 실제 직원 계정에서 온 것인지 확인하고, 회신 Post·작성자·시각을 기록한다.
7. 직원이 답변을 원하지 않거나 중단을 요청하면 즉시 중단하고, OAuth 단계로 진행하지 않는다.

### 3.3 3단계 — 세션 분리 확인

1. 직원의 회신이 `@agent` DM에 도착하는지 확인한다.
2. `employee:{user_id}`에 귀속된 OAOS 세션이 생성되는지 확인한다.
3. 세션 namespace·prompt history·response thread가 다른 사용자와 다른지 확인한다.
4. 이 단계에서는 Google API를 호출하지 않는다.
5. 세션 분리가 확인되지 않으면 OAuth URL을 발급하지 않는다.

### 3.4 4단계 — Google OAuth 시작

세션 분리 확인 후 직원이 다음 문구를 보내면 OAuth를 시작한다.

```text
구글 워크스페이스 연동 시작
```

1. @agent가 현재 검증된 사용자에만 바인딩된 OAuth state와 승인 URL을 생성한다.
2. 직원이 본인의 회사 Google Workspace 계정으로 로그인·승인한다.
3. `localhost:1` 또는 연결 실패 화면이 나타나도 정상일 수 있다.
4. 브라우저 주소창의 **전체 callback URL**을 같은 @agent DM에 회신한다.
5. @agent가 state, Mattermost 사용자, 내부 user_id, Google profile email, scope를 검증한다.
6. `Google profile email == Mattermost/사내 공식 이메일`일 때만 사용자 전용 credential을 저장한다.
7. 검증 통과 안내를 받은 후에만 Calendar·Gmail·Drive 기능을 사용한다.

### 3.5 등록 상태

```text
DISCOVERED   직원 매핑 확인
GREETED      인사말 전송
BASIC_READY  기초 대화·최소 정보 확인
SESSION_OK   사용자별 OAOS 세션 분리 확인
OAUTH_PENDING OAuth URL 발급·승인 대기
VERIFIED     Google profile email·scope 검증 완료
CONNECTED    전용 credential 저장 및 기능 연결
```

`SESSION_OK` 이전에는 `OAUTH_PENDING`으로 이동할 수 없다.
## 4. 금지 입력

다음 값은 절대 Mattermost에 보내지 않는다.

- Google 비밀번호
- OTP·2단계 인증 코드
- `client_secret`
- access token
- refresh token
- credential JSON 전체

OAuth 승인 URL과 callback URL에도 개인정보가 포함될 수 있으므로 다른 사람에게 전달하지 않는다.

## 5. 사용자별 저장·검증 기준

예시:

```text
Mattermost @mykim  → mykim
Mattermost @henry  → hrchoi
Mattermost @bysung → bysung
```

토큰 저장 경로:

```text
~/.hermes/google-tokens/{canonical_user_id}/google_token.json
```

계정 검증:

```bash
python3 ~/.hermes/skills/system/multi-channel-auth/scripts/verify_gws_token.py <canonical_user_id>
```

성공 조건:

```text
GMAIL_PROFILE_EMAIL == Mattermost/사내 공식 이메일
SCOPES가 승인된 업무 범위와 일치
exit code == 0
```

현재 확인된 김민영 계정:

```text
Mattermost: @mykim / mykim@openit.co.kr
Google:     mykim@openit.co.kr
판정:       일치
```

## 6. 오류 처리

- 사용자 매핑 없음: OAuth URL 발급 중단
- 전용 token 없음: Google API 호출 중단
- callback state 불일치: 해당 callback 폐기, 새 연동 시작
- Google profile email 불일치: 토큰 저장·브리핑 중단
- scope 부족: 부분 연동으로 표시하고 완료 처리하지 않음
- 토큰 만료: 동일 사용자의 OAuth 재인증만 수행
- 다른 사용자 token으로 대체: 금지

## 7. 운영자 검증 체크리스트

- [ ] 발신자가 `@agent`인가
- [ ] 수신자가 본인 Mattermost DM인가
- [ ] Mattermost username/user_id가 canonical user_id로 매핑되는가
- [ ] OAuth state가 요청자·세션에 바인딩되는가
- [ ] 전용 token directory를 사용하는가
- [ ] Google profile email이 공식 Mattermost/Workspace 이메일과 일치하는가
- [ ] scope가 확인됐는가
- [ ] callback·token·secret이 로그에 남지 않았는가
- [ ] 사용자별 브리핑 파일이 별도 생성되는가
- [ ] 다른 사용자 token fallback이 없는가
- [ ] 성공 후 Gmail·Calendar·Drive 최소 read-back이 가능한가

## 8. 현재 직원 안내 발송 기록

- 최행로 소장님 `@henry`: `8ijeg1g91pd7jdb738k4883eyw`
- 성백영 부장님 `@bysung`: `tw9t77rq37gbdgt84n4fd15q6h`
- 발신자: `@agent` (`bmhbteup4p8bmb8rfh151y6w1e`)
- 두 DM 모두 Mattermost API read-back 완료

## 9. 상태와 한계

이 매뉴얼은 사용자별 OAuth 등록·검증 기준을 정의한다. 실제 Google provider의 계정 email read-back과 각 직원의 외부 Google 연동 완료 여부는 직원별 OAuth 절차가 끝난 뒤 개별 확인한다. 테스트·설계·운영 적용 증거를 혼합하지 않는다.
