"use client";

import { useEffect, useMemo, useState, Suspense } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { ClipboardCheck, RefreshCw } from "lucide-react";
import { useBackgroundQueryToast, useTableQuery, useToast, useUrlFilter, DataTable, FormField, Skeleton } from "@/components/admin";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { applyClientListQuery } from "@/lib/admin-api/list-query";
import { normalizeAdminError } from "@/lib/admin-api/client";
import { adminKeys } from "@/lib/admin-api/keys";
import { decideApproval, getPendingApprovals, getToken, type ApprovalDecisionType, type ApprovalRequestItem } from "@/lib/api";
import { useI18n } from "@/lib/i18n";

function formatTime(value?: string | null) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function ApprovalsPageContent() {
  const router = useRouter();
  const { t } = useI18n();
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const table = useTableQuery();
  const [risk, setRisk] = useUrlFilter("risk", "all");
  const [groupId, setGroupId] = useState("default-group");

  useEffect(() => { if (!getToken()) router.replace("/login"); }, [router]);

  const approvals = useQuery({
    queryKey: adminKeys.list("approvals", table.query),
    queryFn: getPendingApprovals,
  });
  const allRows = approvals.data?.pending ?? [];
  useBackgroundQueryToast(approvals.error, allRows.length > 0, t("admin.lists.backgroundError"));

  const decision = useMutation({
    mutationFn: (input: { id: string; decision: ApprovalDecisionType }) => decideApproval({
      approval_id: input.id,
      decision: input.decision,
      group_id: input.decision === "APPROVED_GROUP_ALWAYS" ? groupId.trim() : undefined,
    }),
    onSuccess: async (_data, input) => {
      toast({ title: t("approvals.done", { decision: input.decision }), variant: "success" });
      await queryClient.invalidateQueries({ queryKey: adminKeys.lists("approvals") });
    },
    onError: (error) => toast({ title: t("approvals.actionFailed"), description: error instanceof Error ? error.message : undefined, variant: "error" }),
  });

  const filteredByRisk = useMemo(() => risk === "all" ? allRows : allRows.filter((row) => row.risk?.toUpperCase() === risk), [allRows, risk]);
  // The pending-approvals API has no search/sort/page parameters; keep its contract and apply the shared client adapter.
  const visible = useMemo(() => applyClientListQuery(
    filteredByRisk,
    table.query,
    [(row) => row.approval_id, (row) => row.user_id, (row) => row.agent_id, (row) => row.action, (row) => row.resource],
    { requested: (row) => row.created_at ?? "", expires: (row) => row.expires_at, risk: (row) => row.risk, requester: (row) => row.user_id },
  ), [filteredByRisk, table.query]);

  function decide(row: ApprovalRequestItem, type: ApprovalDecisionType) {
    if (type === "APPROVED_GROUP_ALWAYS" && !groupId.trim()) {
      toast({ title: t("approvals.needGroupId"), variant: "error" });
      return;
    }
    decision.mutate({ id: row.approval_id, decision: type });
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div><h1 className="flex items-center gap-2 text-2xl font-semibold"><ClipboardCheck aria-hidden="true" className="h-6 w-6" />{t("approvals.title")}</h1><p className="text-sm text-muted-foreground">{t("approvals.subtitle")}</p></div>
        <Button variant="outline" size="sm" disabled={approvals.isFetching} onClick={() => approvals.refetch()}><RefreshCw aria-hidden="true" className="h-4 w-4" />{t("common.refresh")}</Button>
      </div>
      <div className="grid gap-3 sm:grid-cols-2">
        <FormField id="approval-risk" label={t("admin.lists.riskFilter")}>
          <select value={risk} onChange={(event) => setRisk(event.target.value)} className="flex h-9 w-full rounded-md border bg-background px-3 text-sm">
            <option value="all">{t("llmUsage.filterAll")}</option><option value="HIGH">HIGH</option><option value="MEDIUM">MEDIUM</option><option value="LOW">LOW</option>
          </select>
        </FormField>
        <FormField id="approval-group" label={t("approvals.groupId")}><Input value={groupId} onChange={(event) => setGroupId(event.target.value)} /></FormField>
      </div>
      <DataTable
        rows={visible.rows}
        columns={[
          { id: "requester", header: t("approvals.colRequester"), accessor: (row) => row.user_id, sortable: true },
          { id: "agent", header: t("approvals.colAgent"), accessor: (row) => row.agent_id },
          { id: "action", header: t("approvals.colAction"), accessor: (row) => row.action },
          { id: "resource", header: t("approvals.colResource"), accessor: (row) => row.resource },
          { id: "risk", header: t("approvals.colRisk"), accessor: (row) => row.risk, sortable: true },
          { id: "requested", header: t("approvals.requestedAt"), cell: (row) => formatTime(row.created_at), sortable: true },
          { id: "expires", header: t("approvals.expires"), cell: (row) => formatTime(row.expires_at), sortable: true },
        ]}
        rowKey={(row) => row.approval_id}
        loading={approvals.isLoading}
        error={approvals.error ? normalizeAdminError(approvals.error) : undefined}
        onRetry={() => approvals.refetch()}
        query={table.query}
        onQueryChange={table.onQueryChange}
        totalRows={visible.totalRows}
        searchable
        rowActions={(row) => [
          { id: "approve-once", label: t("approvals.decideOnce"), disabled: decision.isPending, onSelect: () => decide(row, "APPROVED_ONCE") },
          { id: "approve-user", label: t("approvals.decideUserAlways"), disabled: decision.isPending, onSelect: () => decide(row, "APPROVED_USER_ALWAYS") },
          { id: "approve-group", label: t("approvals.decideGroupAlways"), disabled: decision.isPending, onSelect: () => decide(row, "APPROVED_GROUP_ALWAYS") },
          { id: "deny", label: t("approvals.decideDeny"), tone: "danger", disabled: decision.isPending, onSelect: () => decide(row, "DENIED") },
        ]}
        empty={{ title: t("approvals.emptyTitle"), description: t("approvals.emptyDesc"), filtered: Boolean(table.query.search || risk !== "all") }}
        ariaLabel={t("approvals.ariaList")}
      />
    </div>
  );
}


/**
 * `useTableQuery`/`useUrlFilter` call `useSearchParams()`, which Next.js 15 requires
 * to sit under a Suspense boundary during prerendering.
 */
export default function ApprovalsPage() {
  const { t } = useI18n();
  return (
    <Suspense fallback={<Skeleton variant="table" ariaLabel={t("common.loading")} />}>
      <ApprovalsPageContent />
    </Suspense>
  );
}
