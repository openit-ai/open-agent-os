"use client";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import {
  getMe,
  listUsers,
  registerUser,
  deleteUser,
  type AdminUserPublic,
  listMappings,
  createMapping,
  updateMapping,
  deleteMapping,
  resolveMmUser,
  syncPreview,
  deriveEmployeePrincipal,
  deriveAgentId,
  type UserMapping,
  type SyncPreviewItem,
} from "@/lib/api";
import { Trash2, RefreshCw, UserPlus, Users, Link2, Eye, Loader2 } from "lucide-react";
import { useI18n } from "@/lib/i18n";

function mappingStatusVariant(s: string) {
  const v = s?.toLowerCase();
  if (v === "active" || v === "mapped" || v === "verified") return "success" as const;
  if (v === "pending" || v === "sync_pending") return "warning" as const;
  if (v === "inactive" || v === "revoked" || v === "error") return "danger" as const;
  return "secondary" as const;
}

function fmtDate(iso?: string | null) {
  if (!iso) return "-";
  try {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    const locale = typeof window !== "undefined" && localStorage.getItem("oaos_lang") === "ko" ? "ko-KR" : "en-US";
    return d.toLocaleString(locale);
  } catch {
    return iso;
  }
}

const MAX_AVATAR_URL_LENGTH = 2048;
function isSafeAvatarUrl(url: string | null | undefined): boolean {
  if (!url) return false;
  const s = url.trim();
  if (!s || s.length > MAX_AVATAR_URL_LENGTH) return false;
  try {
    const u = new URL(s);
    return u.protocol === "http:" || u.protocol === "https:";
  } catch {
    return false;
  }
}

export default function UsersPage() {
  const router = useRouter();
  const { t } = useI18n();
  const [tab, setTab] = useState<string>("admin");
  const [me, setMe] = useState<AdminUserPublic | null>(null);

  // ---- {t("users.tabsAdmin")} state ----
  const [users, setUsers] = useState<AdminUserPublic[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);
  const [email, setEmail] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<"L5" | "L4">("L4");
  const [formError, setFormError] = useState<string | null>(null);
  const [formLoading, setFormLoading] = useState(false);

  // ---- Mattermost mapping state ----
  const [mappings, setMappings] = useState<UserMapping[]>([]);
  const [mapLoading, setMapLoading] = useState(false);
  const [mapError, setMapError] = useState<string | null>(null);
  const [mmUserId, setMmUserId] = useState("");
  const [mmUsername, setMmUsername] = useState("");
  const [employeePrincipal, setEmployeePrincipal] = useState("");
  const [mapFormError, setMapFormError] = useState<string | null>(null);
  const [mapFormLoading, setMapFormLoading] = useState(false);
  const [deletingMap, setDeletingMap] = useState<string | null>(null);
  const [editingMap, setEditingMap] = useState<string | null>(null);
  const [editDisplayName, setEditDisplayName] = useState<string>("");
  const [editLoading, setEditLoading] = useState(false);
  const [mappingDisplayName, setMappingDisplayName] = useState("");
  const [mappingAvatarUrl, setMappingAvatarUrl] = useState("");
  const [resolving, setResolving] = useState(false);
  const [resolveMsg, setResolveMsg] = useState<string | null>(null);
  const [preview, setPreview] = useState<SyncPreviewItem[] | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);

  const fetchAdmin = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [meData, usersData] = await Promise.all([getMe(), listUsers()]);
      setMe(meData as AdminUserPublic);
      if (Array.isArray(usersData)) setUsers(usersData as AdminUserPublic[]);
      else if ((usersData as { users?: AdminUserPublic[] }).users) setUsers((usersData as { users?: AdminUserPublic[] }).users!);
      else setUsers([]);
    } catch (e) {
      setError(e instanceof Error ? e.message : t("users.lookupFailed"));
    } finally {
      setLoading(false);
    }
  }, []);

  const fetchMappings = useCallback(async () => {
    setMapLoading(true);
    setMapError(null);
    try {
      const res = await listMappings();
      const arr: UserMapping[] = Array.isArray(res)
        ? (res as UserMapping[])
        : ((res as { mappings?: UserMapping[] }).mappings ??
          (res as { items?: UserMapping[] }).items ??
          []);
      setMappings(arr);
    } catch (e) {
      setMapError(e instanceof Error ? e.message : t("common.error"));
    } finally {
      setMapLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!getTokenSafe()) {
      router.replace("/login");
      return;
    }
    fetchAdmin();
    fetchMappings();
  }, [fetchAdmin, fetchMappings, router]);

  function getTokenSafe() {
    try {
      return typeof window !== "undefined" ? localStorage.getItem("admin_token") : null;
    } catch {
      return null;
    }
  }

  const isL5 = me?.role === "L5";
  const derivedPrincipal = useMemo(() => {
    if (employeePrincipal.trim()) return employeePrincipal.trim();
    if (!mmUserId.trim() && !mmUsername.trim()) return "";
    try {
      return deriveEmployeePrincipal(mmUsername.trim(), mmUserId.trim());
    } catch {
      return "";
    }
  }, [employeePrincipal, mmUsername, mmUserId]);

  const derivedAgent = useMemo(() => {
    if (!derivedPrincipal) return "";
    try {
      return deriveAgentId(derivedPrincipal);
    } catch {
      return "";
    }
  }, [derivedPrincipal]);

  async function handleRegister(e: React.FormEvent) {
    e.preventDefault();
    setFormError(null);
    if (!email || !displayName || !password) {
      setFormError(t("users.validationRequired"));
      return;
    }
    if (password.length < 8) {
      setFormError(t("users.validationPassword"));
      return;
    }
    setFormLoading(true);
    try {
      await registerUser({ email, password, display_name: displayName, role });
      setEmail("");
      setDisplayName("");
      setPassword("");
      setRole("L4");
      await fetchAdmin();
    } catch (err) {
      setFormError(err instanceof Error ? err.message : t("common.error"));
    } finally {
      setFormLoading(false);
    }
  }

  async function handleDelete(id: string) {
    if (me && me.id === id) {
      alert(t("users.selfDeleteError"));
      return;
    }
    if (!confirm(t("users.deleteConfirm"))) return;
    setDeleting(id);
    try {
      await deleteUser(id);
      await fetchAdmin();
    } catch (e) {
      alert(e instanceof Error ? e.message : t("common.error"));
    } finally {
      setDeleting(null);
    }
  }

  // ---- mapping handlers ----
  async function handleResolve() {
    setResolveMsg(null);
    const uname = mmUsername.trim() || mmUserId.trim();
    if (!uname) { setResolveMsg(t("users.lookupNeedUsername")); return; }
    setResolving(true);
    try {
      const r = await resolveMmUser(uname);
      setMmUserId(r.mm_user_id);
      if (r.mm_username) setMmUsername(r.mm_username);
      setResolveMsg(t("users.lookupSuccess", { username: r.mm_username, id: r.mm_user_id.slice(0,8) }));
    } catch (err) {
      setResolveMsg(err instanceof Error ? err.message : t("users.lookupFailed"));
    } finally {
      setResolving(false);
    }
  }

  async function handleCreateMapping(e: React.FormEvent) {
    e.preventDefault();
    setMapFormError(null);
    if (!mmUsername.trim()) {
      setMapFormError(t("users.validationMmUsername"));
      return;
    }
    // frontend avatar_url strict validation (https/http only, bounded 2048) — mirrors backend _validate_avatar_url
    const avatarTrimmed = mappingAvatarUrl.trim();
    if (avatarTrimmed) {
      if (avatarTrimmed.length > MAX_AVATAR_URL_LENGTH) {
        setMapFormError(`avatar_url too long (max ${MAX_AVATAR_URL_LENGTH})`);
        return;
      }
      try {
        const u = new URL(avatarTrimmed);
        if (u.protocol !== "http:" && u.protocol !== "https:") {
          setMapFormError("avatar_url must use http or https");
          return;
        }
      } catch {
        setMapFormError("invalid avatar_url");
        return;
      }
    }
    let finalId = mmUserId.trim();
    let finalUsername: string | undefined = mmUsername.trim() || undefined;
    // Username만 입력한 경우 백엔드가 자동 조회하므로 ID 없이도 제출 가능하나, UX상 미리 조회하여 즉시 피드백
    if (!finalId && finalUsername) {
      try {
        setMapFormLoading(true);
        const r = await resolveMmUser(finalUsername);
        finalId = r.mm_user_id;
        finalUsername = r.mm_username || finalUsername;
        setMmUserId(finalId);
        setResolveMsg(t("users.lookupSuccess", { username: r.mm_username, id: r.mm_user_id.slice(0,8) }));
      } catch (err) {
        // 백엔드가 자동 조회하므로 그대로 제출 시도 — 실패 시 백엔드 에러 표시
        // 하지만 UX상 메시지 유지
        setResolveMsg(err instanceof Error ? err.message : t("users.resolveRetry"));
      } finally {
        // keep loading for next step
      }
    }
    if (employeePrincipal.trim() && !employeePrincipal.trim().startsWith("employee:")) {
      setMapFormError(t("users.validationPrincipal"));
      return;
    }
    if (!mapFormLoading) setMapFormLoading(true);
    try {
      await createMapping({
        mm_user_id: finalId,
        mm_username: finalUsername,
        employee_principal: employeePrincipal.trim() || undefined,
        display_name: mappingDisplayName.trim() || undefined,
        avatar_url: avatarTrimmed || undefined,
      });
      setMmUserId("");
      setMmUsername("");
      setEmployeePrincipal("");
      setMappingDisplayName("");
      setMappingAvatarUrl("");
      setPreview(null);
      await fetchMappings();
    } catch (err) {
      setMapFormError(err instanceof Error ? err.message : t("common.error"));
    } finally {
      setMapFormLoading(false);
    }
  }

  async function handleUpdateDisplayName(id: string) {
    if (!editDisplayName.trim()) { alert(t("users.validationDisplayName") || "호칭을 입력하세요"); return; }
    setEditLoading(true);
    try {
      await updateMapping(id, { display_name: editDisplayName.trim() });
      setEditingMap(null);
      setEditDisplayName("");
      await fetchMappings();
    } catch (e) {
      alert(e instanceof Error ? e.message : t("common.error"));
    } finally {
      setEditLoading(false);
    }
  }

  async function handleDeleteMapping(id: string) {
    if (!confirm(t("users.deleteConfirm"))) return;
    setDeletingMap(id);
    try {
      await deleteMapping(id);
      await fetchMappings();
    } catch (e) {
      alert(e instanceof Error ? e.message : t("common.error"));
    } finally {
      setDeletingMap(null);
    }
  }

  async function handleSyncPreview() {
    setPreviewLoading(true);
    setPreviewError(null);
    try {
      const res = await syncPreview();
      const arr: SyncPreviewItem[] = Array.isArray(res)
        ? (res as SyncPreviewItem[])
        : ((res as { preview?: SyncPreviewItem[] }).preview ??
          (res as { items?: SyncPreviewItem[] }).items ??
          []);
      setPreview(arr);
    } catch (e) {
      setPreviewError(e instanceof Error ? e.message : t("common.error"));
    } finally {
      setPreviewLoading(false);
    }
  }

  return (
    <div className="mx-auto w-full max-w-[1200px] space-y-6">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <h1 className="flex items-center gap-2 text-2xl font-semibold">
          <Users className="h-6 w-6" />
          {t("users.title")}
        </h1>
        <Button
          variant="outline"
          size="sm"
          onClick={() => {
            fetchAdmin();
            fetchMappings();
          }}
          disabled={loading || mapLoading}
        >
          <RefreshCw className={`mr-1 h-4 w-4 ${loading || mapLoading ? "animate-spin" : ""}`} />
          {t("common.refresh")}
        </Button>
      </div>

      <Tabs value={tab} onValueChange={setTab} className="w-full">
        <TabsList className="w-full justify-start overflow-x-auto sm:w-auto">
          <TabsTrigger value="admin" className="gap-1.5">
            <Users className="h-4 w-4" />
            {t("users.tabsAdmin")}
          </TabsTrigger>
          <TabsTrigger value="mapping" className="gap-1.5">
            <Link2 className="h-4 w-4" />
            {t("users.tabsMapping")}
          </TabsTrigger>
        </TabsList>

        {/* {t("users.tabsAdmin")} tab - existing intact */}
        <TabsContent value="admin" className="space-y-6">
          {error && (
            <p className="rounded-md bg-[#DC2626]/10 p-3 text-sm text-[#DC2626]" role="alert">
              {error}
            </p>
          )}
          {isL5 ? (
            <Card>
              <CardHeader>
                <CardTitle className="flex items-center gap-2 text-base">
                  <UserPlus className="h-4 w-4" />
                  {t("users.adminRegisterTitle")}
                </CardTitle>
                <CardDescription>{t("users.adminRegisterDesc")}</CardDescription>
              </CardHeader>
              <CardContent>
                <form onSubmit={handleRegister} className="grid gap-4 sm:grid-cols-2">
                  <div className="space-y-1">
                    <Label htmlFor="email">{t("users.email")}</Label>
                    <Input id="email" type="email" placeholder={t("users.emailPlaceholder")} value={email} onChange={(e) => setEmail(e.target.value)} required />
                  </div>
                  <div className="space-y-1">
                    <Label htmlFor="displayName">{t("users.displayName")}</Label>
                    <Input id="displayName" placeholder={t("users.displayNamePlaceholder")} value={displayName} onChange={(e) => setDisplayName(e.target.value)} required />
                  </div>
                  <div className="space-y-1">
                    <Label htmlFor="password">{t("users.password")}</Label>
                    <Input id="password" type="password" placeholder={t("users.passwordPlaceholder")} value={password} onChange={(e) => setPassword(e.target.value)} required />
                  </div>
                  <div className="space-y-1">
                    <Label htmlFor="role">{t("users.role")}</Label>
                    <select
                      id="role"
                      value={role}
                      onChange={(e) => setRole(e.target.value as "L5" | "L4")}
                      className="flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm"
                    >
                      <option value="L4">{t("users.roleL4")}</option>
                      <option value="L5">{t("users.roleL5")}</option>
                    </select>
                  </div>
                  <div className="sm:col-span-2 flex items-end">
                    <Button type="submit" disabled={formLoading} className="w-full sm:w-auto">
                      {formLoading ? t("users.registering") : t("users.register")}
                    </Button>
                  </div>
                  {formError && (
                    <p className="sm:col-span-2 text-sm text-[#DC2626]" role="alert">
                      {formError}
                    </p>
                  )}
                </form>
              </CardContent>
            </Card>
          ) : me ? (
            <Card>
              <CardContent className="pt-6">
                <p className="text-sm text-muted-foreground">{t("users.l4Notice")}</p>
              </CardContent>
            </Card>
          ) : null}

          <Card>
            <CardHeader>
              <CardTitle className="text-base">{t("users.userList")}</CardTitle>
              <CardDescription>{t("users.userListDesc", { count: String(users.length) })}</CardDescription>
            </CardHeader>
            <CardContent className="p-0">
              <div className="w-full overflow-auto">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>{t("users.tableEmail")}</TableHead>
                      <TableHead>{t("users.tableName")}</TableHead>
                      <TableHead>{t("users.tableRole")}</TableHead>
                      <TableHead>{t("users.tableCreated")}</TableHead>
                      <TableHead className="text-right">{t("users.tableActions")}</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {loading ? (
                      <TableRow>
                        <TableCell colSpan={5} className="text-center text-muted-foreground">
                          {t("common.loading")}
                        </TableCell>
                      </TableRow>
                    ) : users.length === 0 ? (
                      <TableRow>
                        <TableCell colSpan={5} className="text-center text-muted-foreground">
                          {t("users.noUsers")}
                        </TableCell>
                      </TableRow>
                    ) : (
                      users.map((u) => {
                        const isSelf = me?.id === u.id;
                        return (
                          <TableRow key={u.id} className={isSelf ? "bg-muted/30" : ""}>
                            <TableCell className="font-mono text-xs">
                              {u.email}
                              {isSelf && <span className="ml-2 rounded bg-primary px-1.5 py-0.5 text-[10px] text-primary-foreground">{t("users.meBadge")}</span>}
                            </TableCell>
                            <TableCell className="text-sm">{u.display_name}</TableCell>
                            <TableCell>
                              <Badge variant={u.role === "L5" ? "success" : "secondary"}>{u.role}</Badge>
                            </TableCell>
                            <TableCell className="text-xs text-muted-foreground">{fmtDate(u.created_at)}</TableCell>
                            <TableCell className="text-right">
                              <Button
                                variant="ghost"
                                size="sm"
                                disabled={!!isSelf || deleting === u.id || !isL5}
                                onClick={() => handleDelete(u.id)}
                                title={isSelf ? t("users.selfDeleteError") : isL5 ? t("users.delete") : t("users.l5Only")}
                                aria-label={`${t("users.delete")} ${u.email}`}
                              >
                                <Trash2 className="h-4 w-4" />
                                {deleting === u.id ? "..." : t("users.delete")}
                              </Button>
                            </TableCell>
                          </TableRow>
                        );
                      })
                    )}
                  </TableBody>
                </Table>
              </div>
            </CardContent>
          </Card>
          <p className="text-xs text-muted-foreground">{t("users.jwtNote")}</p>
        </TabsContent>

        {/* {t("users.tabsMapping")} tab */}
        <TabsContent value="mapping" className="space-y-6">
          {mapError && (
            <p className="rounded-md bg-[#DC2626]/10 p-3 text-sm text-[#DC2626]" role="alert">
              {mapError}
            </p>
          )}

          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2 text-base">
                <Link2 className="h-4 w-4" />
                {t("users.tabsMapping")} 등록
              </CardTitle>
              <CardDescription>
                {t("users.mappingDesc")}
              </CardDescription>
            </CardHeader>
            <CardContent>
              <form onSubmit={handleCreateMapping} className="grid gap-4 sm:grid-cols-3">
                <div className="space-y-1">
                  <Label htmlFor="mm_username">MM Username *</Label>
                  <div className="flex gap-1.5">
                    <Input id="mm_username" placeholder={t("users.mmUsernamePlaceholder")} value={mmUsername} onChange={(e) => setMmUsername(e.target.value)} className="flex-1" />
                    <Button type="button" variant="outline" size="sm" onClick={handleResolve} disabled={resolving || (!mmUsername.trim() && !mmUserId.trim())} className="shrink-0">
                      {resolving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : t("users.lookupId")}
                    </Button>
                  </div>
                </div>
                <div className="space-y-1">
                  <Label htmlFor="mm_user_id">{t("users.mmUserId")}</Label>
                  <Input id="mm_user_id" placeholder={t("users.mmUserIdPlaceholder")} value={mmUserId} onChange={(e) => setMmUserId(e.target.value)} />
                  {resolveMsg && <p className="text-xs text-muted-foreground">{resolveMsg}</p>}
                </div>
                <div className="space-y-1">
                  <Label htmlFor="mappingDisplayName">호칭 (아이콘 옆 표시, A안)</Label>
                  <Input id="mappingDisplayName" placeholder="예: 코코 (개인별, 비우면 username)" maxLength={64} value={mappingDisplayName} onChange={(e) => setMappingDisplayName(e.target.value)} />
                </div>
                <div className="space-y-1">
                  <Label htmlFor="mappingAvatarUrl">아바타 URL (https만)</Label>
                  <Input id="mappingAvatarUrl" placeholder="https://example.com/avatar.png" value={mappingAvatarUrl} onChange={(e) => setMappingAvatarUrl(e.target.value)} maxLength={2048} />
                </div>
                <div className="space-y-1">
                  <Label htmlFor="employee_principal">{t("users.employeePrincipal")}</Label>
                  <Input id="employee_principal" placeholder={t("users.employeePrincipalPlaceholder")} value={employeePrincipal} onChange={(e) => setEmployeePrincipal(e.target.value)} />
                </div>
                <div className="sm:col-span-3 rounded-md border border-dashed bg-muted/30 px-3 py-2 text-xs leading-relaxed">
                  <span className="font-medium">{t("users.hintTitle")}</span>{" "}
                  {derivedPrincipal ? (
                    <>
                      <span className="font-mono">{derivedPrincipal}</span>
                      <span className="mx-1">→</span>
                      <span className="font-mono">{derivedAgent}</span>
                      {employeePrincipal.trim() ? (
                        <span className="ml-2 text-muted-foreground">{t("users.hintManual")}</span>
                      ) : (
                        <span className="ml-2 text-muted-foreground">{t("users.hintAuto")}</span>
                      )}
                    </>
                  ) : (
                    <span className="text-muted-foreground">{t("users.hintEmpty")}</span>
                  )}
                </div>
                <div className="flex flex-col gap-2 sm:col-span-3 sm:flex-row sm:items-center">
                  <Button type="submit" disabled={mapFormLoading || !isL5} className="w-full sm:w-auto">
                    {mapFormLoading ? (
                      <>
                        <Loader2 className="h-4 w-4 animate-spin" /> {t("users.creating")}
                      </>
                    ) : (
                      <>
                        <Link2 className="h-4 w-4" /> {t("users.createMapping")}
                      </>
                    )}
                  </Button>
                  {!isL5 && <span className="text-xs text-muted-foreground">{t("users.l5Only")}</span>}
                  <Button type="button" variant="outline" onClick={handleSyncPreview} disabled={previewLoading} className="w-full sm:w-auto">
                    {previewLoading ? <Loader2 className="h-4 w-4 animate-spin" /> : <Eye className="h-4 w-4" />}
                    {t("users.syncPreview")}
                  </Button>
                  {mapFormError && <span className="text-sm text-[#DC2626]">{mapFormError}</span>}
                </div>
              </form>

              {previewError && <p className="mt-3 text-sm text-[#DC2626]">{previewError}</p>}

              {preview !== null && (
                <div className="mt-4 rounded-md border bg-card">
                  <div className="flex items-center justify-between border-b px-3 py-2">
                    <span className="text-sm font-medium">{t("users.syncPreviewTitle")}</span>
                    <Badge variant="secondary">{preview.length}건</Badge>
                  </div>
                  {preview.length === 0 ? (
                    <p className="px-3 py-6 text-center text-sm text-muted-foreground">{t("users.syncPreviewEmpty")}</p>
                  ) : (
                    <div className="w-full overflow-auto">
                      <Table>
                        <TableHeader>
                          <TableRow>
                            <TableHead>Username</TableHead>
                            <TableHead>MM User ID</TableHead>
                            <TableHead>Principal</TableHead>
                            <TableHead>Agent</TableHead>
                            <TableHead>Status</TableHead>
                          </TableRow>
                        </TableHeader>
                        <TableBody>
                          {preview.map((p, idx) => (
                            <TableRow key={`${p.mm_user_id}-${idx}`}>
                              <TableCell className="text-xs">{p.mm_username}</TableCell>
                              <TableCell className="font-mono text-xs">{p.mm_user_id}</TableCell>
                              <TableCell className="font-mono text-xs">{p.employee_principal}</TableCell>
                              <TableCell className="font-mono text-xs text-muted-foreground">{p.agent_id}</TableCell>
                              <TableCell>
                                <Badge variant={p.already_mapped ? "secondary" : mappingStatusVariant(p.status)}>{p.already_mapped ? t("users.alreadyMapped") : p.status}</Badge>
                              </TableCell>
                            </TableRow>
                          ))}
                        </TableBody>
                      </Table>
                    </div>
                  )}
                </div>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-3">
              <div>
                <CardTitle className="text-base">{t("users.mappingList")}</CardTitle>
                <CardDescription>
                  {t("users.mappingListDesc", { count: String(mappings.length) })}
                </CardDescription>
              </div>
              <Button variant="outline" size="sm" onClick={fetchMappings} disabled={mapLoading}>
                <RefreshCw className={`h-4 w-4 ${mapLoading ? "animate-spin" : ""}`} />
                {t("common.refresh")}
              </Button>
            </CardHeader>
            <CardContent className="p-0">
              <div className="w-full overflow-auto">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead className="min-w-[140px]">호칭 (아이콘 옆)</TableHead>
                      <TableHead className="min-w-[110px]">Username</TableHead>
                      <TableHead className="min-w-[120px]">MM User ID</TableHead>
                      <TableHead className="min-w-[150px]">Employee Principal</TableHead>
                      <TableHead className="min-w-[150px]">Agent ID</TableHead>
                      <TableHead>Status</TableHead>
                      <TableHead className="min-w-[140px]">Created</TableHead>
                      <TableHead className="text-right">Actions</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {mapLoading ? (
                      <TableRow>
                        <TableCell colSpan={8} className="py-8 text-center text-muted-foreground">
                          {t("common.loading")}
                        </TableCell>
                      </TableRow>
                    ) : mappings.length === 0 ? (
                      <TableRow>
                        <TableCell colSpan={8} className="py-8 text-center text-muted-foreground">
                          {t("users.noMappings")}
                        </TableCell>
                      </TableRow>
                    ) : (
                      mappings.map((m) => {
                        const uname = m.mm_username ?? m.username ?? "-";
                        const isEditing = editingMap === m.id;
                        const iconLetter = (m.display_name || uname || "?").trim().charAt(0).toUpperCase() || "?";
                        return (
                          <TableRow key={m.id}>
                            <TableCell>
                              <div className="flex items-center gap-2">
                                <span className="inline-flex h-7 w-7 items-center justify-center rounded-full bg-primary text-[11px] font-semibold text-primary-foreground" title={m.display_name || uname}>
                                  {m.avatar_url && isSafeAvatarUrl(m.avatar_url) ? <img src={m.avatar_url} alt="" className="h-7 w-7 rounded-full object-cover" /> : iconLetter}
                                </span>
                                {isEditing ? (
                                  <Input value={editDisplayName} onChange={(e) => setEditDisplayName(e.target.value)} placeholder="예: 코코" className="h-7 w-[120px] text-xs" maxLength={64} />
                                ) : (
                                  <span className="text-sm font-medium">{m.display_name || <span className="text-muted-foreground italic">(미설정)</span>}</span>
                                )}
                              </div>
                            </TableCell>
                            <TableCell className="text-xs">{uname || "-"}</TableCell>
                            <TableCell className="font-mono text-xs" title={m.mm_user_id}>
                              {m.mm_user_id}
                            </TableCell>
                            <TableCell className="font-mono text-xs">{m.employee_principal}</TableCell>
                            <TableCell className="font-mono text-xs text-muted-foreground">{m.agent_id}</TableCell>
                            <TableCell>
                              <Badge variant={mappingStatusVariant(m.status)}>{m.status}</Badge>
                            </TableCell>
                            <TableCell className="text-xs text-muted-foreground">{fmtDate(m.created_at)}</TableCell>
                            <TableCell className="text-right">
                              <div className="flex items-center justify-end gap-1">
                                {isEditing ? (
                                  <>
                                    <Button variant="default" size="sm" disabled={editLoading} onClick={() => handleUpdateDisplayName(m.id)} className="h-7 px-2 text-xs">{editLoading ? <Loader2 className="h-3 w-3 animate-spin"/> : "저장"}</Button>
                                    <Button variant="ghost" size="sm" onClick={() => { setEditingMap(null); setEditDisplayName(""); }} className="h-7 px-2 text-xs">취소</Button>
                                  </>
                                ) : (
                                  <Button variant="ghost" size="sm" disabled={!isL5} onClick={() => { setEditingMap(m.id); setEditDisplayName(m.display_name || ""); }} title={!isL5 ? t("users.l5Only") : "호칭 수정"} className="h-7 px-2 text-xs">호칭 수정</Button>
                                )}
                                <Button variant="ghost" size="sm" disabled={deletingMap === m.id || !isL5} onClick={() => handleDeleteMapping(m.id)} title={!isL5 ? t("users.l5Only") : t("users.tableDelete")} aria-label={`${t("users.tableDelete")} ${m.mm_user_id}`}>
                                  <Trash2 className="h-4 w-4" />
                                </Button>
                              </div>
                            </TableCell>
                          </TableRow>
                        );
                      })
                    )}
                  </TableBody>
                </Table>
              </div>
            </CardContent>
          </Card>

          <p className="text-xs text-muted-foreground">
            {t("users.oneOneNote")}
          </p>
        </TabsContent>
      </Tabs>
    </div>
  );
}
