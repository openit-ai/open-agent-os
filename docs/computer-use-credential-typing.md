# Computer Use 자격증명 입력 Opt-in 설계 v1.0 (Personal)

> **상태(2026-09-30)**: 설계 정본. Personal에 구현됨
> (`editions/personal/computer-use-policy.sh`).
> Upstream(Hermes Agent 본체) 기본값 `false`와 동일 — upstream 소스는 수정하지 않는다.

## 0. 배경·목표

Hermes Agent 하네스는 에이전트가 비밀번호·카드번호·CVC·인증코드를 직접 타이핑하는 것을
금지한다(볼트 도구 경유만 허용). 마스터(로컬 머신 소유자)는 자신의 Personal 머신에서
이 금지를 **소유자 명시 동의 하에** 풀 수 있어야 한다 — 단 수동 소스 패치가 아니라
OAOS Personal 레이어의 정식 opt-in 기능으로.

- Upstream 이슈: NousResearch/hermes-agent#126421 (OPEN, needs-decision)
- 기존 수동 패치(재사용 참조, 정본 아님):
  `~/.hermes/skills/computer-use-patch/references/computer-use-patch.patch`
  (정책 우회용이므로 Personal 정식 경로로 대체한다)

## 1. 결정

| 항목 | 결정 |
|---|---|
| 설정 키 | `computer_use.allow_credential_typing` (불리언) |
| 기본값 | `false` (upstream 동일 — 미설정 시도 풀림 없음) |
| 적용 범위 | Personal edition, 로컬 머신 소유자만 |
| 구현 위치 | OAOS 자체 레이어 (`editions/personal/computer-use-policy.sh`). **upstream 소스 직접 수정 금지** |
| 동의 방식 | 첫 실행(활성화) 시 경고문 + 직접 입력 동의(`YES`) |
| 비활성화 | 언제든 `disable`로 즉시 복원 (기본값 상태로 복귀) |
| Company/Project | 본 설계 범위 밖 — Company는 정책·승인·감사 체계를 따름 |

## 2. 동작 명세

```
computer-use-policy.sh status    # 현재 상태 출력 (기본 false). 종료코드 0
computer-use-policy.sh enable    # 경고문 출력 → 표준입력으로 YES 입력 시에만 true 기록
computer-use-policy.sh disable   # false로 복원 + 동의 기록 삭제
```

- `enable`은 비대화형 stdin(파이프)에서도 동작하되, 입력값이 정확히 `YES`가 아니면
  거부하고 종료코드 3(게이트 차단)으로 끝낸다. `--yes` 같은 무확인 플래그는 두지 않는다.
- 동의 기록: 상태 디렉터리(`~/.oaos` 또는 플랫폼 경로)에 `computer-use-consent` 파일로
  시각·호스트를 남긴다. 감사 목적이며 비밀을 포함하지 않는다.
- 설정 저장은 Hermes 공식 경로만 사용한다: `hermes config set/get
  computer_use.allow_credential_typing`. Hermes가 키를 모르면(구버전) 스크립트는
  실패를 보고하고 종료코드 1 — 임의 파일에 우회 기록을 남기지 않는다.
- 설치기(`install.sh`)의 9개 스테이지·검증기(`personal-verify.sh`)의 12개 검사는
  그대로 둔다(스모크 테스트 고정). 설치기 `harness` 단계는 안내(info)만 출력한다.

## 3. 안전 원칙

1. 기본 거부 — 오너가 명시적으로 풀기 전까지 upstream 금지 그대로.
2. 비밀 비노출 — 경고·로그·동의 기록에 자격증명 값을 절대 남기지 않는다.
3. 언제든 철회 — `disable` 한 번으로 기본 상태 복귀.
4. 원격·공유 머신 금지 — Personal 로컬 소유자 전제.他人 머신·서버 경로에 적용하지 않는다.

## 4. 관련 문서

- 사용 레시피: [쿡북 §6](cookbook.md)
- Personal 설치: [Personal README](../editions/personal/README.md)
- Harness 구성: [Harness README](../harness/README.md)
