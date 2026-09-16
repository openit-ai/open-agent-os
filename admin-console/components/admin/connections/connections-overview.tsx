"use client";

import Link from "next/link";
import { useQueries } from "@tanstack/react-query";
import { ArrowRight, Cable } from "lucide-react";
import { Skeleton, StatusBadge } from "@/components/admin";
import { getConnectionDiscovery } from "@/lib/admin-api/client";
import { adminKeys } from "@/lib/admin-api/keys";
import type { ConnectionDiscoveryCandidate, ConnectionKind } from "@/lib/admin-api/types";
import { useI18n } from "@/lib/i18n";

const connections: Array<{ kind: ConnectionKind; href: string }> = [
  { kind: "mattermost", href: "/connections/mattermost" },
  { kind: "slack", href: "/connections/slack" },
  { kind: "notion", href: "/connections/notion" },
  { kind: "oauth", href: "/connections/oauth" },
  { kind: "smtp", href: "/connections/smtp" },
];

function bestCandidate(candidates: ConnectionDiscoveryCandidate[]) {
  return candidates.find((candidate) => candidate.applied && candidate.credential_state === "available")
    ?? candidates.find((candidate) => candidate.credential_state === "available")
    ?? candidates[0];
}

function candidateStatus(candidate: ConnectionDiscoveryCandidate | undefined): "healthy" | "warning" | "failed" {
  if (!candidate) return "failed";
  if (candidate.applied && candidate.credential_state === "available") return "healthy";
  if (candidate.credential_state === "authorization_required" || candidate.credential_state === "available") return "warning";
  return "failed";
}

export function ConnectionsOverview() {
  const { t } = useI18n();
  const queries = useQueries({ queries: connections.map(({ kind }) => ({ queryKey: adminKeys.connectionDiscovery(kind), queryFn: ({ signal }: { signal: AbortSignal }) => getConnectionDiscovery(kind, signal), retry: false })) });
  const rows = connections.map((connection, index) => {
    const candidate = bestCandidate(queries[index].data?.candidates ?? []);
    return { ...connection, candidate, status: queries[index].isError ? "failed" as const : candidateStatus(candidate), loading: queries[index].isPending };
  }).sort((left, right) => ({ failed: 0, warning: 1, healthy: 2 }[left.status] - { failed: 0, warning: 1, healthy: 2 }[right.status]));

  return (
    <div className="space-y-6">
      <header><h1 className="flex items-center gap-2 text-2xl font-semibold"><Cable aria-hidden="true" className="h-6 w-6" />{t("admin.connections.overview.title")}</h1><p className="mt-1 text-sm text-muted-foreground">{t("admin.connections.overview.description")}</p></header>
      <div className="grid gap-3">
        {rows.map((row) => row.loading ? <Skeleton key={row.kind} variant="card" ariaLabel={t("admin.loading.content")} /> : (
          <article key={row.kind} className="flex flex-col justify-between gap-4 rounded-lg border bg-card p-5 sm:flex-row sm:items-center">
            <div className="min-w-0"><div className="flex items-center gap-2"><h2 className="font-semibold">{t(`admin.connections.kind.${row.kind}.title`)}</h2><StatusBadge status={row.status} /></div><p className="mt-1 text-sm text-muted-foreground">{row.candidate ? row.candidate.display_target : t("admin.connections.discovery.noCandidates")}</p><p className="mt-2 text-xs text-muted-foreground">{row.candidate ? `${row.candidate.source} · ${row.candidate.confidence} · ${t(`admin.connections.credential.${row.candidate.credential_state}`)}` : t("admin.connections.overview.configureRequired")}</p></div>
            <Link href={row.href} className="inline-flex h-9 shrink-0 items-center justify-center gap-2 rounded-md border px-4 text-sm font-medium transition-colors duration-200 hover:bg-accent motion-reduce:transition-none">{t("admin.connections.overview.open")}<ArrowRight aria-hidden="true" className="h-4 w-4" /></Link>
          </article>
        ))}
      </div>
    </div>
  );
}
