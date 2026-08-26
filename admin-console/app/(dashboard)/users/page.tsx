"use client";
import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { apiFetch, getToken, getMe, listUsers, registerUser, deleteUser, type AdminUserPublic } from "@/lib/api";
import { Trash2, RefreshCw, UserPlus } from "lucide-react";

export default function UsersPage() {
  const router = useRouter();
  const [users, setUsers] = useState<AdminUserPublic[]>([]);
  const [me, setMe] = useState<AdminUserPublic | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);

  // form
  const [email, setEmail] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<"L5" | "L4">("L4");
  const [formError, setFormError] = useState<string | null>(null);
  const [formLoading, setFormLoading] = useState(false);

  const fetchAll = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [meData, usersData] = await Promise.all([getMe(), listUsers()]);
      setMe(meData as AdminUserPublic);
      const list: AdminUserPublic[] = Array.isArray(usersData)
        ? (usersData as AdminUserPublic[])
        : ((usersData as { users?: AdminUserPublic[] }).users ?? (usersData as unknown as AdminUserPublic[]));
      // If response is array, use it; if wrapped, unwrap; fallback empty
      const arr = Array.isArray(usersData) ? usersData : (usersData as any).users ?? usersData;
      // Normalize to array
      const normalized: AdminUserPublic[] = Array.isArray(arr) ? arr : Array.isArray(usersData) ? (usersData as AdminUserPublic[]) : [];
      // If still not correct, handle direct array case already handled
      // For safety, if usersData is array, use it
      if (Array.isArray(usersData)) setUsers(usersData as AdminUserPublic[]);
      else if ((usersData as any).users) setUsers((usersData as any).users);
      else setUsers(normalized as AdminUserPublic[]);
    } catch (e) {
      setError(e instanceof Error ? e.message : "조회 실패");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!getToken()) {
      router.replace("/login");
      return;
    }
    fetchAll();
  }, [fetchAll, router]);

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
      await fetchAll();
    } catch (e) {
      setFormError(e instanceof Error ? e.message : "등록 실패");
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
      await fetchAll();
    } catch (e) {
      alert(e instanceof Error ? e.message : "삭제 실패");
    } finally {
      setDeleting(null);
    }
  }

  const isL5 = me?.role === "L5";

  return (
    <div className="mx-auto w-full max-w-[1200px] space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Users / Agents</h1>
        <Button variant="outline" size="sm" onClick={fetchAll} disabled={loading}>
          <RefreshCw className="mr-1 h-4 w-4" />
          새로고침
        </Button>
      </div>

      {error && (
        <p className="rounded-md bg-[#DC2626]/10 p-3 text-sm text-[#DC2626]" role="alert">
          {error}
        </p>
      )}

      {/* Register form - L5 only */}
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

      {/* Users table */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">사용자 목록</CardTitle>
          <CardDescription>
            총 {users.length}명 · 375px 모바일에서도 가로 스크롤로 확인 가능
          </CardDescription>
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
                        <TableCell className="text-xs text-muted-foreground">
                          {u.created_at ? new Date(u.created_at).toLocaleString("ko-KR") : "-"}
                        </TableCell>
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
    </div>
  );
}
