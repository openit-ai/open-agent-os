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
  deleteMapping,
  resolveMmUser,
  syncPreview,
  deriveEmployeePrincipal,
  deriveAgentId,
  type UserMapping,
  type SyncPreviewItem,
} from "@/lib/api";
import { Trash2, RefreshCw, UserPlus, Users, Link2, Eye, Loader2 } from "lucide-react";

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
    return d.toLocaleString("ko-KR");
  } catch {
    return iso;
  }
}

export default function UsersPage() {
  const router = useRouter();
  const [tab, setTab] = useState<string>("admin");
  const [me, setMe] = useState<AdminUserPublic | null>(null);

  // ---- Admin Users state ----
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
      setError(e instanceof Error ? e.message : "조회 실패");
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
      setMapError(e instanceof Error ? e.message : "매핑 조회 실패");
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
      setFormError("이메일, 이름, 비밀번호는 필수입니다.");
      return;
    }
    if (password.length < 8) {
      setFormError("비밀번호는 8자 이상이어야 합니다.");
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
      setFormError(err instanceof Error ? err.message : "등록 실패");
    } finally {
      setFormLoading(false);
    }
  }

  async function handleDelete(id: string) {
    if (me && me.id === id) {
      alert("자기 자신은 삭제할 수 없습니다.");
      return;
    }
    if (!confirm("해당 사용자를 삭제하시겠습니까?")) return;
    setDeleting(id);
    try {
      await deleteUser(id);
      await fetchAdmin();
    } catch (e) {
      alert(e instanceof Error ? e.message : "삭제 실패");
    } finally {
      setDeleting(null);
    }
  }

  // ---- mapping handlers ----
  async function handleResolve() {
    setResolveMsg(null);
    const uname = mmUsername.trim() || mmUserId.trim();
    if (!uname) { setResolveMsg("Username을 먼저 입력하세요."); return; }
    setResolving(true);
    try {
      const r = await resolveMmUser(uname);
      setMmUserId(r.mm_user_id);
      if (r.mm_username) setMmUsername(r.mm_username);
      setResolveMsg(`조회 성공: ${r.mm_username} → ${r.mm_user_id.slice(0,8)}...`);
    } catch (err) {
      setResolveMsg(err instanceof Error ? err.message : "조회 실패");
    } finally {
      setResolving(false);
    }
  }

  async function handleCreateMapping(e: React.FormEvent) {
    e.preventDefault();
    setMapFormError(null);
    let finalId = mmUserId.trim();
    let finalUsername = mmUsername.trim() || undefined;
    // Username만 입력한 경우 자동 조회 시도
    if (!finalId && finalUsername) {
      try {
        setMapFormLoading(true);
        const r = await resolveMmUser(finalUsername);
        finalId = r.mm_user_id;
        finalUsername = r.mm_username || finalUsername;
        setMmUserId(finalId);
      } catch {
        setMapFormError("MM User ID가 비어있고 Username 조회도 실패했습니다. ID를 직접 입력하거나 Username을 확인하세요.");
        setMapFormLoading(false);
        return;
      } finally {
        // keep loading for next step
      }
    }
    if (!finalId) {
      setMapFormError("MM User ID는 필수입니다. Username으로 자동 조회하려면 Username을 입력하고 다시 시도하세요.");
      setMapFormLoading(false);
      return;
    }
    if (employeePrincipal.trim() && !employeePrincipal.trim().startsWith("employee:")) {
      setMapFormError("employee_principal은 employee: 로 시작해야 합니다.");
      return;
    }
    if (!mapFormLoading) setMapFormLoading(true);
    try {
      await createMapping({
        mm_user_id: finalId,
        mm_username: finalUsername,
        employee_principal: employeePrincipal.trim() || undefined,
      });
      setMmUserId("");
      setMmUsername("");
      setEmployeePrincipal("");
      setPreview(null);
      await fetchMappings();
    } catch (err) {
      setMapFormError(err instanceof Error ? err.message : "매핑 등록 실패");
    } finally {
      setMapFormLoading(false);
    }
  }

  async function handleDeleteMapping(id: string) {
    if (!confirm("해당 매핑을 삭제하시겠습니까?")) return;
    setDeletingMap(id);
    try {
      await deleteMapping(id);
      await fetchMappings();
    } catch (e) {
      alert(e instanceof Error ? e.message : "매핑 삭제 실패");
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
      setPreviewError(e instanceof Error ? e.message : "미리보기 실패");
    } finally {
      setPreviewLoading(false);
    }
  }

  return (
    <div className="mx-auto w-full max-w-[1200px] space-y-6">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <h1 className="flex items-center gap-2 text-2xl font-semibold">
          <Users className="h-6 w-6" />
          Users / Agents
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
          새로고침
        </Button>
      </div>

      <Tabs value={tab} onValueChange={setTab} className="w-full">
        <TabsList className="w-full justify-start overflow-x-auto sm:w-auto">
          <TabsTrigger value="admin" className="gap-1.5">
            <Users className="h-4 w-4" />
            Admin Users
          </TabsTrigger>
          <TabsTrigger value="mapping" className="gap-1.5">
            <Link2 className="h-4 w-4" />
            Mattermost → Agent 매핑
          </TabsTrigger>
        </TabsList>

        {/* Admin Users tab - existing intact */}
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
                  사용자 등록
                </CardTitle>
                <CardDescription>L5 Infra Admin만 신규 사용자를 등록할 수 있습니다.</CardDescription>
              </CardHeader>
              <CardContent>
                <form onSubmit={handleRegister} className="grid gap-4 sm:grid-cols-2">
                  <div className="space-y-1">
                    <Label htmlFor="email">이메일</Label>
                    <Input id="email" type="email" placeholder="user@openit.co.kr" value={email} onChange={(e) => setEmail(e.target.value)} required />
                  </div>
                  <div className="space-y-1">
                    <Label htmlFor="displayName">이름 (display_name)</Label>
                    <Input id="displayName" placeholder="홍길동" value={displayName} onChange={(e) => setDisplayName(e.target.value)} required />
                  </div>
                  <div className="space-y-1">
                    <Label htmlFor="password">비밀번호 (8자 이상)</Label>
                    <Input id="password" type="password" placeholder="••••••••" value={password} onChange={(e) => setPassword(e.target.value)} required />
                  </div>
                  <div className="space-y-1">
                    <Label htmlFor="role">Role</Label>
                    <select
                      id="role"
                      value={role}
                      onChange={(e) => setRole(e.target.value as "L5" | "L4")}
                      className="flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm"
                    >
                      <option value="L4">L4 (읽기 전용)</option>
                      <option value="L5">L5 (Infra Admin)</option>
                    </select>
                  </div>
                  <div className="sm:col-span-2 flex items-end">
                    <Button type="submit" disabled={formLoading} className="w-full sm:w-auto">
                      {formLoading ? "등록 중..." : "등록"}
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
                <p className="text-sm text-muted-foreground">현재 계정은 L4 (읽기 전용)입니다. 사용자 등록은 L5만 가능합니다.</p>
              </CardContent>
            </Card>
          ) : null}

          <Card>
            <CardHeader>
              <CardTitle className="text-base">사용자 목록</CardTitle>
              <CardDescription>총 {users.length}명 · 375px 모바일에서도 가로 스크롤로 확인 가능</CardDescription>
            </CardHeader>
            <CardContent className="p-0">
              <div className="w-full overflow-auto">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>이메일</TableHead>
                      <TableHead>이름</TableHead>
                      <TableHead>Role</TableHead>
                      <TableHead>생성일</TableHead>
                      <TableHead className="text-right">작업</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {loading ? (
                      <TableRow>
                        <TableCell colSpan={5} className="text-center text-muted-foreground">
                          로딩 중...
                        </TableCell>
                      </TableRow>
                    ) : users.length === 0 ? (
                      <TableRow>
                        <TableCell colSpan={5} className="text-center text-muted-foreground">
                          등록된 사용자가 없습니다.
                        </TableCell>
                      </TableRow>
                    ) : (
                      users.map((u) => {
                        const isSelf = me?.id === u.id;
                        return (
                          <TableRow key={u.id} className={isSelf ? "bg-muted/30" : ""}>
                            <TableCell className="font-mono text-xs">
                              {u.email}
                              {isSelf && <span className="ml-2 rounded bg-primary px-1.5 py-0.5 text-[10px] text-primary-foreground">나</span>}
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
                                title={isSelf ? "자기 자신은 삭제할 수 없습니다" : isL5 ? "삭제" : "L5만 삭제 가능"}
                                aria-label={`삭제 ${u.email}`}
                              >
                                <Trash2 className="h-4 w-4" />
                                {deleting === u.id ? "..." : "삭제"}
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
          <p className="text-xs text-muted-foreground">JWT 기반 인증. 삭제는 L5만 가능하며 자기 자신 삭제는 차단됩니다.</p>
        </TabsContent>

        {/* Mattermost → Agent 매핑 tab */}
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
                Mattermost → Agent 매핑 등록
              </CardTitle>
              <CardDescription>
                Username만 입력해도 ID 자동 조회 가능 · Employee Principal 미입력 시 자동 유도 (<span className="font-mono text-xs">employee:&lt;username|id&gt;</span> → <span className="font-mono text-xs">agent:assistant:&lt;suffix&gt;</span>)
              </CardDescription>
            </CardHeader>
            <CardContent>
              <form onSubmit={handleCreateMapping} className="grid gap-4 sm:grid-cols-3">
                <div className="space-y-1">
                  <Label htmlFor="mm_user_id">MM User ID *</Label>
                  <Input id="mm_user_id" placeholder="비우면 Username으로 자동 조회" value={mmUserId} onChange={(e) => setMmUserId(e.target.value)} />
                  {resolveMsg && <p className="text-xs text-muted-foreground">{resolveMsg}</p>}
                </div>
                <div className="space-y-1">
                  <Label htmlFor="mm_username">MM Username</Label>
                  <div className="flex gap-1.5">
                    <Input id="mm_username" placeholder="e.g. mykim" value={mmUsername} onChange={(e) => setMmUsername(e.target.value)} className="flex-1" />
                    <Button type="button" variant="outline" size="sm" onClick={handleResolve} disabled={resolving || (!mmUsername.trim() && !mmUserId.trim())} className="shrink-0">
                      {resolving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : "ID 조회"}
                    </Button>
                  </div>
                </div>
                <div className="space-y-1">
                  <Label htmlFor="employee_principal">Employee Principal</Label>
                  <Input id="employee_principal" placeholder="employee:kim (비우면 자동 유도)" value={employeePrincipal} onChange={(e) => setEmployeePrincipal(e.target.value)} />
                </div>
                <div className="sm:col-span-3 rounded-md border border-dashed bg-muted/30 px-3 py-2 text-xs leading-relaxed">
                  <span className="font-medium">자동 유도 힌트:</span>{" "}
                  {derivedPrincipal ? (
                    <>
                      <span className="font-mono">{derivedPrincipal}</span>
                      <span className="mx-1">→</span>
                      <span className="font-mono">{derivedAgent}</span>
                      {employeePrincipal.trim() ? (
                        <span className="ml-2 text-muted-foreground">(직접 입력값 사용)</span>
                      ) : (
                        <span className="ml-2 text-muted-foreground">(username/id 기반 자동 유도)</span>
                      )}
                    </>
                  ) : (
                    <span className="text-muted-foreground">MM User ID / Username 입력 시 principal/agent 미리보기</span>
                  )}
                </div>
                <div className="flex flex-col gap-2 sm:col-span-3 sm:flex-row sm:items-center">
                  <Button type="submit" disabled={mapFormLoading || !isL5} className="w-full sm:w-auto">
                    {mapFormLoading ? (
                      <>
                        <Loader2 className="h-4 w-4 animate-spin" /> 등록 중...
                      </>
                    ) : (
                      <>
                        <Link2 className="h-4 w-4" /> 매핑 등록
                      </>
                    )}
                  </Button>
                  {!isL5 && <span className="text-xs text-muted-foreground">L5만 쓰기 가능 (L4는 읽기 전용)</span>}
                  <Button type="button" variant="outline" onClick={handleSyncPreview} disabled={previewLoading} className="w-full sm:w-auto">
                    {previewLoading ? <Loader2 className="h-4 w-4 animate-spin" /> : <Eye className="h-4 w-4" />}
                    Sync 미리보기
                  </Button>
                  {mapFormError && <span className="text-sm text-[#DC2626]">{mapFormError}</span>}
                </div>
              </form>

              {previewError && <p className="mt-3 text-sm text-[#DC2626]">{previewError}</p>}

              {preview !== null && (
                <div className="mt-4 rounded-md border bg-card">
                  <div className="flex items-center justify-between border-b px-3 py-2">
                    <span className="text-sm font-medium">Sync 미리보기</span>
                    <Badge variant="secondary">{preview.length}건</Badge>
                  </div>
                  {preview.length === 0 ? (
                    <p className="px-3 py-6 text-center text-sm text-muted-foreground">미리볼 항목이 없습니다.</p>
                  ) : (
                    <div className="w-full overflow-auto">
                      <Table>
                        <TableHeader>
                          <TableRow>
                            <TableHead>MM User ID</TableHead>
                            <TableHead>Username</TableHead>
                            <TableHead>Principal</TableHead>
                            <TableHead>Agent</TableHead>
                            <TableHead>Status</TableHead>
                          </TableRow>
                        </TableHeader>
                        <TableBody>
                          {preview.map((p, idx) => (
                            <TableRow key={`${p.mm_user_id}-${idx}`}>
                              <TableCell className="font-mono text-xs">{p.mm_user_id}</TableCell>
                              <TableCell className="text-xs">{p.mm_username}</TableCell>
                              <TableCell className="font-mono text-xs">{p.employee_principal}</TableCell>
                              <TableCell className="font-mono text-xs text-muted-foreground">{p.agent_id}</TableCell>
                              <TableCell>
                                <Badge variant={p.already_mapped ? "secondary" : mappingStatusVariant(p.status)}>{p.already_mapped ? "이미 매핑" : p.status}</Badge>
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
                <CardTitle className="text-base">매핑 목록</CardTitle>
                <CardDescription>
                  총 {mappings.length}건 · 375px에서도 가로 스크롤로 확인 · MM User ID / Username / Employee Principal / Agent ID / Status / Created / Delete
                </CardDescription>
              </div>
              <Button variant="outline" size="sm" onClick={fetchMappings} disabled={mapLoading}>
                <RefreshCw className={`h-4 w-4 ${mapLoading ? "animate-spin" : ""}`} />
                새로고침
              </Button>
            </CardHeader>
            <CardContent className="p-0">
              <div className="w-full overflow-auto">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead className="min-w-[120px]">MM User ID</TableHead>
                      <TableHead className="min-w-[110px]">Username</TableHead>
                      <TableHead className="min-w-[150px]">Employee Principal</TableHead>
                      <TableHead className="min-w-[150px]">Agent ID</TableHead>
                      <TableHead>Status</TableHead>
                      <TableHead className="min-w-[140px]">Created</TableHead>
                      <TableHead className="text-right">Delete</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {mapLoading ? (
                      <TableRow>
                        <TableCell colSpan={7} className="py-8 text-center text-muted-foreground">
                          로딩 중...
                        </TableCell>
                      </TableRow>
                    ) : mappings.length === 0 ? (
                      <TableRow>
                        <TableCell colSpan={7} className="py-8 text-center text-muted-foreground">
                          등록된 매핑이 없습니다. 위 폼에서 등록하세요.
                        </TableCell>
                      </TableRow>
                    ) : (
                      mappings.map((m) => {
                        const uname = m.mm_username ?? m.username ?? "-";
                        return (
                          <TableRow key={m.id}>
                            <TableCell className="font-mono text-xs" title={m.mm_user_id}>
                              {m.mm_user_id}
                            </TableCell>
                            <TableCell className="text-xs">{uname || "-"}</TableCell>
                            <TableCell className="font-mono text-xs">{m.employee_principal}</TableCell>
                            <TableCell className="font-mono text-xs text-muted-foreground">{m.agent_id}</TableCell>
                            <TableCell>
                              <Badge variant={mappingStatusVariant(m.status)}>{m.status}</Badge>
                            </TableCell>
                            <TableCell className="text-xs text-muted-foreground">{fmtDate(m.created_at)}</TableCell>
                            <TableCell className="text-right">
                              <Button
                                variant="ghost"
                                size="sm"
                                disabled={deletingMap === m.id || !isL5}
                                onClick={() => handleDeleteMapping(m.id)}
                                title={!isL5 ? "L5만 삭제 가능" : "매핑 삭제"}
                                aria-label={`매핑 삭제 ${m.mm_user_id}`}
                              >
                                <Trash2 className="h-4 w-4" />
                                {deletingMap === m.id ? "..." : "삭제"}
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

          <p className="text-xs text-muted-foreground">
            §14 1인 1 Logical Agent: <span className="font-mono">employee:&lt;suffix&gt;</span> ↔ <span className="font-mono">agent:assistant:&lt;suffix&gt;</span>. 미입력 시 username/id로 자동 유도. L4 읽기 / L5 쓰기.
          </p>
        </TabsContent>
      </Tabs>
    </div>
  );
}
