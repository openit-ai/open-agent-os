"use client";
import { useCallback, useEffect, useState, Suspense } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useTableQuery, useToast, useUrlFilter, DataTable, FormField, Skeleton } from "@/components/admin";
import { getToken, getLLMUsageSummary, getLLMUsageHistory, type LLMUsageSummary, type LLMUsageHistoryItem } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { RefreshCw, BarChart3, Coins, Clock3, Activity, Timer, TrendingUp, ExternalLink, Cpu } from "lucide-react";

// Financial Dashboard colors — WCAG AA compliant on white
const C_OK = "#22C55E";
const C_WARN = "#F59E0B";
const C_DANGER = "#DC2626";

function quotaColor(ratio: number): string {
  if (ratio >= 0.9) return C_DANGER;
  if (ratio >= 0.8) return C_WARN;
  return C_OK;
}
function quotaLabel(ratio: number, t: (k: string) => string): { text: string; color: string } {
  if (ratio >= 0.9) return { text: t("llmUsage.quotaExceeded"), color: C_DANGER };
  if (ratio >= 0.8) return { text: t("llmUsage.quotaWarning"), color: C_WARN };
  return { text: t("llmUsage.quotaNormal"), color: C_OK };
}
function latencyColor(ms: number): string {
  if (ms >= 500) return C_DANGER;
  if (ms >= 300) return C_WARN;
  return C_OK;
}
function statusBadgeVariant(s: string): "success" | "danger" | "warning" | "secondary" {
  if (s === "success") return "success";
  if (s === "error") return "danger";
  if (s === "timeout") return "warning";
  return "secondary";
}

// --- lightweight inline SVG sparkline (no external deps) ---
function Sparkline({ data, color, fillOpacity = 0.12, height = 48 }: { data: number[]; color: string; fillOpacity?: number; height?: number }) {
  if (!data.length) return null;
  const w = 200;
  const h = height;
  const pad = 2;
  const min = Math.min(...data);
  const max = Math.max(...data);
  const range = max - min || 1;
  const step = w / (data.length - 1 || 1);
  const points = data.map((v, i) => {
    const x = i * step;
    const y = h - pad - ((v - min) / range) * (h - pad * 2);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  const polyline = points.join(" ");
  const area = `${points.join(" ")} ${w},${h} 0,${h}`;
  return (
    <svg
      role="img"
      aria-label="sparkline"
      viewBox={`0 0 ${w} ${h}`}
      width="100%"
      height={h}
      preserveAspectRatio="none"
      className="overflow-visible"
    >
      <polygon points={area} fill={color} opacity={fillOpacity} />
      <polyline points={polyline} fill="none" stroke={color} strokeWidth={1.8} strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  );
}

function BarChart({ data, color, height = 64, ariaLabel }: { data: number[]; color: string; height?: number; ariaLabel: string }) {
  if (!data.length) return null;
  const max = Math.max(...data, 1);
  return (
    <div
      role="img"
      aria-label={ariaLabel}
      className="flex items-end gap-[2px]"
      style={{ height }}
    >
      {data.map((v, i) => (
        <div
          key={i}
          className="flex-1 rounded-sm transition-all"
          style={{
            height: `${Math.max(4, (v / max) * height)}px`,
            backgroundColor: color,
            opacity: 0.85,
            minWidth: 2,
          }}
          title={`${i}h: ${v}`}
          aria-hidden="true"
        />
      ))}
    </div>
  );
}

function formatNumber(n: unknown): string {
  const v = typeof n === "number" ? n : Number(n);
  if (!Number.isFinite(v)) return "—";
  return v.toLocaleString("ko-KR");
}
function formatCost(n: unknown): string {
  const v = typeof n === "number" ? n : Number(n);
  if (!Number.isFinite(v)) return "—";
  return `$${v.toFixed(4)}`;
}
function safeNum(v: unknown, fallback = 0): number {
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : fallback;
}
function formatTime(iso: string | null | undefined): string {
  if (!iso || typeof iso !== "string") return "—";
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return String(iso);
    return d.toLocaleString("ko-KR", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" });
  } catch {
    return String(iso);
  }
}

function LLMUsagePageContent() {
  const router = useRouter();
  const { t } = useI18n();
  const table = useTableQuery();
  const { toast } = useToast();
  const [tenantFilter, setTenantFilter] = useUrlFilter("tenant");
  const [providerFilter, setProviderFilter] = useUrlFilter("provider");
  const [statusFilter, setStatusFilter] = useUrlFilter("status", "all");
  const [from, setFrom] = useUrlFilter("from");
  const [to, setTo] = useUrlFilter("to");

  const [summary, setSummary] = useState<LLMUsageSummary | null>(null);
  const [history, setHistory] = useState<LLMUsageHistoryItem[]>([]);
  const [historyTotal, setHistoryTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);

  const fetchAll = useCallback(async () => {
    try {
      const [s, h] = await Promise.all([getLLMUsageSummary(), getLLMUsageHistory({
        limit: table.query.pageSize,
        offset: (table.query.page - 1) * table.query.pageSize,
        tenant: tenantFilter || undefined,
        provider: providerFilter || undefined,
        status: statusFilter === "all" ? undefined : statusFilter,
        search: table.query.search,
        sort: table.query.sort?.id ?? "timestamp",
        direction: table.query.sort?.direction ?? "desc",
        from: from ? from + "T00:00:00" : undefined,
        to: to ? to + "T23:59:59.999" : undefined,
      })]);
      // Defensive: api.ts already normalizes, but keep extra guard for mixed shapes
      const rawItems = (h as { items?: unknown[] }).items ?? (Array.isArray(h) ? (h as unknown as unknown[]) : []);
      const items: LLMUsageHistoryItem[] = (rawItems as Record<string, unknown>[]).map((it) => ({
        id: String((it.id as string) ?? ""),
        timestamp: String((it.timestamp as string) ?? (it.created_at as string) ?? ""),
        tenant: String((it.tenant as string) ?? (it.tenant_id as string) ?? "default"),
        provider: String(it.provider ?? "unknown"),
        model: String(it.model ?? ""),
        latency_ms: safeNum(it.latency_ms),
        prompt_tokens: safeNum(it.prompt_tokens),
        completion_tokens: safeNum(it.completion_tokens),
        total_tokens: safeNum(it.total_tokens ?? (safeNum(it.prompt_tokens) + safeNum(it.completion_tokens))),
        cost_usd: safeNum(it.cost_usd),
        status: String(it.status ?? "unknown"),
      }));
      setSummary(s);
      setHistory(items);
      const total = (h as { total?: number }).total ?? (h as { count?: number }).count ?? items.length;
      setHistoryTotal(total);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : t("common.error"));
      if (history.length > 0) toast({ title: t("admin.lists.backgroundError"), description: e instanceof Error ? e.message : undefined, variant: "error" });
    } finally {
      setLoading(false);
    }
  }, [from, history.length, providerFilter, statusFilter, t, table.query, tenantFilter, toast, to]);

  useEffect(() => {
    if (!getToken()) {
      router.replace("/login");
      return;
    }
    fetchAll();
    const id = setInterval(() => {
      setTick((v) => v + 1);
      fetchAll();
    }, 10_000);
    return () => clearInterval(id);
  }, [fetchAll, router]);

  const qRatio = summary?.daily_usage_ratio ?? 0;
  const qColor = quotaColor(qRatio);
  const qInfo = summary ? quotaLabel(qRatio, t) : null;
  const latColor = summary ? latencyColor(summary.p95_latency_ms) : C_OK;

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
            <BarChart3 className="h-6 w-6" aria-hidden="true" />
            {t("llmUsage.title")}
          </h1>
          <p className="text-sm text-muted-foreground">{t("llmUsage.subtitle")}</p>
          {summary?.updated_at && (
            <p className="mt-1 text-xs text-muted-foreground">
              {t("llmUsage.updatedAt")}: {formatTime(summary.updated_at)} · {t("llmUsage.autoPoll")} · tick {tick}
            </p>
          )}
        </div>
        <div className="flex items-center gap-2">
          <Button asChild variant="outline" size="sm">
            <Link href="/providers">
              <ExternalLink className="mr-1 h-4 w-4" aria-hidden="true" />
              {t("llmUsage.viewProviders")}
            </Link>
          </Button>
          <Button variant="outline" size="sm" onClick={() => { setLoading(true); fetchAll(); }} aria-label={t("llmUsage.refresh")}>
            <RefreshCw className="mr-1 h-4 w-4" aria-hidden="true" />
            {t("llmUsage.refresh")}
          </Button>
        </div>
      </div>

      <div className="flex gap-1 border-b" role="tablist" aria-label="providers tabs">
        <Link href="/providers" className="rounded-t-md px-3 py-1.5 text-sm text-muted-foreground hover:bg-accent hover:text-foreground" role="tab" aria-selected="false"><span className="inline-flex items-center gap-1"><Cpu className="h-3.5 w-3.5" />{t("nav.providers")}</span></Link>
        <span className="rounded-t-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground inline-flex items-center gap-1" role="tab" aria-selected="true"><BarChart3 className="h-3.5 w-3.5" />{t("providers.usageTab")}</span>
      </div>

      {error && (
        <p className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive" role="alert">
          {error}
        </p>
      )}

      {loading && !summary ? (
        <p className="text-sm text-muted-foreground">{t("llmUsage.loading")}</p>
      ) : summary ? (
        <>
          {/* Summary cards */}
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            {/* Daily quota */}
            <Card className="overflow-hidden">
              <CardHeader className="pb-2">
                <CardDescription className="flex items-center gap-1.5">
                  <Activity className="h-3.5 w-3.5" aria-hidden="true" /> {t("llmUsage.dailyUsage")}
                </CardDescription>
                <CardTitle className="text-xl">
                  {formatNumber(summary.daily_tokens)} <span className="text-sm font-normal text-muted-foreground">/ {formatNumber(summary.daily_quota)}</span>
                </CardTitle>
              </CardHeader>
              <CardContent className="space-y-2 pt-0">
                <div className="h-2 w-full rounded-full bg-muted" role="progressbar" aria-valuenow={Math.round(qRatio * 100)} aria-valuemin={0} aria-valuemax={100} aria-label={t("llmUsage.quotaProgress")}>
                  <div
                    className="h-2 rounded-full transition-all"
                    style={{ width: `${Math.min(100, qRatio * 100)}%`, backgroundColor: qColor }}
                  />
                </div>
                <div className="flex items-center justify-between text-xs">
                  <span className="font-medium" style={{ color: qInfo?.color }}>
                    {(qRatio * 100).toFixed(1)}% · {qInfo?.text}
                  </span>
                  <Badge variant={qRatio >= 0.9 ? "danger" : qRatio >= 0.8 ? "warning" : "success"} className="text-[11px]">
                    {qRatio >= 0.9 ? t("llmUsage.danger") : qRatio >= 0.8 ? t("llmUsage.warning") : t("llmUsage.normal")}
                  </Badge>
                </div>
                <Sparkline data={summary.hourly_tokens ?? []} color={qColor} />
                <p className="text-[11px] text-muted-foreground">{t("llmUsage.tokensTrend")}</p>
              </CardContent>
            </Card>

            {/* Per-minute */}
            <Card>
              <CardHeader className="pb-2">
                <CardDescription className="flex items-center gap-1.5">
                  <Timer className="h-3.5 w-3.5" aria-hidden="true" /> {t("llmUsage.perMinute")}
                </CardDescription>
                <CardTitle className="text-xl">
                  {formatNumber(summary.per_minute_tokens)} <span className="text-sm font-normal text-muted-foreground">{t("llmUsage.unitTokens")}/min</span>
                </CardTitle>
              </CardHeader>
              <CardContent className="pt-0">
                <p className="text-xs text-muted-foreground">
                  {t("llmUsage.perMinuteLimit")}: {summary.per_minute_limit ? formatNumber(summary.per_minute_limit) : "—"}
                </p>
                {summary.per_minute_limit ? (
                  <div className="mt-2 h-1.5 w-full rounded-full bg-muted" role="progressbar" aria-valuenow={Math.round((summary.per_minute_tokens / summary.per_minute_limit) * 100)} aria-valuemin={0} aria-valuemax={100} aria-label={t("llmUsage.perMinute")}>
                    <div className="h-1.5 rounded-full" style={{ width: `${Math.min(100, (summary.per_minute_tokens / summary.per_minute_limit) * 100)}%`, backgroundColor: C_OK }} />
                  </div>
                ) : null}
                <p className="mt-3 text-xs">
                  {t("llmUsage.totalRequests")}: <strong>{formatNumber(summary.total_requests)}</strong> · {t("llmUsage.successRate")}: <strong style={{ color: summary.success_rate >= 0.95 ? C_OK : summary.success_rate >= 0.9 ? C_WARN : C_DANGER }}>{(summary.success_rate * 100).toFixed(1)}%</strong>
                </p>
              </CardContent>
            </Card>

            {/* Total cost */}
            <Card>
              <CardHeader className="pb-2">
                <CardDescription className="flex items-center gap-1.5">
                  <Coins className="h-3.5 w-3.5" aria-hidden="true" /> {t("llmUsage.totalCost")}
                </CardDescription>
                <CardTitle className="text-xl font-mono">{formatCost(summary.total_cost_usd)}</CardTitle>
              </CardHeader>
              <CardContent className="pt-0 space-y-2">
                <p className="text-xs text-muted-foreground">{t("llmUsage.totalCostDesc")}</p>
                <Sparkline data={summary.hourly_cost ?? []} color={C_OK} fillOpacity={0.14} />
                <p className="text-[11px] text-muted-foreground">{t("llmUsage.costTrend")}</p>
              </CardContent>
            </Card>

            {/* Latency */}
            <Card>
              <CardHeader className="pb-2">
                <CardDescription className="flex items-center gap-1.5">
                  <Clock3 className="h-3.5 w-3.5" aria-hidden="true" /> {t("llmUsage.avgLatency")} / {t("llmUsage.p95Latency")}
                </CardDescription>
                <CardTitle className="text-xl">
                  <span style={{ color: latencyColor(summary.avg_latency_ms) }}>{summary.avg_latency_ms}</span>
                  <span className="text-sm font-normal text-muted-foreground"> {t("llmUsage.unitMs")}</span>
                  <span className="mx-1 text-muted-foreground">/</span>
                  <span style={{ color: latColor }}>{summary.p95_latency_ms}</span>
                  <span className="text-sm font-normal text-muted-foreground"> {t("llmUsage.unitMs")}</span>
                </CardTitle>
              </CardHeader>
              <CardContent className="pt-0 space-y-2">
                <div className="flex gap-2 text-xs">
                  <span>
                    {t("llmUsage.p50Latency")}: {summary.p50_latency_ms ?? "—"}ms
                  </span>
                  <span>
                    {t("llmUsage.p99Latency")}: {summary.p99_latency_ms ?? "—"}ms
                  </span>
                  <Badge variant={summary.p95_latency_ms >= 500 ? "danger" : summary.p95_latency_ms >= 300 ? "warning" : "success"} className="ml-auto text-[11px]">
                    {summary.p95_latency_ms >= 500 ? t("llmUsage.danger") : summary.p95_latency_ms >= 300 ? t("llmUsage.warning") : t("llmUsage.normal")}
                  </Badge>
                </div>
                <Sparkline data={summary.hourly_latency ?? []} color={latColor} />
                <p className="text-[11px] text-muted-foreground">{t("llmUsage.latencyTrend")}</p>
              </CardContent>
            </Card>
          </div>

          {/* Charts row */}
          <div className="grid gap-4 lg:grid-cols-3">
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="flex items-center gap-1.5 text-sm">
                  <TrendingUp className="h-4 w-4" aria-hidden="true" /> {t("llmUsage.usageChart")}
                </CardTitle>
              </CardHeader>
              <CardContent>
                <BarChart data={summary.hourly_tokens ?? []} color={C_OK} ariaLabel={t("llmUsage.usageChart")} />
                <div className="mt-1 flex justify-between text-[10px] text-muted-foreground" aria-hidden="true">
                  <span>00h</span>
                  <span>12h</span>
                  <span>23h</span>
                </div>
              </CardContent>
            </Card>
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="flex items-center gap-1.5 text-sm">
                  <Coins className="h-4 w-4" aria-hidden="true" /> {t("llmUsage.costChart")}
                </CardTitle>
              </CardHeader>
              <CardContent>
                <BarChart data={(summary.hourly_cost ?? []).map((v) => Math.round(v * 10000))} color="#0EA5E9" ariaLabel={t("llmUsage.costChart")} />
                <div className="mt-1 flex justify-between text-[10px] text-muted-foreground" aria-hidden="true">
                  <span>00h</span>
                  <span>12h</span>
                  <span>23h</span>
                </div>
              </CardContent>
            </Card>
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="flex items-center gap-1.5 text-sm">
                  <Clock3 className="h-4 w-4" aria-hidden="true" /> {t("llmUsage.latencyChart")}
                </CardTitle>
              </CardHeader>
              <CardContent>
                <BarChart data={summary.hourly_latency ?? []} color={latColor} ariaLabel={t("llmUsage.latencyChart")} />
                <div className="mt-1 flex justify-between text-[10px] text-muted-foreground" aria-hidden="true">
                  <span>00h</span>
                  <span>12h</span>
                  <span>23h</span>
                </div>
              </CardContent>
            </Card>
          </div>
        </>
      ) : null}

      {/* History table */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">{t("llmUsage.historyTitle")}</CardTitle>
          <CardDescription>{t("llmUsage.historyDesc", { count: String(historyTotal) })}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
            <FormField id="usage-tenant" label={t("llmUsage.colTenant")}><input value={tenantFilter} onChange={(event) => setTenantFilter(event.target.value)} className="flex h-9 w-full rounded-md border bg-background px-3 text-sm" /></FormField>
            <FormField id="usage-provider" label={t("llmUsage.colProvider")}><input value={providerFilter} onChange={(event) => setProviderFilter(event.target.value)} className="flex h-9 w-full rounded-md border bg-background px-3 text-sm" /></FormField>
            <FormField id="usage-status" label={t("llmUsage.colStatus")}><select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)} className="flex h-9 w-full rounded-md border bg-background px-3 text-sm"><option value="all">{t("llmUsage.filterAll")}</option><option value="success">success</option><option value="error">error</option><option value="timeout">timeout</option></select></FormField>
            <FormField id="usage-from" label={t("admin.lists.fromDate")}><input type="date" value={from} onChange={(event) => setFrom(event.target.value)} className="flex h-9 w-full rounded-md border bg-background px-3 text-sm" /></FormField>
            <FormField id="usage-to" label={t("admin.lists.toDate")}><input type="date" value={to} onChange={(event) => setTo(event.target.value)} className="flex h-9 w-full rounded-md border bg-background px-3 text-sm" /></FormField>
          </div>
          <DataTable
            rows={history}
            columns={[
              { id: "timestamp", header: t("llmUsage.colTime"), cell: (row) => formatTime(row.timestamp), sortable: true },
              { id: "tenant", header: t("llmUsage.colTenant"), accessor: (row) => row.tenant, sortable: true },
              { id: "provider", header: t("llmUsage.colProvider"), accessor: (row) => row.provider, sortable: true },
              { id: "model", header: t("llmUsage.colModel"), accessor: (row) => row.model, sortable: true },
              { id: "latency", header: t("llmUsage.colLatency"), cell: (row) => `${row.latency_ms}ms`, sortable: true, align: "end" },
              { id: "tokens", header: t("llmUsage.colTokens"), accessor: (row) => formatNumber(row.total_tokens), sortable: true, align: "end" },
              { id: "cost", header: t("llmUsage.colCost"), accessor: (row) => formatCost(row.cost_usd), sortable: true, align: "end" },
              { id: "status", header: t("llmUsage.colStatus"), cell: (row) => <Badge variant={statusBadgeVariant(row.status)}>{row.status}</Badge>, sortable: true },
            ]}
            rowKey={(row) => row.id}
            loading={loading && history.length === 0}
            query={table.query}
            onQueryChange={table.onQueryChange}
            totalRows={historyTotal}
            searchable
            empty={{ title: t("llmUsage.noHistory"), description: t("llmUsage.historyTitle"), filtered: Boolean(table.query.search || tenantFilter || providerFilter || statusFilter !== "all" || from || to) }}
            ariaLabel={t("llmUsage.historyTitle")}
          />
          <p className="text-xs text-muted-foreground">{t("admin.usage.chartAlternative")} · 10초마다 자동 갱신</p>
        </CardContent>
      </Card>
    </div>
  );
}


/**
 * `useTableQuery`/`useUrlFilter` call `useSearchParams()`, which Next.js 15 requires
 * to sit under a Suspense boundary during prerendering.
 */
export default function LLMUsagePage() {
  return (
    <Suspense fallback={<Skeleton variant="table" ariaLabel="Loading" />}>
      <LLMUsagePageContent />
    </Suspense>
  );
}
