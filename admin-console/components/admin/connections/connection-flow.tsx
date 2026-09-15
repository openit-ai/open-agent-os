"use client";

import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { ChevronDown, CircleDot, KeyRound, Link2, Search, ShieldCheck } from "lucide-react";
import { ErrorState, Skeleton, TestConnectionButton } from "@/components/admin";
import { Button } from "@/components/ui/button";
import { getConnectionDiscovery, normalizeAdminError } from "@/lib/admin-api/client";
import { adminKeys } from "@/lib/admin-api/keys";
import type { ConnectionDiscoveryCandidate, ConnectionKind } from "@/lib/admin-api/types";
import { useI18n } from "@/lib/i18n";

const confidenceOrder = { high: 0, medium: 1, low: 2 } as const;

function candidatePriority(candidate: ConnectionDiscoveryCandidate) {
  const credential = candidate.credential_state === "available" ? 0 : candidate.credential_state === "authorization_required" ? 1 : 2;
  return credential * 10 + confidenceOrder[candidate.confidence] + (candidate.applied ? -1 : 0);
}

export interface ConnectionFlowProps {
  kind: ConnectionKind;
  title: string;
  description: string;
  serviceLabel: string;
  children: React.ReactNode;
}

export function ConnectionFlow({ kind, title, description, serviceLabel, children }: ConnectionFlowProps) {
  const { t } = useI18n();
  const discovery = useQuery({
    queryKey: adminKeys.connectionDiscovery(kind),
    queryFn: ({ signal }) => getConnectionDiscovery(kind, signal),
    retry: false,
  });
  const candidates = React.useMemo(
    () => [...(discovery.data?.candidates ?? [])].sort((left, right) => candidatePriority(left) - candidatePriority(right)),
    [discovery.data],
  );
  const [selectedId, setSelectedId] = React.useState<string>();

  React.useEffect(() => {
    if (!selectedId && candidates[0]) setSelectedId(candidates[0].candidate_id);
  }, [candidates, selectedId]);

  const selected = candidates.find((candidate) => candidate.candidate_id === selectedId);

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-semibold">{title}</h1>
        <p className="mt-1 text-sm text-muted-foreground">{description}</p>
      </header>

      <ol className="grid gap-2 text-sm sm:grid-cols-4" aria-label={t("admin.connections.flow.label")}>
        {[
          [Search, t("admin.connections.flow.discover")],
          [CircleDot, t("admin.connections.flow.select")],
          [Link2, t("admin.connections.flow.connect")],
          [ShieldCheck, t("admin.connections.flow.test")],
        ].map(([Icon, label], index) => {
          const StepIcon = Icon as typeof Search;
          return <li key={String(label)} className="flex items-center gap-2 rounded-md border bg-card px-3 py-2"><span className="flex h-6 w-6 items-center justify-center rounded-full bg-muted text-xs font-semibold">{index + 1}</span><StepIcon aria-hidden="true" className="h-4 w-4" />{String(label)}</li>;
        })}
      </ol>

      <section aria-labelledby={`${kind}-candidates-title`} className="space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div><h2 id={`${kind}-candidates-title`} className="text-lg font-semibold">{t("admin.connections.discovery.title")}</h2><p className="text-sm text-muted-foreground">{t("admin.connections.discovery.description")}</p></div>
          <Button type="button" variant="outline" disabled={discovery.isFetching} onClick={() => void discovery.refetch()}>{t("admin.connections.discovery.refresh")}</Button>
        </div>
        {discovery.isPending ? <Skeleton variant="card" rows={2} ariaLabel={t("admin.connections.discovery.loading")} /> : null}
        {discovery.isError ? (() => {
          const error = normalizeAdminError(discovery.error);
          return <ErrorState title={t("admin.connections.discovery.failed")} description={t(error.message_key)} code={error.code} retry={() => void discovery.refetch()} />;
        })() : null}
        {!discovery.isPending && !discovery.isError && candidates.length === 0 ? <p className="rounded-md border bg-muted/20 p-4 text-sm text-muted-foreground">{t(discovery.data?.reasons[0] ?? "admin.connections.discovery.noCandidates")}</p> : null}
        <div className="grid gap-3 lg:grid-cols-2">
          {candidates.map((candidate, index) => (
            <label key={candidate.candidate_id} className={selectedId === candidate.candidate_id ? "cursor-pointer rounded-lg border-2 border-primary bg-primary/5 p-4" : "cursor-pointer rounded-lg border p-4 transition-colors duration-200 hover:bg-accent motion-reduce:transition-none"}>
              <div className="flex items-start gap-3">
                <input className="mt-1 h-4 w-4 accent-primary" type="radio" name={`${kind}-candidate`} checked={selectedId === candidate.candidate_id} onChange={() => setSelectedId(candidate.candidate_id)} />
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2"><span className="font-medium">{candidate.display_target}</span>{index === 0 ? <span className="rounded bg-status-ok-surface px-2 py-0.5 text-xs font-medium text-status-ok-text">{t("admin.connections.discovery.recommended")}</span> : null}</div>
                  <dl className="mt-3 grid gap-2 text-xs sm:grid-cols-2">
                    <div><dt className="text-muted-foreground">{t("admin.connections.discovery.source")}</dt><dd className="font-medium">{candidate.source}</dd></div>
                    <div><dt className="text-muted-foreground">{t("admin.connections.discovery.confidence")}</dt><dd className="font-medium">{candidate.confidence}</dd></div>
                    <div><dt className="text-muted-foreground">{t("admin.connections.discovery.credential")}</dt><dd className="flex items-center gap-1 font-medium"><KeyRound aria-hidden="true" className="h-3.5 w-3.5" />{t(`admin.connections.credential.${candidate.credential_state}`)}</dd></div>
                    <div><dt className="text-muted-foreground">{t("admin.connections.discovery.applyState")}</dt><dd className={candidate.applied ? "font-medium text-status-ok-text" : "font-medium text-status-warn-text"}>{candidate.applied ? t("admin.connections.apply.applied") : t("admin.connections.apply.savedNotApplied")}{candidate.requires_restart ? ` · ${t("admin.connections.apply.restartRequired")}` : ""}</dd></div>
                  </dl>
                </div>
              </div>
            </label>
          ))}
        </div>
      </section>

      <section className="rounded-lg border bg-card p-5" aria-labelledby={`${kind}-test-title`}>
        <h2 id={`${kind}-test-title`} className="text-lg font-semibold">{t("admin.connections.test.title")}</h2>
        <p className="mt-1 text-sm text-muted-foreground">{selected ? t("admin.connections.test.selectedTarget", { target: selected.display_target }) : t("admin.connections.test.selectFirst")}</p>
        <div className="mt-4"><TestConnectionButton connectionId={kind} candidateId={selected?.candidate_id} disabled={!selected} /></div>
      </section>

      <details className="group rounded-lg border bg-card">
        <summary className="flex cursor-pointer list-none items-center justify-between gap-3 p-5 font-semibold focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2">
          <span>{t("admin.connections.advanced.title")}</span>
          <ChevronDown aria-hidden="true" className="h-5 w-5 transition-transform duration-200 group-open:rotate-180 motion-reduce:transition-none" />
        </summary>
        <div className="border-t p-5"><p className="mb-4 text-sm text-muted-foreground">{t("admin.connections.advanced.description", { service: serviceLabel })}</p>{children}</div>
      </details>
    </div>
  );
}
