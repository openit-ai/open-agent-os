"use client";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState, useRef } from "react";
import { LayoutDashboard, Server, Users, Shield, ClipboardCheck, ScrollText, KeyRound, LogOut, BadgeCheck, ShieldAlert, DatabaseBackup, Image as ImageIcon, UserCog, ChevronDown } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { cn } from "@/lib/utils";
import { clearToken, getToken, getAvatarUrl, setAvatarUrl, clearAvatarUrl, changePassword, updateProfile, getMe, listUsers, login, setToken, type AdminUserPublic } from "@/lib/api";

const navItems = [
  { href: "/", label: "Dashboard", icon: LayoutDashboard },
  { href: "/infra", label: "Infra 관리", icon: Server },
  { href: "/users", label: "Users / Agents", icon: Users },
  { href: "/policy", label: "Policy", icon: Shield },
  { href: "/approvals", label: "Approvals", icon: ClipboardCheck },
  { href: "/audit", label: "Audit", icon: ScrollText },
  { href: "/credentials", label: "Credentials", icon: KeyRound },
  { href: "/license", label: "License", icon: BadgeCheck },
  { href: "/security-updates", label: "Security Updates", icon: ShieldAlert },
  { href: "/backup", label: "Backup / Upgrade", icon: DatabaseBackup },
];

export default function DashboardLayout({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const [email, setEmail] = useState<string | null>(null);
  const [displayName, setDisplayName] = useState<string | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [dropdownOpen, setDropdownOpen] = useState(false);
  const [avatarUrl, setAvatarUrlState] = useState<string | null>(null);

  const [showPwdModal, setShowPwdModal] = useState(false);
  const [showIconModal, setShowIconModal] = useState(false);
  const [showAdminModal, setShowAdminModal] = useState(false);

  const dropdownRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const token = getToken();
    if (!token) { router.replace("/login"); return; }
    try {
      const payload = JSON.parse(atob(token.split(".")[1] ?? ""));
      setEmail(payload.sub ?? payload.email ?? "admin");
    } catch { setEmail("admin"); }
    setAvatarUrlState(getAvatarUrl());
    getMe().then((me) => { setDisplayName(me.display_name); setEmail(me.email); }).catch(() => {});
  }, [router]);

  useEffect(() => {
    function onClickOutside(e: MouseEvent) {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target as Node)) setDropdownOpen(false);
    }
    if (dropdownOpen) document.addEventListener("mousedown", onClickOutside);
    return () => document.removeEventListener("mousedown", onClickOutside);
  }, [dropdownOpen]);

  function handleLogout() {
    clearToken();
    router.replace("/login");
  }

  return (
    <div className="flex min-h-screen">
      {/* Sidebar desktop */}
      <aside className="hidden w-60 shrink-0 border-r bg-card md:flex md:flex-col">
        <div className="flex h-14 items-center border-b px-4">
          <span className="text-sm font-semibold">Open Agent OS</span>
          <span className="ml-2 rounded bg-secondary px-2 py-0.5 text-xs">Admin</span>
        </div>
        <nav className="flex-1 space-y-1 p-3">
          {navItems.map((item) => {
            const active = pathname === item.href;
            return (
              <Link key={item.href} href={item.href} className={cn("flex items-center gap-2 rounded-md px-3 py-2 text-sm transition-colors", active ? "bg-primary text-primary-foreground" : "hover:bg-accent")}>
                <item.icon className="h-4 w-4" />{item.label}
              </Link>
            );
          })}
        </nav>
        <div className="border-t px-4 py-3">
          <p className="text-[11px] leading-4 text-muted-foreground">© 2026 오픈잇. All rights reserved.</p>
          <p className="text-[11px] text-muted-foreground/70">Open Agent OS v1.5.1</p>
        </div>
      </aside>

      <div className="flex flex-1 flex-col">
        {/* Header */}
        <header className="flex h-14 items-center justify-between border-b bg-card px-4">
          <div className="flex items-center gap-2">
            <Button variant="ghost" size="icon" className="md:hidden" onClick={() => setDrawerOpen(!drawerOpen)} aria-label="메뉴">☰</Button>
            <span className="text-sm font-medium md:hidden">Open Agent OS</span>
          </div>
          <div className="flex items-center gap-3">
            <span className="text-sm text-muted-foreground hidden sm:inline">{displayName ?? email ?? ""}</span>
            <div className="relative" ref={dropdownRef}>
              <button
                onClick={() => setDropdownOpen(!dropdownOpen)}
                className="flex items-center gap-1.5 rounded-full p-0.5 pr-1 hover:bg-accent"
                aria-label="관리자 메뉴"
              >
                {avatarUrl ? (
                  <img src={avatarUrl} alt="avatar" className="h-8 w-8 rounded-full object-cover border" />
                ) : (
                  <span className="flex h-8 w-8 items-center justify-center rounded-full bg-primary text-xs font-medium text-primary-foreground">{(displayName?.[0] ?? email?.[0] ?? "A").toUpperCase()}</span>
                )}
                <ChevronDown className="h-3 w-3 text-muted-foreground" />
              </button>
              {dropdownOpen && (
                <div className="absolute right-0 z-50 mt-2 w-56 rounded-md border bg-popover p-1 shadow-md">
                  <div className="px-3 py-2 border-b mb-1">
                    <p className="text-sm font-medium truncate">{displayName ?? "관리자"}</p>
                    <p className="text-xs text-muted-foreground truncate">{email}</p>
                  </div>
                  <button onClick={() => { setDropdownOpen(false); setShowAdminModal(true); }} className="flex w-full items-center gap-2 rounded-sm px-3 py-2 text-sm hover:bg-accent text-left">
                    <UserCog className="h-4 w-4" /> 관리자 변경
                  </button>
                  <button onClick={() => { setDropdownOpen(false); setShowIconModal(true); }} className="flex w-full items-center gap-2 rounded-sm px-3 py-2 text-sm hover:bg-accent text-left">
                    <ImageIcon className="h-4 w-4" /> 아이콘 이미지 변경
                  </button>
                  <button onClick={() => { setDropdownOpen(false); setShowPwdModal(true); }} className="flex w-full items-center gap-2 rounded-sm px-3 py-2 text-sm hover:bg-accent text-left">
                    <KeyRound className="h-4 w-4" /> 패스워드 변경
                  </button>
                  <div className="my-1 border-t" />
                  <button onClick={handleLogout} className="flex w-full items-center gap-2 rounded-sm px-3 py-2 text-sm hover:bg-accent text-left text-destructive">
                    <LogOut className="h-4 w-4" /> 로그아웃
                  </button>
                </div>
              )}
            </div>
            <Button variant="ghost" size="sm" onClick={handleLogout} className="hidden sm:inline-flex"><LogOut className="mr-1 h-4 w-4" />로그아웃</Button>
          </div>
        </header>

        {/* Mobile drawer */}
        {drawerOpen && (
          <div className="border-b bg-card p-3 md:hidden">
            <nav className="space-y-1">
              {navItems.map((item) => {
                const active = pathname === item.href;
                return (
                  <Link key={item.href} href={item.href} onClick={() => setDrawerOpen(false)} className={cn("flex items-center gap-2 rounded-md px-3 py-2 text-sm", active ? "bg-primary text-primary-foreground" : "hover:bg-accent")}>
                    <item.icon className="h-4 w-4" />{item.label}
                  </Link>
                );
              })}
            </nav>
            <div className="mt-3 border-t pt-3">
              <p className="text-[11px] text-muted-foreground">© 2026 오픈잇. All rights reserved.</p>
            </div>
          </div>
        )}

        <main className="flex-1 bg-muted/20 p-4 md:p-6">{children}</main>
      </div>

      {showPwdModal && <PasswordModal onClose={() => setShowPwdModal(false)} />}
      {showIconModal && <IconModal currentUrl={avatarUrl} onClose={() => setShowIconModal(false)} onSave={(url) => { if (url) setAvatarUrl(url); else clearAvatarUrl(); setAvatarUrlState(url); setShowIconModal(false); }} />}
      {showAdminModal && <AdminModal email={email} onClose={() => setShowAdminModal(false)} onSwitched={() => { try { const t = getToken(); const p = JSON.parse(atob((t ?? "").split(".")[1] ?? "")); setEmail(p.sub ?? p.email ?? email); } catch {} getMe().then((m) => { setDisplayName(m.display_name); setEmail(m.email); }).catch(() => {}); setAvatarUrlState(getAvatarUrl()); setShowAdminModal(false); }} />}
    </div>
  );
}

function PasswordModal({ onClose }: { onClose: () => void }) {
  const [cur, setCur] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  async function handleSubmit() {
    setErr(null); setMsg(null);
    if (next.length < 8) { setErr("새 비밀번호는 8자 이상이어야 합니다."); return; }
    if (next !== confirm) { setErr("새 비밀번호 확인이 일치하지 않습니다."); return; }
    setSaving(true);
    try { await changePassword(cur, next); setMsg("비밀번호가 변경되었습니다."); setCur(""); setNext(""); setConfirm(""); } catch (e) { setErr(e instanceof Error ? e.message : "변경 실패"); } finally { setSaving(false); }
  }
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onClick={onClose}>
      <div className="w-full max-w-sm rounded-lg bg-card p-5 shadow-lg border" onClick={(e) => e.stopPropagation()}>
        <h3 className="text-base font-semibold flex items-center gap-2"><KeyRound className="h-4 w-4" /> 패스워드 변경</h3>
        <p className="text-xs text-muted-foreground mt-1">현재 비밀번호 확인 후 새 비밀번호로 변경합니다.</p>
        <div className="mt-4 space-y-3">
          <div><Label className="text-xs">현재 비밀번호</Label><Input type="password" value={cur} onChange={(e) => setCur(e.target.value)} placeholder="현재 비밀번호" /></div>
          <div><Label className="text-xs">새 비밀번호 (8자 이상)</Label><Input type="password" value={next} onChange={(e) => setNext(e.target.value)} placeholder="새 비밀번호" /></div>
          <div><Label className="text-xs">새 비밀번호 확인</Label><Input type="password" value={confirm} onChange={(e) => setConfirm(e.target.value)} placeholder="새 비밀번호 확인" /></div>
          {err && <p className="text-xs text-destructive">{err}</p>}
          {msg && <p className="text-xs text-green-600">{msg}</p>}
        </div>
        <div className="mt-5 flex justify-end gap-2">
          <Button variant="outline" size="sm" onClick={onClose}>닫기</Button>
          <Button size="sm" onClick={handleSubmit} disabled={saving}>{saving ? "변경 중..." : "변경"}</Button>
        </div>
      </div>
    </div>
  );
}

function IconModal({ currentUrl, onClose, onSave }: { currentUrl: string | null; onClose: () => void; onSave: (url: string | null) => void }) {
  const [preview, setPreview] = useState<string | null>(currentUrl);
  const [err, setErr] = useState<string | null>(null);
  function onFile(e: React.ChangeEvent<HTMLInputElement>) {
    setErr(null);
    const f = e.target.files?.[0];
    if (!f) return;
    if (f.size > 2 * 1024 * 1024) { setErr("2MB 이하 이미지만 가능합니다."); return; }
    if (!f.type.startsWith("image/")) { setErr("이미지 파일만 가능합니다."); return; }
    const reader = new FileReader();
    reader.onload = () => setPreview(reader.result as string);
    reader.readAsDataURL(f);
  }
  function handleSave() {
    if (preview) setAvatarUrl(preview);
    else clearAvatarUrl();
    onSave(preview);
  }
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onClick={onClose}>
      <div className="w-full max-w-sm rounded-lg bg-card p-5 shadow-lg border" onClick={(e) => e.stopPropagation()}>
        <h3 className="text-base font-semibold flex items-center gap-2"><ImageIcon className="h-4 w-4" /> 아이콘 이미지 변경</h3>
        <p className="text-xs text-muted-foreground mt-1">프로필 아이콘을 이미지로 변경합니다. (브라우저 저장, 2MB 이하)</p>
        <div className="mt-4 flex flex-col items-center gap-3">
          <div className="h-20 w-20 rounded-full border bg-muted flex items-center justify-center overflow-hidden">
            {preview ? <img src={preview} alt="preview" className="h-full w-full object-cover" /> : <span className="text-xs text-muted-foreground">없음</span>}
          </div>
          <Input type="file" accept="image/*" onChange={onFile} className="text-xs" />
          {err && <p className="text-xs text-destructive">{err}</p>}
        </div>
        <div className="mt-5 flex justify-between">
          <Button variant="ghost" size="sm" onClick={() => { setPreview(null); }}>이미지 제거</Button>
          <div className="flex gap-2">
            <Button variant="outline" size="sm" onClick={onClose}>취소</Button>
            <Button size="sm" onClick={handleSave}>저장</Button>
          </div>
        </div>
      </div>
    </div>
  );
}

function AdminModal({ email, onClose, onSwitched }: { email: string | null; onClose: () => void; onSwitched: () => void }) {
  const [users, setUsers] = useState<AdminUserPublic[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [switchEmail, setSwitchEmail] = useState("");
  const [switchPw, setSwitchPw] = useState("");
  const [switchErr, setSwitchErr] = useState<string | null>(null);
  const [switching, setSwitching] = useState(false);
  const [editName, setEditName] = useState("");
  const [editMsg, setEditMsg] = useState<string | null>(null);
  const [editErr, setEditErr] = useState<string | null>(null);

  useEffect(() => {
    listUsers().then((res) => {
      const arr = Array.isArray(res) ? res : (res as { users: AdminUserPublic[] }).users ?? [];
      setUsers(arr);
      const me = arr.find((u) => u.email === email);
      if (me) setEditName(me.display_name);
    }).catch((e) => setErr(e instanceof Error ? e.message : "조회 실패")).finally(() => setLoading(false));
  }, [email]);

  async function handleSwitch() {
    setSwitchErr(null);
    if (!switchEmail || !switchPw) { setSwitchErr("이메일과 비밀번호를 입력하세요."); return; }
    setSwitching(true);
    try { const r = await login(switchEmail, switchPw); setToken(r.access_token); onSwitched(); } catch (e) { setSwitchErr(e instanceof Error ? e.message : "로그인 실패"); } finally { setSwitching(false); }
  }
  async function handleEditName() {
    setEditErr(null); setEditMsg(null);
    if (!editName.trim()) { setEditErr("표시 이름을 입력하세요."); return; }
    try { const me = await updateProfile(editName.trim()); setEditMsg(`표시 이름이 '${me.display_name}' 로 변경되었습니다.`); setUsers((prev) => prev.map((u) => u.email === me.email ? me : u)); } catch (e) { setEditErr(e instanceof Error ? e.message : "변경 실패"); }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onClick={onClose}>
      <div className="w-full max-w-lg rounded-lg bg-card p-5 shadow-lg border max-h-[90vh] overflow-auto" onClick={(e) => e.stopPropagation()}>
        <h3 className="text-base font-semibold flex items-center gap-2"><UserCog className="h-4 w-4" /> 관리자 변경</h3>
        <p className="text-xs text-muted-foreground mt-1">현재 로그인: <span className="font-medium text-foreground">{email}</span> — 다른 계정으로 전환하거나 표시 이름을 변경합니다.</p>

        <div className="mt-4 space-y-4">
          <div className="rounded-md border p-3">
            <p className="text-xs font-medium">표시 이름 변경</p>
            <div className="mt-2 flex gap-2">
              <Input value={editName} onChange={(e) => setEditName(e.target.value)} placeholder="표시 이름" className="text-sm" />
              <Button size="sm" onClick={handleEditName}>변경</Button>
            </div>
            {editErr && <p className="text-xs text-destructive mt-1">{editErr}</p>}
            {editMsg && <p className="text-xs text-green-600 mt-1">{editMsg}</p>}
          </div>

          <div className="rounded-md border p-3">
            <p className="text-xs font-medium">계정 전환</p>
            <p className="text-xs text-muted-foreground">다른 관리자 이메일/비밀번호로 로그인하여 전환합니다.</p>
            <div className="mt-2 space-y-2">
              <Input value={switchEmail} onChange={(e) => setSwitchEmail(e.target.value)} placeholder="이메일 (예: admin@openit.co.kr)" />
              <Input type="password" value={switchPw} onChange={(e) => setSwitchPw(e.target.value)} placeholder="비밀번호" />
              {switchErr && <p className="text-xs text-destructive">{switchErr}</p>}
              <Button size="sm" onClick={handleSwitch} disabled={switching} className="w-full">{switching ? "전환 중..." : "전환"}</Button>
            </div>
          </div>

          <div>
            <p className="text-xs font-medium mb-2">등록된 관리자 ({users.length})</p>
            {loading ? <p className="text-xs text-muted-foreground">로딩 중...</p> : err ? <p className="text-xs text-destructive">{err}</p> : (
              <div className="rounded-md border divide-y max-h-40 overflow-auto">
                {users.map((u) => (
                  <div key={u.id} className="flex items-center justify-between px-3 py-2">
                    <div><p className="text-xs font-medium">{u.display_name} <span className="text-[11px] text-muted-foreground">({u.role})</span></p><p className="text-xs text-muted-foreground">{u.email}</p></div>
                    {u.email === email && <span className="text-[11px] rounded bg-primary text-primary-foreground px-2 py-0.5">현재</span>}
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>

        <div className="mt-5 flex justify-end">
          <Button variant="outline" size="sm" onClick={onClose}>닫기</Button>
        </div>
      </div>
    </div>
  );
}
