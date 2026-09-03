#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
daily_brief.py — 개인 브리핑 생성기 (교본 §6.2 항목 3)

'오늘 내 일정과 준비사항 알려줘' → 일정 + 메일 + 문서 종합 브리핑.

수집 소스 (4종):
  (a) Google Calendar 오늘 일정          — gws calendar events list
  (b) Gmail 오늘 중요 메일 요약          — gws gmail users messages (list + get metadata)
  (c) wiki 업무일지 오늘/어제             — /data/wiki/note/업무일지/YYYY-MM-DD*.md
  (d) mattermost-log 최근 1일 주요 항목   — /data/wiki/mattermost-log.md

권한 판정: ~/.hermes/scripts/permission_check.py --user {사용자} --action read --target company
  → exit 0(PASS) 전제. FAIL이면 브리핑 생성 중단.

출력:
  - ~/.hermes/briefings/YYYY-MM-DD-{사용자}.md 파일 저장 + stdout 동일 내용 출력
  - --dry-run: 수집·조립만 하고 파일 저장 안 함

사용법:
  python3 ~/.hermes/scripts/daily_brief.py                      # 오늘, 김민영 기본
  python3 ~/.hermes/scripts/daily_brief.py --user 김민영          # 명시
  python3 ~/.hermes/scripts/daily_brief.py --user mykim --dry-run  # 수집만 (파일 저장 안 함)
  python3 ~/.hermes/scripts/daily_brief.py --date 2026-08-01      # 특정 날짜 (테스트용)

제약:
  - Python3 표준 라이브러리만 사용 (외부 패키지 금지)
  - Google API 실제 호출은 gws CLI 경유 (직접 OAuth 재구현 금지)
  - 개인정보(메일 본문)는 브리핑 파일에만 저장 — stdout은 요약(제목·발신자·스니펫)만 출력
  - /data/wiki는 읽기 전용
"""

import argparse
import html
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── 경로/상수 ──────────────────────────────────────────────────────────
HERMES = Path.home() / ".hermes"
PERM_CHECK = HERMES / "scripts/permission_check.py"
IDENTITY_FILE = HERMES / "users" / "identity.md"
PERMISSIONS_FILE = HERMES / "users" / "permissions.md"
BRIEFINGS_DIR = HERMES / "briefings"
GWS_TOKENS_BASE = HERMES / "google-tokens"          # 요청자 내부 user_id 기준 전용 토큰 경로
USER_CHANNEL_MAP = GWS_TOKENS_BASE / "user-channel-map.json"
GWS_BIN = "gws"

VAULT_WIKI = Path("/data/wiki")
WORKLOG_DIR = VAULT_WIKI / "note" / "업무일지"
MATTERMOST_LOG = VAULT_WIKI / "mattermost-log.md"

KST = timezone(timedelta(hours=9))
MAX_MAILS = 10        # 메일 최대 수집 수
MAX_MM_BLOCKS = 8     # mattermost 최대 표시 블록 수
MAX_MAIL_SNIPPET = 120  # 메일 스니펫 최대 길이

# 메일 필터 — 뉴스레터/광고/시스템 발신 제외 (generate_work_log.py 패턴 재사용)
SKIP_KEYWORDS = ["광고", "newsletter", "unsubscribe", "뉴스레터", "특별 이벤트"]
GMAIL_SYSTEM_FROM = re.compile(
    r"(mailer-daemon|mail delivery|noreply|no-reply|notifications?@"
    r"|cron@|alert@|monitor@|backerupdate|service\.alibaba)", re.IGNORECASE
)


# ── 사용자/권한 ────────────────────────────────────────────────────────

def parse_identity(path: Path) -> dict:
    """identity.md → 성명 → {email, mm_ids}."""
    users = {}
    if not path.exists():
        return users
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 7:
            continue
        name = cells[0]
        if not re.search(r"[가-힣]", name) or name.startswith(":"):
            continue
        if cells[1] == "직급":
            continue
        email = cells[5] if ("@" in cells[5]) else None
        mm_ids = []
        raw = cells[6]
        if raw and raw != "(미기재)":
            for token in re.split(r"[()\s,]+", raw):
                token = token.strip().lstrip("@")
                if re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_.-]*", token):
                    mm_ids.append(token)
        users[name] = {"email": email, "mm_ids": mm_ids}
    return users


def parse_permissions(path: Path) -> dict:
    """permissions.md → 성명 → level."""
    users = {}
    if not path.exists():
        return users
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 8:
            continue
        name = cells[0]
        if not re.search(r"[가-힣]", name) or name.startswith(":"):
            continue
        m = re.search(r"L[0-5]", cells[2] or "")
        if m:
            users[name] = {"level": m.group(0)}
    return users


def resolve_user(identifier: str) -> dict:
    """이름|MM ID|이메일 → {name, email, mm_ids, level}. 미등록이면 None."""
    ids = parse_identity(IDENTITY_FILE)
    perms = parse_permissions(PERMISSIONS_FILE)
    key = identifier.strip().lstrip("@").lower()
    for name, info in ids.items():
        if name == identifier.strip() or name.lower() == key:
            return {"name": name, "email": info["email"], "mm_ids": info["mm_ids"],
                    "level": perms.get(name, {}).get("level", "?")}
        if info["email"] and info["email"].lower() == key:
            return {"name": name, "email": info["email"], "mm_ids": info["mm_ids"],
                    "level": perms.get(name, {}).get("level", "?")}
        for mid in info["mm_ids"]:
            if mid.lower() == key:
                return {"name": name, "email": info["email"], "mm_ids": info["mm_ids"],
                        "level": perms.get(name, {}).get("level", "?")}
    return None


def check_permission(identifier: str) -> tuple[bool, str]:
    """permission_check.py 호출 — read company PASS(exit 0) 여부. (bool, 근거/출력)"""
    if not PERM_CHECK.exists():
        return False, f"permission_check.py 없음: {PERM_CHECK}"
    try:
        r = subprocess.run(
            [sys.executable, str(PERM_CHECK), "--user", identifier,
             "--action", "read", "--target", "company"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"permission_check.py 실행 실패: {e}"
    out = (r.stdout or "").strip() or (r.stderr or "").strip()
    return (r.returncode == 0), out


# ── Google (gws CLI 경유) ─────────────────────────────────────────────

def _canonical_user_id(user: dict) -> str | None:
    """Resolve the stable token-directory ID from the verified user profile."""
    mm_ids = {str(value).strip().lower() for value in user.get("mm_ids", []) if value}
    if USER_CHANNEL_MAP.exists():
        try:
            mapping = json.loads(USER_CHANNEL_MAP.read_text(encoding="utf-8"))
            for key, value in mapping.items():
                channel, _, channel_id = str(key).partition(":")
                if channel == "mattermost" and channel_id.lower() in mm_ids:
                    candidate = str(value).strip()
                    if re.fullmatch(r"[A-Za-z0-9_.-]+", candidate):
                        return candidate
        except (OSError, json.JSONDecodeError):
            pass
    email = str(user.get("email") or "").split("@", 1)[0].strip()
    if re.fullmatch(r"[A-Za-z0-9_.-]+", email):
        return email
    return None


def _get_token_path(mm_ids: list[str], user_id: str | None = None) -> Path:
    """Return only the verified owner's token path; never use a global fallback."""
    if not user_id or not re.fullmatch(r"[A-Za-z0-9_.-]+", str(user_id)):
        raise RuntimeError("Google 토큰 소유자 ID가 검증되지 않았습니다")
    token_path = GWS_TOKENS_BASE / str(user_id) / "google_token.json"
    if not token_path.exists():
        raise RuntimeError(f"Google 토큰 없음: 요청자 {user_id} 전용 토큰이 필요합니다")
    return token_path


def _refresh_token(token_data: dict, path: Path) -> dict:
    """refresh_token grant로 access token 갱신 (gws_bridge.py와 동일 패턴)."""
    params = urllib.parse.urlencode({
        "client_id": token_data["client_id"],
        "client_secret": token_data["client_secret"],
        "refresh_token": token_data["refresh_token"],
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request(token_data["token_uri"], data=params)
    with urllib.request.urlopen(req, timeout=15) as resp:
        result = json.loads(resp.read())
    token_data["token"] = result["access_token"]
    token_data["expiry"] = datetime.fromtimestamp(
        datetime.now(timezone.utc).timestamp() + result["expires_in"], tz=timezone.utc
    ).isoformat()
    path.write_text(json.dumps(token_data, indent=2), encoding="utf-8")
    return token_data


def _valid_token(mm_ids: list[str], user_id: str | None = None) -> str:
    """유효한 access token 반환 (요청자 전용 경로, 만료 시 refresh)."""
    path = _get_token_path(mm_ids, user_id)
    if not path.exists():
        raise RuntimeError(f"Google 토큰 없음: {path} — setup.py로 OAuth 인증 필요")
    token_data = json.loads(path.read_text(encoding="utf-8"))
    expiry = token_data.get("expiry", "")
    if expiry:
        if isinstance(expiry, (int, float)):
            exp_dt = datetime.fromtimestamp(expiry, tz=timezone.utc)
        else:
            exp_dt = datetime.fromisoformat(str(expiry).replace("Z", "+00:00"))
        if datetime.now(timezone.utc) >= exp_dt:
            token_data = _refresh_token(token_data, path)
    return token_data["token"]


def _gws(args: list[str], mm_ids: list[str], user_id: str | None = None, timeout: int = 60) -> dict:
    """gws CLI 호출 — 요청자 전용 token만 사용."""
    token = _valid_token(mm_ids, user_id)
    env = os.environ.copy()
    env["GOOGLE_WORKSPACE_CLI_TOKEN"] = token
    try:
        r = subprocess.run([GWS_BIN] + args, env=env, capture_output=True,
                           text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"gws 실행 실패: {e}") from e
    if r.returncode != 0 or not r.stdout.strip():
        detail = (r.stderr or r.stdout or "").strip()
        raise RuntimeError(f"gws {args[0]} {args[1] if len(args) > 1 else ''} 오류: {detail[:300]}")
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"gws 응답 JSON 파싱 실패: {e}") from e


# ── 수집: Calendar ────────────────────────────────────────────────────

def collect_calendar(date_str: str, mm_ids: list[str], user_id: str | None = None) -> list[dict]:
    """오늘 일정. 없으면 빈 리스트."""
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=KST)
    t_min = dt.isoformat()
    t_max = (dt + timedelta(days=1)).isoformat()
    params = {
        "calendarId": "primary",
        "timeMin": t_min,
        "timeMax": t_max,
        "singleEvents": True,
        "orderBy": "startTime",
        "maxResults": 50,
    }
    data = _gws(["calendar", "events", "list", "--params", json.dumps(params)], mm_ids, user_id)
    items = data.get("items", []) or []
    events = []
    for it in items:
        start_raw = it.get("start") or {}
        end_raw = it.get("end") or {}
        start = start_raw.get("dateTime") or start_raw.get("date") or ""
        end = end_raw.get("dateTime") or end_raw.get("date") or ""
        events.append({
            "summary": (it.get("summary") or "").strip(),
            "start": start,
            "end": end,
            "location": (it.get("location") or "").strip(),
            "description": (it.get("description") or "").strip(),
            "htmlLink": it.get("htmlLink") or "",
        })
    events.sort(key=lambda e: e["start"])
    return events


# ── 수집: Gmail ───────────────────────────────────────────────────────

def collect_mail(date_str: str, mm_ids: list[str], user_id: str | None = None) -> list[dict]:
    """오늘 중요 메일 요약 (제목·발신자·스니펫만 — 본문 미수신)."""
    # Gmail 검색: 최근 1일 수신 + 안읽음 메일 (읽지 않음/중요 우선)
    query = "(newer_than:1d OR is:unread)"
    data = _gws(["gmail", "users", "messages", "list", "--params", json.dumps({
        "userId": "me", "q": query, "maxResults": 20,
    })], mm_ids, user_id)
    msg_ids = [m["id"] for m in (data.get("messages") or [])][:MAX_MAILS]

    mails = []
    for mid in msg_ids:
        md = _gws(["gmail", "users", "messages", "get", "--params", json.dumps({
            "userId": "me", "id": mid, "format": "metadata",
            "metadataHeaders": ["From", "To", "Subject", "Date"],
        })], mm_ids, user_id)
        headers = {h["name"].lower(): h["value"] for h in (md.get("payload") or {}).get("headers", [])}
        subject = html.unescape(headers.get("subject", "") or "(제목 없음)")
        frm = headers.get("from", "") or ""
        date_hdr = headers.get("date", "") or ""
        snippet = html.unescape((md.get("snippet") or "").strip().replace("\n", " ")[:MAX_MAIL_SNIPPET])
        labels = md.get("labelIds", []) or []

        if any(k.lower() in subject.lower() for k in SKIP_KEYWORDS):
            continue
        if GMAIL_SYSTEM_FROM.search(frm):
            continue

        em = re.search(r"[\w.+-]+@[\w.-]+", frm)
        from_addr = em.group(0) if em else frm
        mails.append({
            "id": mid,
            "subject": subject,
            "from": from_addr,
            "date": date_hdr,
            "snippet": snippet,
            "unread": "UNREAD" in labels,
            "important": "IMPORTANT" in labels,
        })
    # 안읽음 → 중요 → 최신순
    mails.sort(key=lambda m: (not m["unread"], not m["important"], m["date"]), reverse=False)
    return mails


# ── 수집: wiki 업무일지 ───────────────────────────────────────────────

def collect_worklog(date_str: str) -> dict:
    """오늘/어제 업무일지 파일. {date: {path, title, lines}}"""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    yesterday = (dt - timedelta(days=1)).strftime("%Y-%m-%d")
    result = {}
    for d in (date_str, yesterday):
        matches = sorted(WORKLOG_DIR.glob(f"{d}*.md")) if WORKLOG_DIR.exists() else []
        if not matches:
            continue
        path = matches[0]
        text = path.read_text(encoding="utf-8")
        # frontmatter 제거
        body = re.sub(r"^---\n.*?\n---\n", "", text, flags=re.S)
        lines = []
        for ln in body.splitlines():
            s = ln.rstrip()
            if not s.strip():
                continue
            if s.startswith("# ") and not s.startswith("## "):
                continue  # H1 제목 생략 (출처에 이미 표시)
            if s.startswith("## ") or s.startswith("- ") or s.startswith("* "):
                lines.append(s)
        result[d] = {"path": str(path), "title": text.splitlines()[0] if text.splitlines() else "", "lines": lines}
    return result


# ── 수집: mattermost-log ──────────────────────────────────────────────

def collect_mattermost(date_str: str) -> list[dict]:
    """최근 1일 주요 대화 블록 (주제·결정·액션)."""
    if not MATTERMOST_LOG.exists():
        return []
    text = MATTERMOST_LOG.read_text(encoding="utf-8")
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    cutoff = (dt - timedelta(days=1)).strftime("%Y-%m-%d")

    # 블록: ## [YYYY-MM-DD HH:MM~HH:MM] 채널 (발신 → 수신) — 제목
    blocks = re.findall(
        r"## \[(\d{4}-\d{2}-\d{2})[^\]]*\]\s*([^\n]*)\n(.*?)(?=\n## \[|\Z)",
        text, re.DOTALL,
    )
    picked = []
    for bdate, header, body in blocks:
        if bdate < cutoff:
            continue
        title = header.strip()
        # 주제·결정·액션 라인만 추출
        notes = []
        for ln in body.splitlines():
            s = ln.strip()
            if s.startswith("- **") and ("**:" in s or "**: " in s):
                notes.append(s[:160])
        picked.append({"date": bdate, "title": title, "notes": notes[:6]})
        if len(picked) >= MAX_MM_BLOCKS:
            break
    return picked


# ── 조립 ──────────────────────────────────────────────────────────────

def _fmt_time(iso: str) -> str:
    """ISO 시작 시각 → 'HH:MM' 또는 '종일'."""
    if not iso:
        return ""
    if len(iso) == 10:  # date only → 종일
        return "종일"
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(KST).strftime("%H:%M")
    except (ValueError, TypeError):
        return iso[:16]


def compose(cal: list[dict], mails: list[dict], worklog: dict,
            mm_blocks: list[dict], date_str: str, user: dict) -> str:
    """최종 마크다운 브리핑 조립 — [오늘 일정] [메일 요약] [업무일지 연계] [준비사항·우선순위]."""
    w = []
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=KST)
    wday = ("월", "화", "수", "목", "금", "토", "일")[dt.weekday()]
    w.append(f"# 📋 개인 브리핑 — {dt.year}년 {dt.month}월 {dt.day}일 ({wday}요일)")
    w.append(f"> 대상: {user['name']} (권한 {user['level']}) · 생성: {datetime.now(KST).strftime('%Y-%m-%d %H:%M')} KST")
    w.append("")

    # 1) 오늘 일정
    w.append("## 📅 오늘 일정")
    if not cal:
        w.append("- 일정 없음")
    else:
        for ev in cal:
            tm = _fmt_time(ev["start"])
            loc = f" @ {ev['location']}" if ev["location"] else ""
            desc = ""
            if ev["description"]:
                desc = f" — {ev['description'][:60]}"
            w.append(f"- **{tm or '시간 미지정'}** {ev['summary']}{loc}{desc}")
    w.append("")

    # 2) 메일 요약
    w.append("## 📧 메일 요약 (오늘)")
    if not mails:
        w.append("- 오늘 중요 메일 없음")
    else:
        for m in mails:
            flag = "📌 " if (m["unread"] or m["important"]) else ""
            snip = f" — {m['snippet']}" if m["snippet"] else ""
            w.append(f"- {flag}**{m['subject']}** ({m['from']}){snip}")
    w.append("")

    # 3) 업무일지 연계
    w.append("## 📝 업무일지 연계")
    if not worklog:
        w.append(f"- {date_str} 기준 업무일지 기록 없음 (오늘·어제 파일 미존재)")
    else:
        for d in sorted(worklog.keys(), reverse=True):
            wl = worklog[d]
            label = "오늘" if d == date_str else "어제"
            w.append(f"### {label} ({d})")
            w.append(f"- 출처: `{wl['path']}`")
            for ln in wl["lines"][:14]:
                w.append(f"  {ln}")
    w.append("")

    # 4) 준비사항·우선순위 (규칙 기반 합성)
    w.append("## ✅ 준비사항·우선순위")
    items = []
    if cal:
        first = cal[0]
        tm = _fmt_time(first["start"])
        items.append(f"첫 일정 **{tm}** — {first['summary']} (오늘 일정 {len(cal)}건)")
    unread = [m for m in mails if m["unread"]]
    if unread:
        items.append(f"안읽은 메일 **{len(unread)}통** 확인 필요 — {unread[0]['subject']} 외")
    elif mails:
        items.append(f"오늘 수신 중요 메일 {len(mails)}통 검토")
    mm_actions = []
    for b in mm_blocks:
        for n in b["notes"]:
            if n.startswith("- **액션") or n.startswith("- **결정"):
                mm_actions.append(f"[{b['date']}] {n}")
    if mm_actions:
        items.append("Mattermost 결정/액션 대기:")
        for a in mm_actions[:4]:
            items.append(f"  - {a}")
    if not items:
        items.append("특별한 준비사항 없음 — 일정·메일·대기 업무 모두 없음")
    for it in items:
        w.append(f"- {it}")
    w.append("")

    # 꼬리말
    src = []
    if cal:
        src.append("일정")
    if mails:
        src.append("메일")
    if worklog:
        src.append("업무일지")
    if mm_blocks:
        src.append("Mattermost")
    w.append("---")
    w.append(f"*자동 생성 — daily_brief.py · 수집: {', '.join(src) if src else '없음'}*")
    w.append("")
    return "\n".join(w)


# ── main ──────────────────────────────────────────────────────────────

def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="개인 브리핑 생성기 (교본 §6.2 항목 3)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--user", default="김민영",
                   help="사용자 (이름|MM ID|이메일, 기본: 김민영)")
    p.add_argument("--date", default=None,
                   help="대상 날짜 YYYY-MM-DD (기본: 오늘, 테스트용)")
    p.add_argument("--dry-run", action="store_true",
                   help="수집·조립만 하고 파일 저장 안 함")
    return p


def main() -> int:
    args = _parser().parse_args()
    date_str = args.date or datetime.now(KST).strftime("%Y-%m-%d")

    # 1) 사용자 식별
    user = resolve_user(args.user)
    if not user:
        print(f"[ERROR] 사용자 미등록: {args.user} (identity.md/permissions.md 확인)", file=sys.stderr)
        return 2

    # 2) Resolve the stable per-user token directory before any Google call.
    token_user_id = _canonical_user_id(user)
    if not token_user_id:
        print(f"[ERROR] 요청자 전용 Google 토큰 ID를 확인할 수 없습니다: {user['name']}", file=sys.stderr)
        return 1

    # 3) 권한 판정 — read company PASS 전제
    ok, evidence = check_permission(user["name"])
    if not ok:
        print(f"[ERROR] 권한 판정 실패 — 브리핑 생성 불가", file=sys.stderr)
        print(evidence, file=sys.stderr)
        return 1
    print(f"[권한] {user['name']} ({user['level']}) read company → PASS · token_owner={token_user_id}", file=sys.stderr)

    # 4) 수집 — every Google call receives the same verified owner ID.
    try:
        cal = collect_calendar(date_str, user["mm_ids"], token_user_id)
        mails = collect_mail(date_str, user["mm_ids"], token_user_id)
    except RuntimeError as e:
        print(f"[ERROR] Google 수집 실패: {e}", file=sys.stderr)
        return 1
    worklog = collect_worklog(date_str)
    mm_blocks = collect_mattermost(date_str)

    print(f"[수집] 일정 {len(cal)}건 · 메일 {len(mails)}통 · 업무일지 {len(worklog)}건 · MM {len(mm_blocks)}블록",
          file=sys.stderr)

    # 4) 조립
    brief = compose(cal, mails, worklog, mm_blocks, date_str, user)

    # 5) 출력 — stdout (브리핑 형식)
    print(brief)

    # 6) 저장 — dry-run이 아니면 파일 (파일명은 영문 ID — 부록 I 네이밍 표준)
    if not args.dry_run:
        BRIEFINGS_DIR.mkdir(parents=True, exist_ok=True)
        user_id = (user.get("mm_ids") or [None])[0] or user.get("email", "").split("@")[0] or "unknown"
        out_path = BRIEFINGS_DIR / f"{date_str}-{user_id}.md"
        out_path.write_text(brief, encoding="utf-8")
        print(f"\n[저장] {out_path}", file=sys.stderr)
    else:
        print("\n[dry-run] 파일 저장 안 함", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
