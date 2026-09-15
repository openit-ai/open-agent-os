"use client";
import { useEffect, useState } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { apiFetch, getToken } from "@/lib/api";
import { useRouter } from "next/navigation";
import { Activity, Users, ClipboardCheck, ScrollText } from "lucide-react";
import { useI18n } from "@/lib/i18n";
import { ErrorState, RequiredConnectionsSummary, Skeleton } from "@/components/admin";
import { getAdminReadiness, getSetupProgress, normalizeAdminError } from "@/lib/admin-api/client";
import { adminKeys } from "@/lib/admin-api/keys";
import { isSetupDeferredForSession } from "@/lib/setup-session";

interface DashboardStats { total_users: number; total_agents: number; pending_approvals: number; audit_events_today: number; }
interface InfraItem { id: string; service: string; status: string; host: string; port: number; latency_ms: number | null; last_check: string | null; }
interface ApprovalItem { id: string; resource: string; action: string; status: string; created_at: string; }
interface AuditChain { head_hash: string | null; chain_length: number; verified: boolean; last_checkpoint: string | null; }

export default function DashboardPage() {
  const router = useRouter();
  const { t } = useI18n();
  const [stats, setStats] = useState<DashboardStats | null>(null);
  const [infra, setInfra] = useState<InfraItem[]>([]);
  const [approvals, setApprovals] = useState<ApprovalItem[]>([]);
  const [audit, setAudit] = useState<AuditChain | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [setupDeferred, setSetupDeferred] = useState(false);
  const authenticated = Boolean(getToken());

  const setupQuery = useQuery({
    queryKey: adminKeys.setupProgress(),
    queryFn: ({ signal }) => getSetupProgress(signal),
    enabled: authenticated,
    retry: false,
  });
  const readinessQuery = useQuery({
    queryKey: adminKeys.readiness(),
    queryFn: ({ signal }) => getAdminReadiness(signal),
    enabled: authenticated,
    retry: false,
  });

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
      } catch (e) { if (!cancelled) setError(e instanceof Error ? e.message : t("common.error")); }
    }
    load();
    return () => { cancelled = true; };
  }, [router, t]);

  useEffect(() => {
    if (!setupQuery.data || setupQuery.data.required_complete) return;
    if (isSetupDeferredForSession()) {
      setSetupDeferred(true);
      return;
    }
    router.replace("/setup");
  }, [router, setupQuery.data]);

  const healthy = infra.filter((x) => x.status === "healthy").length;
  const unhealthy = infra.filter((x) => x.status === "unhealthy").length;
  const unknown = infra.length - healthy - unhealthy;

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold">{t("dashboard.title")}</h1>
      {error && <p className="text-sm text-[#DC2626]" role="alert">{error}</p>}

      {readinessQuery.isPending ? <Skeleton variant="card" ariaLabel={t("admin.readiness.summary.checking")} /> : null}
      {readinessQuery.isError ? (() => {
        const readinessError = normalizeAdminError(readinessQuery.error);
        return <ErrorState title={t("admin.readiness.summary.loadFailed")} description={t(readinessError.message_key)} code={readinessError.code} correlationId={readinessError.correlation_id} retry={() => void readinessQuery.refetch()} />;
      })() : null}
      {readinessQuery.data ? <RequiredConnectionsSummary readiness={readinessQuery.data} checking={readinessQuery.isFetching} onRecheck={() => void readinessQuery.refetch()} /> : null}

      {setupQuery.data && !setupQuery.data.required_complete && setupDeferred ? (
        <section className="rounded-lg border border-status-warn bg-status-warn-surface p-4 text-status-warn-text" aria-labelledby="continue-setup-title">
          <h2 id="continue-setup-title" className="font-semibold">{t("admin.readiness.summary.continueSetupTitle")}</h2>
          <p className="mt-1 text-sm">{t("admin.readiness.summary.continueSetupDescription")}</p>
          <Link href={`/setup?step=${setupQuery.data.current_step}`} className="mt-3 inline-flex h-9 items-center rounded-md border border-status-warn bg-background px-4 text-sm font-medium">{t("admin.readiness.summary.continueSetupAction")}</Link>
        </section>
      ) : null}

      {/* 통계 카드 */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Card><CardHeader className="flex flex-row items-center justify-between pb-2"><CardTitle className="text-sm font-medium">{t("dashboard.users")}</CardTitle><Users className="h-4 w-4 text-muted-foreground" /></CardHeader><CardContent><div className="text-2xl font-bold">{stats?.total_users ?? "-"}</div><p className="text-xs text-muted-foreground">{t("dashboard.usersDesc")}</p></CardContent></Card>
        <Card><CardHeader className="flex flex-row items-center justify-between pb-2"><CardTitle className="text-sm font-medium">{t("dashboard.agents")}</CardTitle><Activity className="h-4 w-4 text-muted-foreground" /></CardHeader><CardContent><div className="text-2xl font-bold">{stats?.total_agents ?? "-"}</div><p className="text-xs text-muted-foreground">{t("dashboard.agentsDesc")}</p></CardContent></Card>
        <Card><CardHeader className="flex flex-row items-center justify-between pb-2"><CardTitle className="text-sm font-medium">{t("dashboard.pendingApprovals")}</CardTitle><ClipboardCheck className="h-4 w-4 text-muted-foreground" /></CardHeader><CardContent><div className="text-2xl font-bold">{stats?.pending_approvals ?? "-"}</div><p className="text-xs text-muted-foreground">{t("dashboard.pendingApprovalsDesc")}</p></CardContent></Card>
        <Card><CardHeader className="flex flex-row items-center justify-between pb-2"><CardTitle className="text-sm font-medium">{t("dashboard.auditToday")}</CardTitle><ScrollText className="h-4 w-4 text-muted-foreground" /></CardHeader><CardContent><div className="text-2xl font-bold">{stats?.audit_events_today ?? "-"}</div><p className="text-xs text-muted-foreground">{t("dashboard.auditTodayDesc")}</p></CardContent></Card>
      </div>

      {/* Infra 상태 요약 3종 */}
      <div className="grid gap-4 sm:grid-cols-3">
        <Card><CardHeader><CardTitle className="text-sm">{t("dashboard.infraHealthy")}</CardTitle></CardHeader><CardContent><div className="text-3xl font-bold text-[#22C55E]">{healthy}</div><CardDescription>{infra.length} {t("dashboard.infraHealthyDesc")}</CardDescription></CardContent></Card>
        <Card><CardHeader><CardTitle className="text-sm">{t("dashboard.unhealthy")}</CardTitle></CardHeader><CardContent><div className="text-3xl font-bold text-[#DC2626]">{unhealthy}</div><CardDescription>{t("dashboard.unhealthyDesc")}</CardDescription></CardContent></Card>
        <Card><CardHeader><CardTitle className="text-sm">{t("dashboard.unknown")}</CardTitle></CardHeader><CardContent><div className="text-3xl font-bold text-[#F59E0B]">{unknown}</div><CardDescription>{t("dashboard.unknownDesc")}</CardDescription></CardContent></Card>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        {/* 최근 approvals */}
        <Card>
          <CardHeader><CardTitle>{t("dashboard.recentApprovals")}</CardTitle><CardDescription>{t("dashboard.recentApprovalsDesc")}</CardDescription></CardHeader>
          <CardContent>
            {approvals.length === 0 ? <p className="text-sm text-muted-foreground">{t("dashboard.noData")}</p> : (
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
          <CardHeader><CardTitle>{t("dashboard.auditChain")}</CardTitle><CardDescription>{t("dashboard.auditChainDesc")}</CardDescription></CardHeader>
          <CardContent className="space-y-2 text-sm">
            {audit ? (
              <>
                <div className="flex justify-between"><span className="text-muted-foreground">{t("dashboard.chainLength")}</span><span className="font-mono">{audit.chain_length}</span></div>
                <div className="flex justify-between"><span className="text-muted-foreground">{t("dashboard.verified")}</span>{audit.verified ? <Badge variant="success">verified</Badge> : <Badge variant="danger">tampered</Badge>}</div>
                <div className="flex justify-between gap-2"><span className="text-muted-foreground shrink-0">{t("dashboard.headHash")}</span><span className="truncate font-mono text-xs">{audit.head_hash ?? "-"}</span></div>
                <div className="flex justify-between"><span className="text-muted-foreground">{t("dashboard.lastCheckpoint")}</span><span className="font-mono text-xs">{audit.last_checkpoint ?? "-"}</span></div>
              </>
            ) : <p className="text-muted-foreground">{t("dashboard.loading")}</p>}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
