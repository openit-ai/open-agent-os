"""Company C10 console: G10 setup, one-time login, sessions and minimal tabs.

The process binds only to 127.0.0.1:8765 behind the existing nginx HTTPS path.
It never stores or prints a plaintext code/session token in PostgreSQL, the
service log or the rendered page. Browser state is an HttpOnly Secure cookie;
CSRF tokens are derived from that cookie with the Company console secret.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from project_api import ProjectAPI, ProjectAPIError, ProjectUserNotAdmin


HEALTH_PATH = "/company/health"
SETUP_PATH = "/company/setup"
LOGIN_PATH = "/company/login"
CONSOLE_PATH = "/company/console"
COOKIE_NAME = "oaos_company_session"
MAX_FORM_BYTES = 64 * 1024
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class ConsoleError(Exception):
    """A generic console failure that is safe to show without detail."""


def _sha256(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _console_secret():
    raw = os.environ.get("OAOS_COMPANY_CONSOLE_SECRET", "")
    try:
        key = base64.b64decode(raw, validate=True)
    except (ValueError, TypeError):
        key = b""
    if len(key) != 32:
        raise ConsoleError("Company console secret is missing")
    return key


def csrf_for(secret, token):
    return hmac.new(secret, b"csrf:" + token.encode("ascii"), hashlib.sha256).hexdigest()


def _check_hash(value):
    return bool(value and HASH_RE.fullmatch(value))


def _check_uuid(value):
    return bool(value and UUID_RE.fullmatch(value))


def _check_project_id(value):
    return bool(value and PROJECT_ID_RE.fullmatch(value))


def _psql(sql, variables=None, timeout=10):
    command = ["psql", "-X", "-q", "-A", "-t", "-F", "|", "-v", "ON_ERROR_STOP=1"]
    for name, value in (variables or {}).items():
        command.extend(("-v", name + "=" + value))
    try:
        result = subprocess.run(command, input=sql, capture_output=True, text=True,
                                timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ConsoleError("Company database is unavailable") from exc
    if result.returncode != 0:
        raise ConsoleError("Company database rejected the request")
    return [line for line in result.stdout.splitlines() if line != ""]


def _fields(line):
    return line.split("|")


def _text(value):
    return value if value not in ("", None) else None


def setup_status():
    rows = _psql("SELECT org_id, slug, state, admin_member_id, active_admins, "
                 "active_members, complete, next_step "
                 "FROM company.console_setup_status()")
    if not rows:
        raise ConsoleError("Company setup status is unavailable")
    org, slug, state, admin, admins, members, complete, next_step = _fields(rows[0])
    return {
        "org_id": _text(org),
        "slug": _text(slug),
        "state": state,
        "admin_member_id": _text(admin),
        "active_admins": int(admins or 0),
        "active_members": int(members or 0),
        "complete": complete == "t",
        "next_step": next_step,
    }


def start_setup(slug, project_user_id, code_hash):
    if not _check_hash(code_hash) or not _check_project_id(project_user_id):
        raise ConsoleError("Company setup input is invalid")
    rows = _psql(
        "SELECT decision, org_id, expires_at FROM company.console_start_setup("
        ":'slug', :'project', :'hash', interval '10 minutes')",
        {"slug": slug, "project": project_user_id, "hash": code_hash})
    if not rows:
        raise ConsoleError("Company setup could not start")
    decision, org_id, _expires = _fields(rows[0])[:3]
    return decision, _text(org_id)


def confirm_admin(org_id, project_user_id, code_hash):
    if not _check_uuid(org_id) or not _check_hash(code_hash):
        raise ConsoleError("Company setup confirmation is invalid")
    rows = _psql(
        "SELECT decision, member_id FROM company.console_confirm_admin("
        ":'org'::uuid, :'project', :'hash')",
        {"org": org_id, "project": project_user_id, "hash": code_hash})
    if not rows:
        raise ConsoleError("Company setup confirmation failed")
    decision, member_id = _fields(rows[0])[:2]
    return decision, _text(member_id)


def add_member(org_id, actor_id, project_user_id):
    if not _check_uuid(org_id) or not _check_uuid(actor_id):
        raise ConsoleError("Company mapping input is invalid")
    rows = _psql(
        "SELECT decision, member_id FROM company.console_add_member("
        ":'org'::uuid, :'actor'::uuid, :'project')",
        {"org": org_id, "actor": actor_id, "project": project_user_id})
    if not rows:
        raise ConsoleError("Company mapping failed")
    decision, member_id = _fields(rows[0])[:2]
    return decision, _text(member_id)


def complete_setup(org_id, actor_id):
    rows = _psql(
        "SELECT decision, mapped_members FROM company.console_complete_setup("
        ":'org'::uuid, :'actor'::uuid)",
        {"org": org_id, "actor": actor_id})
    if not rows:
        raise ConsoleError("Company setup completion failed")
    decision, mapped = _fields(rows[0])[:2]
    return decision, int(mapped or 0)


def begin_login(project_user_id, code_hash):
    if not _check_project_id(project_user_id) or not _check_hash(code_hash):
        raise ConsoleError("Company login input is invalid")
    rows = _psql(
        "SELECT decision, org_id, member_id, expires_at FROM "
        "company.console_begin_login(:'project', :'hash', interval '10 minutes')",
        {"project": project_user_id, "hash": code_hash})
    if not rows:
        raise ConsoleError("Company login could not start")
    decision, org_id, member_id, _expires = _fields(rows[0])[:4]
    return decision, _text(org_id), _text(member_id)


def open_session(org_id, member_id, token_hash, csrf_hash):
    rows = _psql(
        "SELECT decision, session_id, expires_at FROM company.console_open_session("
        ":'org'::uuid, :'member'::uuid, :'token', :'csrf', interval '8 hours')",
        {"org": org_id, "member": member_id, "token": token_hash, "csrf": csrf_hash})
    if not rows:
        raise ConsoleError("Company session could not be created")
    decision, _session_id, _expires = _fields(rows[0])[:3]
    return decision


def consume_login(project_user_id, code_hash, token_hash, csrf_hash):
    rows = _psql(
        "SELECT decision, org_id, member_id, role, expires_at FROM "
        "company.console_consume_login_code(:'project', :'hash', :'token', "
        ":'csrf', interval '8 hours')",
        {"project": project_user_id, "hash": code_hash, "token": token_hash,
         "csrf": csrf_hash})
    if not rows:
        raise ConsoleError("Company login failed")
    decision, org_id, member_id, role, _expires = _fields(rows[0])[:5]
    return decision, _text(org_id), _text(member_id), _text(role)


def validate_session(token_hash, csrf_hash, require_csrf):
    if not _check_hash(token_hash):
        return None
    rows = _psql(
        "SELECT decision, org_id, member_id, role, session_id, expires_at FROM "
        "company.console_validate_session(:'token', :'csrf', :'require'::boolean)",
        {"token": token_hash, "csrf": csrf_hash or "",
         "require": "true" if require_csrf else "false"})
    if not rows:
        return None
    decision, org_id, member_id, role, session_id, _expires = _fields(rows[0])[:6]
    if decision != "active" or not _check_uuid(org_id) or not _check_uuid(member_id):
        return None
    return {"org_id": org_id, "member_id": member_id, "role": role,
            "session_id": session_id}


def revoke_session(token_hash):
    if not _check_hash(token_hash):
        return "invalid"
    rows = _psql("SELECT company.console_revoke_session(:'token')",
                 {"token": token_hash})
    return rows[0] if rows else "invalid"


def dashboard(org_id, member_id, role):
    variables = {"the_org": org_id, "the_member": member_id}
    if role == "admin":
        member_sql = ("SELECT id, project_user_id, role, state "
                      "FROM company.members ORDER BY created_at, id")
        audit_sql = ("SELECT event_type, action, decision, at::text "
                     "FROM company.audit_events ORDER BY at DESC LIMIT 10")
    else:
        member_sql = ("SELECT id, project_user_id, role, state FROM company.members "
                      "WHERE id = :'the_member'::uuid")
        audit_sql = ("SELECT event_type, action, decision, at::text "
                     "FROM company.audit_events "
                     "WHERE actor_id = :'the_member'::uuid "
                     "OR owner_id = :'the_member'::uuid "
                     "ORDER BY at DESC LIMIT 10")
    query = (
        "BEGIN; "
        "SELECT set_config('company.org_id', :'the_org', true); "
        "SELECT set_config('company.member_id', :'the_member', true); "
        "SELECT 'M|' || coalesce(id::text, '') || '|' || project_user_id || '|' || "
        "role || '|' || state FROM (" + member_sql + ") AS members; "
        "SELECT 'I|' || provider || '|' || state || '|' || coalesce(reason, '') "
        "FROM company.integration_settings ORDER BY provider; "
        "SELECT 'P|' || action || '|' || risk || '|' || decision || '|' || version "
        "FROM company.policies ORDER BY action; "
        "SELECT 'A|' || event_type || '|' || action || '|' || decision || '|' || at "
        "FROM (" + audit_sql + ") AS audit; "
        "COMMIT;")
    rows = _psql(query, variables)
    result = {"members": [], "integrations": [], "policies": [], "audit": []}
    for row in rows:
        parts = _fields(row)
        if parts[0] == "M" and len(parts) >= 5:
            result["members"].append({"id": parts[1], "project_user_id": parts[2],
                                      "role": parts[3], "state": parts[4]})
        elif parts[0] == "I" and len(parts) >= 4:
            result["integrations"].append({"provider": parts[1], "state": parts[2],
                                           "reason": parts[3]})
        elif parts[0] == "P" and len(parts) >= 5:
            result["policies"].append({"action": parts[1], "risk": parts[2],
                                       "decision": parts[3], "version": parts[4]})
        elif parts[0] == "A" and len(parts) >= 5:
            result["audit"].append({"event_type": parts[1], "action": parts[2],
                                    "decision": parts[3], "at": parts[4]})
    return result


def _page(title, body, csrf=None):
    meta = f'<meta name="csrf-token" content="{html.escape(csrf)}">' if csrf else ""
    return (
        "<!doctype html><html lang=\"ko\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"{meta}<title>{html.escape(title)}</title>"
        "<style>body{font-family:system-ui,sans-serif;max-width:60rem;margin:2rem auto;"
        "padding:0 1rem;line-height:1.5}table{border-collapse:collapse;width:100%}"
        "th,td{border:1px solid #ccc;padding:.35rem .5rem;text-align:left}"
        "nav a{margin-right:1rem}label{display:block;margin:.75rem 0 .2rem}"
        "input,textarea,button{font:inherit;padding:.35rem}"
        ".notice{border:1px solid #888;padding:.5rem;margin:1rem 0}</style></head>"
        f"<body>{body}</body></html>"
    ).encode("utf-8")


def _setup_body(status, message="", csrf=None):
    notice = f'<p class="notice">{html.escape(message)}</p>' if message else ""
    csrf_field = (f'<input type="hidden" name="csrf_token" value="{html.escape(csrf)}">'
                  if csrf else "")
    if status["complete"]:
        return (notice + "<h1>Company 관리 콘솔</h1>"
                "<p>G10 초기 설정이 완료되었습니다.</p>"
                f"<p><a href=\"{LOGIN_PATH}\">관리 콘솔 로그인</a></p>")
    if status["state"] == "admin_created":
        return (notice + "<h1>G10 구성원 매핑</h1>"
                "<p>초기 관리자와 구성원별 Project 계정 ID를 연결합니다. "
                "실패한 항목은 다시 제출할 수 있으며 완료된 항목은 유지됩니다.</p>"
                "<form method=\"post\" action=\"/company/setup/mappings\">"
                "<label for=\"project_user_ids\">구성원 Project 계정 ID (줄바꿈 또는 쉼표)</label>"
                "<textarea id=\"project_user_ids\" name=\"project_user_ids\" "
                "rows=\"6\" cols=\"48\" required></textarea>"
                + csrf_field +
                "<p><button type=\"submit\">매핑 저장</button></p></form>")
    return (notice + "<h1>G10 초기 관리자 설정</h1>"
            "<p>초기 관리자와 구성원별 비서 소유자를 연결해야 합니다.</p>"
            "<form method=\"post\" action=\"/company/setup/start\">"
            "<label for=\"project_user_id\">관리자 Project 계정 ID</label>"
            "<input id=\"project_user_id\" name=\"project_user_id\" "
            "autocomplete=\"off\" required>"
            "<p><button type=\"submit\">Project DM으로 일회 코드 받기</button></p>"
            "</form>")


def _confirm_admin_body(project_user_id, message=""):
    notice = f'<p class="notice">{html.escape(message)}</p>' if message else ""
    return (notice + "<h1>G10 일회 코드 확인</h1>"
            "<p>Project DM으로 받은 10분짜리 코드를 입력하세요.</p>"
            "<form method=\"post\" action=\"/company/setup/confirm\">"
            f"<input type=\"hidden\" name=\"project_user_id\" "
            f"value=\"{html.escape(project_user_id)}\">"
            "<label for=\"code\">일회 코드</label>"
            "<input id=\"code\" name=\"code\" autocomplete=\"one-time-code\" "
            "inputmode=\"text\" required>"
            "<p><button type=\"submit\">관리자 계정 확정</button></p></form>")


def _login_body(message="", project_user_id=None):
    notice = f'<p class="notice">{html.escape(message)}</p>' if message else ""
    if project_user_id:
        return (notice + "<h1>Company 관리 콘솔 로그인</h1>"
                "<p>Project DM으로 받은 10분짜리 코드를 입력하세요.</p>"
                "<form method=\"post\" action=\"/company/login/confirm\">"
                f"<input type=\"hidden\" name=\"project_user_id\" "
                f"value=\"{html.escape(project_user_id)}\">"
                "<label for=\"code\">일회 코드</label>"
                "<input id=\"code\" name=\"code\" autocomplete=\"one-time-code\" required>"
                "<p><button type=\"submit\">로그인</button></p></form>")
    return (notice + "<h1>Company 관리 콘솔 로그인</h1>"
            "<form method=\"post\" action=\"/company/login/start\">"
            "<label for=\"project_user_id\">Project 계정 ID</label>"
            "<input id=\"project_user_id\" name=\"project_user_id\" "
            "autocomplete=\"username\" required>"
            "<p><button type=\"submit\">Project DM으로 일회 코드 받기</button></p>"
            "</form>")


def _console_body(session, data, status, csrf, message=""):
    admin = session["role"] == "admin"
    notice = f'<p class="notice">{html.escape(message)}</p>' if message else ""
    tabs = ["members", "integrations", "policies", "status"]
    nav = " ".join(
        f'<a href="{CONSOLE_PATH}?tab={name}">'
        f'{"구성원" if name == "members" else "연동" if name == "integrations" else "정책" if name == "policies" else "상태"}</a>'
        for name in tabs)
    rows = []
    if not admin:
        rows.append("<p>구성원 화면은 본인 비서·연결 상태만 표시합니다.</p>")
    rows.append("<table><thead><tr><th>Project 계정</th><th>역할</th><th>상태</th>"
                "</tr></thead><tbody>")
    for member in data["members"]:
        rows.append("<tr><td>" + html.escape(member["project_user_id"]) + "</td><td>"
                    + html.escape(member["role"]) + "</td><td>"
                    + html.escape(member["state"]) + "</td></tr>")
    rows.append("</tbody></table>")
    rows.append("<h2>연동</h2><table><thead><tr><th>공급자</th><th>선택</th>"
                "<th>사유</th></tr></thead><tbody>")
    for item in data["integrations"]:
        rows.append("<tr><td>" + html.escape(item["provider"]) + "</td><td>"
                    + html.escape(item["state"]) + "</td><td>"
                    + html.escape(item["reason"]) + "</td></tr>")
    rows.append("</tbody></table>")
    rows.append("<h2>정책</h2><table><thead><tr><th>작업</th><th>위험</th>"
                "<th>결정</th><th>버전</th></tr></thead><tbody>")
    for item in data["policies"]:
        rows.append("<tr><td>" + html.escape(item["action"]) + "</td><td>"
                    + html.escape(item["risk"]) + "</td><td>"
                    + html.escape(item["decision"]) + "</td><td>"
                    + html.escape(item["version"]) + "</td></tr>")
    rows.append("</tbody></table>")
    rows.append("<h2>상태</h2><p>서비스: 정상 · G10: "
                + ("완료" if status["complete"] else "대기")
                + " · 구성원 " + str(status["active_members"])
                + "명 · 관리자 " + str(status["active_admins"]) + "명</p>")
    rows.append("<table><thead><tr><th>감사 사건</th><th>작업</th><th>결정</th>"
                "<th>시각</th></tr></thead><tbody>")
    for item in data["audit"]:
        rows.append("<tr><td>" + html.escape(item["event_type"]) + "</td><td>"
                    + html.escape(item["action"]) + "</td><td>"
                    + html.escape(item["decision"]) + "</td><td>"
                    + html.escape(item["at"]) + "</td></tr>")
    rows.append("</tbody></table>")
    rows.append("<form method=\"post\" action=\"/company/logout\">"
                f"<input type=\"hidden\" name=\"csrf_token\" value=\"{html.escape(csrf)}\">"
                "<p><button type=\"submit\">로그아웃</button></p></form>")
    body = (notice + "<h1>Company 관리 콘솔</h1><nav>" + nav + "</nav>"
            "<h2>구성원</h2>" + "".join(rows))
    return _page("Company 관리 콘솔", body, csrf)


class ConsoleHandler(BaseHTTPRequestHandler):
    server_version = "OAOSCompany/1.0"

    def log_message(self, _format, *_args):
        pass

    def _send(self, status, body, headers=None, set_cookie=None):
        headers = dict(headers or {})
        content_type = headers.pop("Content-Type", "text/html; charset=utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; style-src 'unsafe-inline'; "
                         "form-action 'self'; frame-ancestors 'none'")
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location, set_cookie=None):
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _cookie(self):
        raw = self.headers.get("Cookie", "")
        for part in raw.split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE_NAME and value:
                return value
        return ""

    def _cookie_header(self, token):
        secure = os.environ.get("OAOS_COMPANY_COOKIE_SECURE", "1") != "0"
        parts = [COOKIE_NAME + "=" + token, "Path=/company", "HttpOnly",
                 "SameSite=Strict", "Max-Age=28800"]
        if secure:
            parts.append("Secure")
        return "; ".join(parts)

    def _clear_cookie(self):
        return COOKIE_NAME + "=; Path=/company; HttpOnly; SameSite=Strict; Max-Age=0"

    def _session(self, require_csrf=False, csrf_value=None):
        token = self._cookie()
        if not token:
            return None
        try:
            secret = _console_secret()
            expected = csrf_for(secret, token)
            if require_csrf and not hmac.compare_digest(expected, csrf_value or ""):
                return None
            return validate_session(_sha256(token), _sha256(expected), require_csrf)
        except ConsoleError:
            return None

    def _same_origin(self):
        origin = self.headers.get("Origin")
        if not origin:
            return True
        allowed = os.environ.get("OAOS_COMPANY_HTTPS_ORIGIN", "").rstrip("/")
        return bool(allowed) and origin.rstrip("/") == allowed

    def _form(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if length <= 0 or length > MAX_FORM_BYTES:
            return None
        if self.headers.get("Content-Type", "").split(";")[0] != "application/x-www-form-urlencoded":
            return None
        raw = self.rfile.read(length).decode("utf-8", "replace")
        return {key: values[-1] for key, values in parse_qs(raw, keep_blank_values=True).items()}

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == HEALTH_PATH:
            self._send(200, b"ok\n", {"Content-Type": "text/plain; charset=utf-8"})
            return
        if path == SETUP_PATH:
            try:
                status = setup_status()
                csrf = None
                if status["state"] == "admin_created":
                    session = self._session()
                    if session and session["role"] == "admin":
                        csrf = csrf_for(_console_secret(), self._cookie())
                self._send(200, _page("G10 Company 설정",
                                      _setup_body(status, csrf=csrf)))
            except ConsoleError:
                self._send(503, _page("G10 Company 설정",
                                      "<p>Company 데이터베이스를 사용할 수 없습니다.</p>"))
            return
        if path == LOGIN_PATH:
            self._send(200, _page("Company 로그인", _login_body()))
            return
        if path == "/company/setup/status":
            try:
                status = setup_status()
                safe = {key: status[key] for key in
                        ("state", "complete", "next_step", "active_admins", "active_members")}
                self._send(200, json.dumps(safe, ensure_ascii=False).encode("utf-8"),
                           {"Content-Type": "application/json; charset=utf-8"})
            except ConsoleError:
                self._send(503, b'{"state":"unavailable"}',
                           {"Content-Type": "application/json; charset=utf-8"})
            return
        if path == CONSOLE_PATH:
            session = self._session()
            if not session:
                self._redirect(LOGIN_PATH)
                return
            try:
                secret = _console_secret()
                token = self._cookie()
                csrf = csrf_for(secret, token)
                data = dashboard(session["org_id"], session["member_id"], session["role"])
                status = setup_status()
                self._send(200, _console_body(session, data, status, csrf))
            except ConsoleError:
                self._send(503, _page("Company 관리 콘솔",
                                      "<p>Company 상태를 읽을 수 없습니다.</p>"))
            return
        self._send(404, b"not found\n", {"Content-Type": "text/plain; charset=utf-8"})

    def do_POST(self):
        if not self._same_origin():
            self._send(403, _page("Company", "<p>요청 출처를 확인할 수 없습니다.</p>"))
            return
        path = urlsplit(self.path).path
        form = self._form()
        if form is None:
            self._send(400, _page("Company", "<p>요청 형식이 올바르지 않습니다.</p>"))
            return
        try:
            if path == "/company/setup/start":
                self._setup_start(form)
            elif path == "/company/setup/confirm":
                self._setup_confirm(form)
            elif path == "/company/setup/mappings":
                self._setup_mappings(form)
            elif path == "/company/login/start":
                self._login_start(form)
            elif path == "/company/login/confirm":
                self._login_confirm(form)
            elif path == "/company/logout":
                self._logout(form)
            else:
                self._send(404, b"not found\n", {"Content-Type": "text/plain; charset=utf-8"})
        except ConsoleError:
            self._send(503, _page("Company", "<p>요청을 처리할 수 없습니다. "
                                            "같은 단계에서 다시 시도하세요.</p>"))
        except ProjectAPIError:
            self._send(503, _page("Company", "<p>Project API를 확인할 수 없습니다. "
                                            "G10은 대기 상태로 유지됩니다.</p>"))

    def _project(self):
        return ProjectAPI.from_environment()

    def _new_code(self):
        return secrets.token_hex(4).upper()

    def _setup_start(self, form):
        project_user_id = form.get("project_user_id", "")
        if not _check_project_id(project_user_id):
            self._send(400, _page("G10", _setup_body(setup_status(),
                                                     "Project 계정 ID를 확인하세요.")))
            return
        project = self._project()
        project.require_admin(project_user_id)
        code = self._new_code()
        slug = os.environ.get("OAOS_COMPANY_ORG_SLUG", "company")
        decision, org_id = start_setup(slug, project_user_id, _sha256(code))
        if decision == "issued":
            project.send_direct_message(
                project_user_id,
                "OAOS Company G10 초기 관리자 인증 코드: " + code + " (10분)")
            self._send(200, _page("G10 일회 코드", _confirm_admin_body(project_user_id)))
        elif decision == "locked":
            self._send(429, _page("G10", _setup_body(setup_status(),
                                                     "잠시 후 다시 시도하세요.")))
        else:
            self._send(409, _page("G10", _setup_body(setup_status(),
                                                     "이미 완료되었거나 시작할 수 없습니다.")))

    def _setup_confirm(self, form):
        project_user_id = form.get("project_user_id", "")
        code = form.get("code", "")
        if not _check_project_id(project_user_id) or not code:
            self._send(400, _page("G10", _confirm_admin_body(project_user_id,
                                                             "코드를 확인하세요.")))
            return
        status = setup_status()
        if not status["org_id"]:
            self._send(409, _page("G10", _setup_body(status, "먼저 관리자 코드를 요청하세요.")))
            return
        decision, member_id = confirm_admin(status["org_id"], project_user_id, _sha256(code))
        if decision != "admin_created" or not member_id:
            message = "코드가 만료되었거나 일치하지 않습니다. 다시 요청하세요."
            self._send(400, _page("G10", _confirm_admin_body(project_user_id, message)))
            return
        secret = _console_secret()
        token = secrets.token_urlsafe(32)
        csrf = csrf_for(secret, token)
        if open_session(status["org_id"], member_id, _sha256(token), _sha256(csrf)) != "created":
            self._send(503, _page("G10", "<p>세션을 만들 수 없습니다.</p>"))
            return
        self._redirect(SETUP_PATH, self._cookie_header(token))

    def _setup_mappings(self, form):
        session = self._session(require_csrf=True, csrf_value=form.get("csrf_token"))
        if not session or session["role"] != "admin":
            self._send(403, _page("G10", "<p>관리자 세션이 필요합니다.</p>"))
            return
        project = self._project()
        raw = form.get("project_user_ids", "").replace(",", "\n")
        requested = [item.strip() for item in raw.splitlines() if item.strip()]
        saved, failed = [], []
        for project_user_id in requested:
            if not _check_project_id(project_user_id):
                failed.append(project_user_id)
                continue
            try:
                project.require_member(project_user_id)
            except ProjectAPIError:
                failed.append(project_user_id)
                continue
            decision, _member_id = add_member(session["org_id"], session["member_id"],
                                              project_user_id)
            if decision in ("created", "exists"):
                saved.append(project_user_id)
            else:
                failed.append(project_user_id)
        status = setup_status()
        completed = "incomplete"
        if status["active_members"] >= 2:
            completed, _count = complete_setup(session["org_id"], session["member_id"])
        message = "저장됨: " + (", ".join(saved) if saved else "없음")
        if failed:
            message += " · 실패(같은 단계에서 재시도): " + ", ".join(failed)
        if completed == "complete":
            message += " · G10 완료"
        data = dashboard(session["org_id"], session["member_id"], session["role"])
        secret = _console_secret()
        csrf = csrf_for(secret, self._cookie())
        body = _console_body(session, data, setup_status(), csrf, message)
        self._send(200, body)

    def _login_start(self, form):
        project_user_id = form.get("project_user_id", "")
        if not _check_project_id(project_user_id):
            self._send(400, _page("Company 로그인", _login_body("계정 ID를 확인하세요.")))
            return
        project = self._project()
        code = self._new_code()
        decision, _org_id, _member_id = begin_login(project_user_id, _sha256(code))
        if decision == "issued":
            project.send_direct_message(
                project_user_id,
                "OAOS Company 관리 콘솔 일회 코드: " + code + " (10분)")
            self._send(200, _page("Company 로그인", _login_body("", project_user_id)))
        elif decision == "locked":
            self._send(429, _page("Company 로그인", _login_body("잠시 후 다시 시도하세요.")))
        else:
            self._send(401, _page("Company 로그인",
                                  _login_body("등록된 활성 계정에만 코드를 보냅니다.")))

    def _login_confirm(self, form):
        project_user_id = form.get("project_user_id", "")
        code = form.get("code", "")
        if not _check_project_id(project_user_id) or not code:
            self._send(400, _page("Company 로그인",
                                  _login_body("코드를 확인하세요.", project_user_id)))
            return
        secret = _console_secret()
        token = secrets.token_urlsafe(32)
        csrf = csrf_for(secret, token)
        decision, _org_id, _member_id, _role = consume_login(
            project_user_id, _sha256(code), _sha256(token), _sha256(csrf))
        if decision != "authenticated":
            self._send(401, _page("Company 로그인",
                                  _login_body("코드가 만료되었거나 일치하지 않습니다.",
                                              project_user_id)))
            return
        self._redirect(CONSOLE_PATH, self._cookie_header(token))

    def _logout(self, form):
        session = self._session(require_csrf=True, csrf_value=form.get("csrf_token"))
        if not session:
            self._redirect(LOGIN_PATH)
            return
        revoke_session(_sha256(self._cookie()))
        self._redirect(LOGIN_PATH, self._clear_cookie())


def main():
    try:
        _console_secret()
    except ConsoleError:
        print("Company console secret is missing.", file=sys.stderr)
        return 3
    server = ThreadingHTTPServer(("127.0.0.1", 8765), ConsoleHandler)
    server.daemon_threads = True
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
