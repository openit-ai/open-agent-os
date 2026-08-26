"use client";
import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { getToken, getCredentialsStatus, type CredentialsStatusResponse } from "@/lib/api";
import { KeyRound, RefreshCw, ShieldAlert } from "lucide-react";

function formatTime(iso?: string | null) {
  if (!iso) return "-";
  try {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    return d.toLocaleString("ko-KR", { year: "2-digit", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
  } catch { return iso; }
}

function statusVariant(s: string) {
  const u = s.toUpperCase();
  if (u === "ACTIVE") return "success" as const;
  if (u === "REVOKED") return "danger" as const;
  if (u === "EXPIRED") return "warning" as const;
  return "secondary" as const;
}

export default function CredentialsPage() {
  const router = useRouter();
  const [data, setData] = useState<CredentialsStatusResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchData = useCallback(async () => {
    setError(null);
    try {
      const res = await getCredentialsStatus();
      setData(res);
    } catch (e) {
      setError(e instanceof Error ? e.message : "조회 실패");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!getToken()) { router.replace("/login"); return; }
    fetchData();
  }, [fetchData, router]);

  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-semibold"><KeyRound className="h-6 w-6" /> Credentials</h1>
          <p className="text-sm text-muted-foreground">Provider별 자격증명 현황 · 활성/만료/취소 집계 · 최근 위임 목록</p>
        </div>
        <Button variant="outline" size="sm" onClick={() => { setLoading(true); fetchData(); }} disabled={loading}>
          <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} /> 새로고침
        </Button>
      </div>

      {error && <div className="rounded-md border border-destructive/50 bg-destructive/10 px-3 py-2 text-sm text-destructive" role="alert">{error}</div>}

      {/* summary cards */}
      <div className="grid gap-4 sm:grid-cols-4">
        <Card><CardHeader className="pb-2"><CardTitle className="text-sm">전체</CardTitle></CardHeader><CardContent><div className="text-2xl font-bold">{data?.total ?? (loading ? "-" : 0)}</div><CardDescription>모든 위임</CardDescription></CardContent></Card>
        <Card><CardHeader className="pb-2"><CardTitle className="text-sm text-[#22C55E]">활성</CardTitle></CardHeader><CardContent><div className="text-2xl font-bold text-[#22C55E]">{data?.active ?? (loading ? "-" : 0)}</div><CardDescription>사용 가능</CardDescription></CardContent></Card>
        <Card><CardHeader className="pb-2"><CardTitle className="text-sm text-[#DC2626]">취소</CardTitle></CardHeader><CardContent><div className="text-2xl font-bold text-[#DC2626]">{data?.revoked ?? (loading ? "-" : 0)}</div><CardDescription>revoked</CardDescription></CardContent></Card>
        <Card><CardHeader className="pb-2"><CardTitle className="text-sm text-[#F59E0B]">만료</CardTitle></CardHeader><CardContent><div className="text-2xl font-bold text-[#F59E0B]">{data?.expired ?? (loading ? "-" : 0)}</div><CardDescription>expired</CardDescription></CardContent></Card>
      </div>

      {/* provider table */}
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-base">Provider별 현황</CardTitle>
          <CardDescription>provider · 활성 / 만료 / 취소 · bindings</CardDescription>
        </CardHeader>
        <CardContent className="p-0">
          {loading ? (
            <div className="px-6 py-12 text-center text-sm text-muted-foreground">로딩 중...</div>
          ) : !data || data.providers.length === 0 ? (
            <div className="flex flex-col items-center justify-center px-6 py-16 text-center">
              <div className="mb-3 flex h-12 w-12 items-center justify-center rounded-full bg-muted"><ShieldAlert className="h-6 w-6 text-muted-foreground" /></div>
              <p className="text-sm font-medium">위임 데이터가 없습니다</p>
              <p className="mt-1 text-xs text-muted-foreground">delegation/grant를 통해 자격증명 위임이 생성되면 여기에 표시됩니다.</p>
            </div>
          ) : (
            <div className="relative w-full overflow-auto">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Provider</TableHead>
                    <TableHead className="text-right">활성</TableHead>
                    <TableHead className="text-right">취소</TableHead>
                    <TableHead className="text-right">만료</TableHead>
                    <TableHead className="text-right">Bindings</TableHead>
                    <TableHead className="text-right">전체</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {data.providers.map((p) => (
                    <TableRow key={p.provider}>
                      <TableCell className="font-medium">{p.provider}</TableCell>
                      <TableCell className="text-right"><Badge variant="success">{p.active}</Badge></TableCell>
                      <TableCell className="text-right"><Badge variant="danger">{p.revoked}</Badge></TableCell>
                      <TableCell className="text-right"><Badge variant="warning">{p.expired}</Badge></TableCell>
                      <TableCell className="text-right text-muted-foreground">{p.bindings ?? "-"}</TableCell>
                      <TableCell className="text-right font-mono">{p.total}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
        </CardContent>
      </Card>

      {/* recent delegations */}
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-base">최근 위임 목록</CardTitle>
          <CardDescription>최근 10건 · 생성시각 역순</CardDescription>
        </CardHeader>
        <CardContent className="p-0">
          {loading ? (
            <div className="px-6 py-12 text-center text-sm text-muted-foreground">로딩 중...</div>
          ) : !data || data.recent.length === 0 ? (
            <div className="px-6 py-12 text-center text-sm text-muted-foreground">최근 위임이 없습니다.</div>
          ) : (
            <div className="relative w-full overflow-auto">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="min-w-[130px]">ID</TableHead>
                    <TableHead>User</TableHead>
                    <TableHead>Agent</TableHead>
                    <TableHead>Provider</TableHead>
                    <TableHead>Scope</TableHead>
                    <TableHead>상태</TableHead>
                    <TableHead className="min-w-[130px]">생성</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {data.recent.map((r) => (
                    <TableRow key={r.id}>
                      <TableCell className="font-mono text-xs" title={r.id}>{r.id}</TableCell>
                      <TableCell className="text-xs">{r.user_id}</TableCell>
                      <TableCell className="text-xs">{r.agent_id}</TableCell>
                      <TableCell className="text-xs">{r.provider}</TableCell>
                      <TableCell className="max-w-[160px] truncate text-xs" title={r.scope}>{r.scope}</TableCell>
                      <TableCell><Badge variant={statusVariant(r.status)}>{r.status}</Badge></TableCell>
                      <TableCell className="text-xs text-muted-foreground">{formatTime(r.created_at)}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
