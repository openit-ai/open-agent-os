"use client";
import { useEffect, useState } from "react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { apiFetch, getToken } from "@/lib/api";
import { useRouter } from "next/navigation";
import { Activity, Users, ClipboardCheck, ScrollText } from "lucide-react";

interface DashboardStats { total_users: number; total_agents: number; pending_approvals: number; audit_events_today: number; }
interface InfraItem { id: string; service: string; status: string; host: string; port: number; latency_ms: number | null; last_check: string | null; }
interface ApprovalItem { id: string; resource: string; action: string; status: string; created_at: string; }
interface AuditChain { head_hash: string | null; chain_length: number; verified: boolean; last_checkpoint: string | null; }

export default function DashboardPage() {
  const router = useRouter();
  const [stats, setStats] = useState<DashboardStats | null>(null);
  const [infra, setInfra] = useState<InfraItem[]>([]);
  const [approvals, setApprovals] = useState<ApprovalItem[]>([]);
  const [audit, setAudit] = useState<AuditChain | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!getToken()) { router.replace("/login"); return; }
    let cancelled = false;
    async function load() {
      try {
        const [s, i, a, c] = await Promise.allSettled([
          apiFetch<DashboardStats>("/v1/dashboard/stats"),
          apiFetch<{ items: InfraItem[] } | InfraItem[]>("/v1/infra"),
          apiFetch<{ items: ApprovalItem[] } | ApprovalItem[]>("/v1/approvals?limit=5"),
          apiFetch<AuditChain>("/v1/audit/chain"),
        ]);
        if (cancelled) return;
        if (s.status === "fulfilled") setStats(s.value);
        if (i.status === "fulfilled") {
          const v = i.value as unknown as { items: InfraItem[] };
          setInfra(Array.isArray(v) ? v : v.items ?? []);
        }
        if (a.status === "fulfilled") {
          const v = a.value as unknown as { items: ApprovalItem[] };
          setApprovals(Array.isArray(v) ? v.slice(0,5) : (v.items ?? []).slice(0,5));
        }
        if (c.status === "fulfilled") setAudit(c.value);
      } catch (e) { if (!cancelled) setError(e instanceof Error ? e.message : "로드 실패"); }
    }
    load();
    return () => { cancelled = true; };
  }, [router]);

  const healthy = infra.filter((x) => x.status === "healthy").length;
  const unhealthy = infra.filter((x) => x.status === "unhealthy").length;
  const unknown = infra.length - healthy - unhealthy;

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold">Dashboard</h1>
      {error && <p className="text-sm text-[#DC2626]" role="alert">{error}</p>}

      {/* 통계 카드 */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Card><CardHeader className="flex flex-row items-center justify-between pb-2"><CardTitle className="text-sm font-medium">Users</CardTitle><Users className="h-4 w-4 text-muted-foreground" /></CardHeader><CardContent><div className="text-2xl font-bold">{stats?.total_users ?? "-"}</div><p className="text-xs text-muted-foreground">등록 사용자</p></CardContent></Card>
        <Card><CardHeader className="flex flex-row items-center justify-between pb-2"><CardTitle className="text-sm font-medium">Agents</CardTitle><Activity className="h-4 w-4 text-muted-foreground" /></CardHeader><CardContent><div className="text-2xl font-bold">{stats?.total_agents ?? "-"}</div><p className="text-xs text-muted-foreground">Logical Personal Agents</p></CardContent></Card>
        <Card><CardHeader className="flex flex-row items-center justify-between pb-2"><CardTitle className="text-sm font-medium">Pending Approvals</CardTitle><ClipboardCheck className="h-4 w-4 text-muted-foreground" /></CardHeader><CardContent><div className="text-2xl font-bold">{stats?.pending_approvals ?? "-"}</div><p className="text-xs text-muted-foreground">대기 중 승인</p></CardContent></Card>
        <Card><CardHeader className="flex flex-row items-center justify-between pb-2"><CardTitle className="text-sm font-medium">Audit Today</CardTitle><ScrollText className="h-4 w-4 text-muted-foreground" /></CardHeader><CardContent><div className="text-2xl font-bold">{stats?.audit_events_today ?? "-"}</div><p className="text-xs text-muted-foreground">오늘 감사 이벤트</p></CardContent></Card>
      </div>

      {/* Infra 상태 요약 3종 */}
      <div className="grid gap-4 sm:grid-cols-3">
        <Card><CardHeader><CardTitle className="text-sm">Infra Healthy</CardTitle></CardHeader><CardContent><div className="text-3xl font-bold text-[#22C55E]">{healthy}</div><CardDescription>{infra.length} 중 정상</CardDescription></CardContent></Card>
        <Card><CardHeader><CardTitle className="text-sm">Unhealthy</CardTitle></CardHeader><CardContent><div className="text-3xl font-bold text-[#DC2626]">{unhealthy}</div><CardDescription>즉시 확인 필요</CardDescription></CardContent></Card>
        <Card><CardHeader><CardTitle className="text-sm">Unknown</CardTitle></CardHeader><CardContent><div className="text-3xl font-bold text-[#F59E0B]">{unknown}</div><CardDescription>점검 대기</CardDescription></CardContent></Card>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        {/* 최근 approvals */}
        <Card>
          <CardHeader><CardTitle>최근 Approvals</CardTitle><CardDescription>최근 5건</CardDescription></CardHeader>
          <CardContent>
            {approvals.length === 0 ? <p className="text-sm text-muted-foreground">데이터 없음</p> : (
              <ul className="space-y-2">
                {approvals.map((a) => (
                  <li key={a.id} className="flex items-center justify-between rounded border px-3 py-2 text-sm">
                    <span className="truncate">{a.action} — {a.resource}</span>
                    <Badge variant={a.status === "pending" ? "warning" : a.status === "approved" ? "success" : "secondary"}>{a.status}</Badge>
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>

        {/* Audit 체인 상태 */}
        <Card>
          <CardHeader><CardTitle>Audit Chain 상태</CardTitle><CardDescription>Hash-chain 무결성</CardDescription></CardHeader>
          <CardContent className="space-y-2 text-sm">
            {audit ? (
              <>
                <div className="flex justify-between"><span className="text-muted-foreground">Chain length</span><span className="font-mono">{audit.chain_length}</span></div>
                <div className="flex justify-between"><span className="text-muted-foreground">Verified</span>{audit.verified ? <Badge variant="success">verified</Badge> : <Badge variant="danger">tampered</Badge>}</div>
                <div className="flex justify-between gap-2"><span className="text-muted-foreground shrink-0">Head hash</span><span className="truncate font-mono text-xs">{audit.head_hash ?? "-"}</span></div>
                <div className="flex justify-between"><span className="text-muted-foreground">Last checkpoint</span><span className="font-mono text-xs">{audit.last_checkpoint ?? "-"}</span></div>
              </>
            ) : <p className="text-muted-foreground">로드 중...</p>}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
