"use client";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState, useRef } from "react";
import { LayoutDashboard, Server, Users, Shield, ClipboardCheck, ScrollText, KeyRound, LogOut, BadgeCheck, ShieldAlert, DatabaseBackup, Image as ImageIcon, UserCog, ChevronDown, Globe, Cpu, BarChart3, Layers, Settings2, Gauge, Database, Lock, Flag } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { cn } from "@/lib/utils";
import { clearToken, getToken, getAvatarUrl, setAvatarUrl, clearAvatarUrl, changePassword, updateProfile, getMe, listUsers, login, setToken, type AdminUserPublic } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { VersionDisplay } from "@/components/VersionDisplay";

export default function DashboardLayout({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const { t, lang, setLang } = useI18n();
  const [email, setEmail] = useState<string | null>(null);
  const [displayName, setDisplayName] = useState<string | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [dropdownOpen, setDropdownOpen] = useState(false);
  const [avatarUrl, setAvatarUrlState] = useState<string | null>(null);

  const [showPwdModal, setShowPwdModal] = useState(false);
  const [showIconModal, setShowIconModal] = useState(false);
  const [showAdminModal, setShowAdminModal] = useState(false);

  const dropdownRef = useRef<HTMLDivElement>(null);

  const navItems = [
    { href: "/", label: t("nav.dashboard"), icon: LayoutDashboard },
    { href: "/infra", label: t("nav.infra"), icon: Server },
    { href: "/providers", label: t("nav.providers"), icon: Cpu },
    { href: "/fallback", label: t("nav.fallback"), icon: Layers },
    { href: "/runtime-config", label: t("nav.runtimeConfig"), icon: Settings2 },
    { href: "/control/acp", label: t("nav.controlAcp"), icon: Globe },
    { href: "/control/runtime", label: t("nav.controlRuntime"), icon: Settings2 },
    { href: "/execution/mcp", label: t("nav.executionMcp"), icon: Database },
    { href: "/llm-usage", label: t("nav.llmUsage"), icon: BarChart3 },
    { href: "/quota", label: t("nav.quota"), icon: Gauge },
    { href: "/embedding", label: t("nav.embedding"), icon: Database },
    { href: "/secrets", label: t("nav.secrets"), icon: Lock },
    { href: "/feature-flags", label: t("nav.featureFlags"), icon: Flag },
    { href: "/profile-ops", label: t("nav.profileOps"), icon: UserCog },
    { href: "/knowledge-ops", label: t("nav.knowledgeOps"), icon: Database },
    { href: "/users", label: t("nav.users"), icon: Users },
    { href: "/policy", label: t("nav.policy"), icon: Shield },
    { href: "/approvals", label: t("nav.approvals"), icon: ClipboardCheck },
    { href: "/audit", label: t("nav.audit"), icon: ScrollText },
    { href: "/credentials", label: t("nav.credentials"), icon: KeyRound },
    { href: "/license", label: t("nav.license"), icon: BadgeCheck },
    { href: "/security-updates", label: t("nav.securityUpdates"), icon: ShieldAlert },
    { href: "/backup", label: t("nav.backup"), icon: DatabaseBackup },
  ];

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
          <VersionDisplay />
        </div>
      </aside>

      <div className="flex flex-1 flex-col">
        {/* Header */}
        <header className="flex h-14 items-center justify-between border-b bg-card px-4">
          <div className="flex items-center gap-2">
            <Button variant="ghost" size="icon" className="md:hidden" onClick={() => setDrawerOpen(!drawerOpen)} aria-label={t("header.menu")}>☰</Button>
            <span className="text-sm font-medium md:hidden">Open Agent OS</span>
          </div>
          <div className="flex items-center gap-3">
            <span className="text-sm text-muted-foreground hidden sm:inline">{displayName ?? email ?? ""}</span>
            <div className="relative" ref={dropdownRef}>
              <button
                onClick={() => setDropdownOpen(!dropdownOpen)}
                className="flex items-center gap-1.5 rounded-full p-0.5 pr-1 hover:bg-accent"
                aria-label={t("header.adminMenu")}
              >
                {avatarUrl ? (
                  <img src={avatarUrl} alt="avatar" className="h-8 w-8 rounded-full object-cover border" />
                ) : (
                  <span className="flex h-8 w-8 items-center justify-center rounded-full bg-primary text-xs font-medium text-primary-foreground">{(displayName?.[0] ?? email?.[0] ?? "A").toUpperCase()}</span>
                )}
                <ChevronDown className="h-3 w-3 text-muted-foreground" />
              </button>
              {dropdownOpen && (
                <div className="absolute right-0 z-50 mt-2 w-64 rounded-md border bg-popover p-1 shadow-md">
                  <div className="px-3 py-2 border-b mb-1">
                    <p className="text-sm font-medium truncate">{displayName ?? "Admin"}</p>
                    <p className="text-xs text-muted-foreground truncate">{email}</p>
                  </div>
                  <button onClick={() => { setDropdownOpen(false); setShowAdminModal(true); }} className="flex w-full items-center gap-2 rounded-sm px-3 py-2 text-sm hover:bg-accent text-left">
                    <UserCog className="h-4 w-4" /> {t("header.switchAdmin")}
                  </button>
                  <button onClick={() => { setDropdownOpen(false); setShowIconModal(true); }} className="flex w-full items-center gap-2 rounded-sm px-3 py-2 text-sm hover:bg-accent text-left">
                    <ImageIcon className="h-4 w-4" /> {t("header.changeIcon")}
                  </button>
                  <button onClick={() => { setDropdownOpen(false); setShowPwdModal(true); }} className="flex w-full items-center gap-2 rounded-sm px-3 py-2 text-sm hover:bg-accent text-left">
                    <KeyRound className="h-4 w-4" /> {t("header.changePassword")}
                  </button>
                  <div className="my-1 border-t" />
                  <div className="px-3 py-1.5">
                    <p className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground"><Globe className="h-3.5 w-3.5" /> {t("header.language")}</p>
                    <div className="mt-1.5 flex flex-col gap-1">
                      <label className="flex cursor-pointer items-center gap-2 rounded-sm px-2 py-1.5 text-sm hover:bg-accent">
                        <input type="radio" name="oaos_lang" value="en" checked={lang === "en"} onChange={() => setLang("en")} className="h-3.5 w-3.5 accent-primary" />
                        <span className={lang === "en" ? "font-medium" : ""}>{t("header.english")}</span>
                        {lang === "en" && <span className="ml-auto text-[10px] text-muted-foreground">{t("header.current")}</span>}
                      </label>
                      <label className="flex cursor-pointer items-center gap-2 rounded-sm px-2 py-1.5 text-sm hover:bg-accent">
                        <input type="radio" name="oaos_lang" value="ko" checked={lang === "ko"} onChange={() => setLang("ko")} className="h-3.5 w-3.5 accent-primary" />
                        <span className={lang === "ko" ? "font-medium" : ""}>{t("header.korean")}</span>
                        {lang === "ko" && <span className="ml-auto text-[10px] text-muted-foreground">{t("header.current")}</span>}
                      </label>
                    </div>
                  </div>
                  <div className="my-1 border-t" />
                  <button onClick={handleLogout} className="flex w-full items-center gap-2 rounded-sm px-3 py-2 text-sm hover:bg-accent text-left text-destructive">
                    <LogOut className="h-4 w-4" /> {t("header.logout")}
                  </button>
                </div>
              )}
            </div>
            <Button variant="ghost" size="sm" onClick={handleLogout} className="hidden sm:inline-flex"><LogOut className="mr-1 h-4 w-4" />{t("header.logout")}</Button>
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
              <p className="text-[11px] text-muted-foreground">{t("header.copyright")}</p>
            </div>
          </div>
        )}

        <main className="flex-1 bg-muted/20 p-4 md:p-6">{children}</main>
      </div>

      {showPwdModal && <PasswordModal onClose={() => setShowPwdModal(false)} />}
      {showIconModal && <IconModal currentUrl={avatarUrl} onClose={() => setShowIconModal(false)} onSave={(url) => { if (url) setAvatarUrl(url); else clearAvatarUrl(); setAvatarUrlState(url); setShowIconModal(false); }} />}
      {showAdminModal && <AdminModal email={email} onClose={() => setShowAdminModal(false)} onSwitched={() => { try { const tkn = getToken(); const p = JSON.parse(atob((tkn ?? "").split(".")[1] ?? "")); setEmail(p.sub ?? p.email ?? email); } catch {} getMe().then((m) => { setDisplayName(m.display_name); setEmail(m.email); }).catch(() => {}); setAvatarUrlState(getAvatarUrl()); setShowAdminModal(false); }} />}
    </div>
  );
}

function PasswordModal({ onClose }: { onClose: () => void }) {
  const { t } = useI18n();
  const [cur, setCur] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  async function handleSubmit() {
    setErr(null); setMsg(null);
    if (next.length < 8) { setErr(t("modals.validationNewPassword")); return; }
    if (next !== confirm) { setErr(t("modals.validationConfirm")); return; }
    setSaving(true);
    try { await changePassword(cur, next); setMsg(t("modals.changed")); setCur(""); setNext(""); setConfirm(""); } catch (e) { setErr(e instanceof Error ? e.message : t("modals.changeFailed")); } finally { setSaving(false); }
  }
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onClick={onClose}>
      <div className="w-full max-w-sm rounded-lg bg-card p-5 shadow-lg border" onClick={(e) => e.stopPropagation()}>
        <h3 className="text-base font-semibold flex items-center gap-2"><KeyRound className="h-4 w-4" /> {t("modals.changePasswordTitle")}</h3>
        <p className="text-xs text-muted-foreground mt-1">{t("modals.changePasswordDesc")}</p>
        <div className="mt-4 space-y-3">
          <div><Label className="text-xs">{t("modals.currentPassword")}</Label><Input type="password" value={cur} onChange={(e) => setCur(e.target.value)} placeholder={t("modals.currentPasswordPlaceholder")} /></div>
          <div><Label className="text-xs">{t("modals.newPassword")}</Label><Input type="password" value={next} onChange={(e) => setNext(e.target.value)} placeholder={t("modals.newPasswordPlaceholder")} /></div>
          <div><Label className="text-xs">{t("modals.confirmPassword")}</Label><Input type="password" value={confirm} onChange={(e) => setConfirm(e.target.value)} placeholder={t("modals.confirmPasswordPlaceholder")} /></div>
          {err && <p className="text-xs text-destructive">{err}</p>}
          {msg && <p className="text-xs text-green-600">{msg}</p>}
        </div>
        <div className="mt-5 flex justify-end gap-2">
          <Button variant="outline" size="sm" onClick={onClose}>{t("common.close")}</Button>
          <Button size="sm" onClick={handleSubmit} disabled={saving}>{saving ? t("modals.changing") : t("modals.change")}</Button>
        </div>
      </div>
    </div>
  );
}

function IconModal({ currentUrl, onClose, onSave }: { currentUrl: string | null; onClose: () => void; onSave: (url: string | null) => void }) {
  const { t } = useI18n();
  const [preview, setPreview] = useState<string | null>(currentUrl);
  const [err, setErr] = useState<string | null>(null);
  function onFile(e: React.ChangeEvent<HTMLInputElement>) {
    setErr(null);
    const f = e.target.files?.[0];
    if (!f) return;
    if (f.size > 2 * 1024 * 1024) { setErr(t("modals.fileTooLarge")); return; }
    if (!f.type.startsWith("image/")) { setErr(t("modals.fileNotImage")); return; }
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
        <h3 className="text-base font-semibold flex items-center gap-2"><ImageIcon className="h-4 w-4" /> {t("modals.iconTitle")}</h3>
        <p className="text-xs text-muted-foreground mt-1">{t("modals.iconDesc")}</p>
        <div className="mt-4 flex flex-col items-center gap-3">
          <div className="h-20 w-20 rounded-full border bg-muted flex items-center justify-center overflow-hidden">
            {preview ? <img src={preview} alt="preview" className="h-full w-full object-cover" /> : <span className="text-xs text-muted-foreground">{t("modals.noImage")}</span>}
          </div>
          <Input type="file" accept="image/*" onChange={onFile} className="text-xs" />
          {err && <p className="text-xs text-destructive">{err}</p>}
        </div>
        <div className="mt-5 flex justify-between">
          <Button variant="ghost" size="sm" onClick={() => { setPreview(null); }}>{t("modals.removeImage")}</Button>
          <div className="flex gap-2">
            <Button variant="outline" size="sm" onClick={onClose}>{t("common.cancel")}</Button>
            <Button size="sm" onClick={handleSave}>{t("common.save")}</Button>
          </div>
        </div>
      </div>
    </div>
  );
}

function AdminModal({ email, onClose, onSwitched }: { email: string | null; onClose: () => void; onSwitched: () => void }) {
  const { t } = useI18n();
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
    }).catch((e) => setErr(e instanceof Error ? e.message : t("common.error"))).finally(() => setLoading(false));
  }, [email, t]);

  async function handleSwitch() {
    setSwitchErr(null);
    if (!switchEmail || !switchPw) { setSwitchErr(t("modals.switchValidation")); return; }
    setSwitching(true);
    try { const r = await login(switchEmail, switchPw); setToken(r.access_token); onSwitched(); } catch (e) { setSwitchErr(e instanceof Error ? e.message : t("modals.loginFailed")); } finally { setSwitching(false); }
  }
  async function handleEditName() {
    setEditErr(null); setEditMsg(null);
    if (!editName.trim()) { setEditErr(t("modals.displayNameRequired")); return; }
    try { const me = await updateProfile(editName.trim()); setEditMsg(t("modals.displayNameChanged", { name: me.display_name })); setUsers((prev) => prev.map((u) => u.email === me.email ? me : u)); } catch (e) { setEditErr(e instanceof Error ? e.message : t("modals.changeFailedGeneric")); }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onClick={onClose}>
      <div className="w-full max-w-lg rounded-lg bg-card p-5 shadow-lg border max-h-[90vh] overflow-auto" onClick={(e) => e.stopPropagation()}>
        <h3 className="text-base font-semibold flex items-center gap-2"><UserCog className="h-4 w-4" /> {t("modals.switchTitle")}</h3>
        <p className="text-xs text-muted-foreground mt-1">{t("modals.switchDesc", { email: email ?? "" })}</p>

        <div className="mt-4 space-y-4">
          <div className="rounded-md border p-3">
            <p className="text-xs font-medium">{t("modals.displayNameChange")}</p>
            <div className="mt-2 flex gap-2">
              <Input value={editName} onChange={(e) => setEditName(e.target.value)} placeholder={t("modals.displayNamePlaceholder")} className="text-sm" />
              <Button size="sm" onClick={handleEditName}>{t("modals.changeBtn")}</Button>
            </div>
            {editErr && <p className="text-xs text-destructive mt-1">{editErr}</p>}
            {editMsg && <p className="text-xs text-green-600 mt-1">{editMsg}</p>}
          </div>

          <div className="rounded-md border p-3">
            <p className="text-xs font-medium">{t("modals.switchAccount")}</p>
            <p className="text-xs text-muted-foreground">{t("modals.switchAccountDesc")}</p>
            <div className="mt-2 space-y-2">
              <Input value={switchEmail} onChange={(e) => setSwitchEmail(e.target.value)} placeholder={t("modals.emailPlaceholder")} />
              <Input type="password" value={switchPw} onChange={(e) => setSwitchPw(e.target.value)} placeholder={t("modals.passwordPlaceholderModal")} />
              {switchErr && <p className="text-xs text-destructive">{switchErr}</p>}
              <Button size="sm" onClick={handleSwitch} disabled={switching} className="w-full">{switching ? t("modals.switching") : t("modals.switchBtn")}</Button>
            </div>
          </div>

          <div>
            <p className="text-xs font-medium mb-2">{t("modals.registeredAdmins", { count: String(users.length) })}</p>
            {loading ? <p className="text-xs text-muted-foreground">{t("modals.loadingUsers")}</p> : err ? <p className="text-xs text-destructive">{err}</p> : (
              <div className="rounded-md border divide-y max-h-40 overflow-auto">
                {users.map((u) => (
                  <div key={u.id} className="flex items-center justify-between px-3 py-2">
                    <div><p className="text-xs font-medium">{u.display_name} <span className="text-[11px] text-muted-foreground">({u.role})</span></p><p className="text-xs text-muted-foreground">{u.email}</p></div>
                    {u.email === email && <span className="text-[11px] rounded bg-primary text-primary-foreground px-2 py-0.5">{t("modals.currentBadge")}</span>}
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>

        <div className="mt-5 flex justify-end">
          <Button variant="outline" size="sm" onClick={onClose}>{t("common.close")}</Button>
        </div>
      </div>
    </div>
  );
}
