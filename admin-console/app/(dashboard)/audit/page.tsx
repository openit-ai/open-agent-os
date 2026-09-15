"use client";

import { useEffect, useMemo, Suspense } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { RefreshCw, ScrollText, ShieldCheck } from "lucide-react";
import { useBackgroundQueryToast, useTableQuery, useToast, useUrlFilter, DataTable, ErrorState, FormField, Skeleton } from "@/components/admin";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { applyClientListQuery } from "@/lib/admin-api/list-query";
import { normalizeAdminError } from "@/lib/admin-api/client";
import { adminKeys } from "@/lib/admin-api/keys";
import { getAuditCheckpoint, getAuditEvents, getToken, verifyAuditChain } from "@/lib/api";
import { useI18n } from "@/lib/i18n";

function formatTime(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function AuditPageContent() {
  const router = useRouter();
  const { t } = useI18n();
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const table = useTableQuery();
  const [from, setFrom] = useUrlFilter("from");
  const [to, setTo] = useUrlFilter("to");

  useEffect(() => { if (!getToken()) router.replace("/login"); }, [router]);

  const audit = useQuery({
    queryKey: adminKeys.list("audit", table.query),
    queryFn: async () => {
      const [events, checkpoint, verification] = await Promise.all([
        getAuditEvents(),
        getAuditCheckpoint().catch(() => null),
        verifyAuditChain().catch(() => null),
      ]);
      return { events, checkpoint, verification };
    },
  });
  const sourceRows = audit.data?.events.events ?? [];
  useBackgroundQueryToast(audit.error, sourceRows.length > 0, t("admin.lists.backgroundError"));

  const verify = useMutation({
    mutationFn: verifyAuditChain,
    onSuccess: async (result) => {
      toast({ title: result.chain_valid ? t("admin.audit.integrityValid") : t("admin.audit.integrityInvalid"), variant: result.chain_valid ? "success" : "error" });
      await queryClient.invalidateQueries({ queryKey: adminKeys.lists("audit") });
    },
    onError: (error) => toast({ title: t("common.verifyFailed"), description: error instanceof Error ? error.message : undefined, variant: "error" }),
  });

  const datedRows = useMemo(() => sourceRows.filter((row) => {
    const timestamp = new Date(row.timestamp).getTime();
    if (from && timestamp < new Date(from + "T00:00:00").getTime()) return false;
    if (to && timestamp > new Date(to + "T23:59:59.999").getTime()) return false;
    return true;
  }), [from, sourceRows, to]);
  // Audit events currently arrive as one compatible response; client filtering/paging is used until additive server parameters exist.
  const visible = useMemo(() => applyClientListQuery(
    datedRows,
    table.query.sort ? table.query : { ...table.query, sort: { id: "timestamp", direction: "desc" } },
    [(row) => row.event_type, (row) => row.user_id, (row) => row.agent_id, (row) => row.action, (row) => row.resource],
    { timestamp: (row) => row.timestamp, actor: (row) => row.user_id ?? row.agent_id ?? "", event: (row) => row.event_type },
  ), [datedRows, table.query]);
  const integrityFailed = audit.data?.verification && !audit.data.verification.chain_valid;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div><h1 className="flex items-center gap-2 text-2xl font-semibold"><ScrollText aria-hidden="true" className="h-6 w-6" />{t("audit.title")}</h1><p className="text-sm text-muted-foreground">{t("audit.subtitle")}</p></div>
        <div className="flex gap-2">
          <Button variant="outline" size="sm" disabled={audit.isFetching} onClick={() => audit.refetch()}><RefreshCw aria-hidden="true" className="h-4 w-4" />{t("common.refresh")}</Button>
          <Button size="sm" disabled={verify.isPending} onClick={() => verify.mutate()}><ShieldCheck aria-hidden="true" className="h-4 w-4" />{t("common.verify")}</Button>
        </div>
      </div>
      {integrityFailed ? <ErrorState title={t("admin.audit.integrityInvalid")} description={t("admin.audit.integrityInvalidDescription")} code="AUDIT_INTEGRITY_FAILED" retry={() => verify.mutate()} /> : null}
      <div className="grid gap-3 sm:grid-cols-2">
        <FormField id="audit-from" label={t("admin.lists.fromDate")}><input type="date" value={from} onChange={(event) => setFrom(event.target.value)} className="flex h-9 w-full rounded-md border bg-background px-3 text-sm" /></FormField>
        <FormField id="audit-to" label={t("admin.lists.toDate")}><input type="date" value={to} onChange={(event) => setTo(event.target.value)} className="flex h-9 w-full rounded-md border bg-background px-3 text-sm" /></FormField>
      </div>
      <div className="grid gap-3 sm:grid-cols-3">
        <Card><CardHeader className="pb-2"><CardTitle className="text-sm">{t("audit.eventCount")}</CardTitle></CardHeader><CardContent className="text-2xl font-semibold">{audit.data?.events.count ?? sourceRows.length}</CardContent></Card>
        <Card><CardHeader className="pb-2"><CardTitle className="text-sm">{t("audit.chainIntegrity")}</CardTitle></CardHeader><CardContent><Badge variant={audit.data?.verification?.chain_valid ? "success" : "secondary"}>{audit.data?.verification?.chain_valid ? "valid" : "unknown"}</Badge></CardContent></Card>
        <Card><CardHeader className="pb-2"><CardTitle className="text-sm">Checkpoint</CardTitle></CardHeader><CardContent className="truncate font-mono text-xs">{audit.data?.checkpoint?.chain_head_hash ?? "—"}</CardContent></Card>
      </div>
      <DataTable
        rows={visible.rows}
        columns={[
          { id: "timestamp", header: t("llmUsage.colTime"), cell: (row) => formatTime(row.timestamp), sortable: true },
          { id: "event", header: t("audit.colEvent"), accessor: (row) => row.event_type, sortable: true },
          { id: "actor", header: t("audit.colActor"), accessor: (row) => row.user_id ?? row.agent_id ?? "—", sortable: true },
          { id: "action", header: t("audit.colAction"), accessor: (row) => row.action },
          { id: "resource", header: t("audit.colResource"), accessor: (row) => row.resource },
          { id: "decision", header: t("audit.colDecision"), accessor: (row) => row.decision },
        ]}
        rowKey={(row) => row.event_id}
        loading={audit.isLoading}
        error={audit.error ? normalizeAdminError(audit.error) : undefined}
        onRetry={() => audit.refetch()}
        query={table.query}
        onQueryChange={table.onQueryChange}
        totalRows={visible.totalRows}
        searchable
        empty={{ title: t("audit.noEvents"), description: t("audit.noEventsDesc"), filtered: Boolean(table.query.search || from || to) }}
        ariaLabel={t("audit.timelineTitle")}
      />
    </div>
  );
}


/**
 * `useTableQuery`/`useUrlFilter` call `useSearchParams()`, which Next.js 15 requires
 * to sit under a Suspense boundary during prerendering.
 */
export default function AuditPage() {
  const { t } = useI18n();
  return (
    <Suspense fallback={<Skeleton variant="table" ariaLabel={t("common.loading")} />}>
      <AuditPageContent />
    </Suspense>
  );
}
