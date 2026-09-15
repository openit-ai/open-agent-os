"use client";

import Link from "next/link";
import { RefreshCw, Settings2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { ApplyStateBanner } from "@/components/admin/apply-state-banner";
import { StatusBadge } from "@/components/admin/status-badge";
import { useI18n } from "@/lib/i18n";
import type { AdminReadiness, ReadinessConnection } from "@/lib/admin-api/types";

function sortIssues(left: ReadinessConnection, right: ReadinessConnection) {
  const severity = { failed: 0, warning: 1, healthy: 2 };
  return severity[left.status] - severity[right.status];
}

function formatCheckedAt(value: string, lang: string, fallback: string) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? fallback : new Intl.DateTimeFormat(lang, { dateStyle: "medium", timeStyle: "short" }).format(date);
}

export interface RequiredConnectionsSummaryProps {
  readiness: AdminReadiness;
  checking?: boolean;
  onRecheck(): void;
}

export function RequiredConnectionsSummary({ readiness, checking = false, onRecheck }: RequiredConnectionsSummaryProps) {
  const { t, lang } = useI18n();
  const required = readiness.connections.filter((connection) => connection.required);
  const healthy = required.filter((connection) => connection.status === "healthy");
  const issues = required.filter((connection) => connection.status !== "healthy").sort(sortIssues).slice(0, 3);
  const unapplied = required.filter((connection) => connection.configured && !connection.applied);
  const allHealthy = required.length > 0 && issues.length === 0;

  return (
    <section
      aria-labelledby="readiness-summary-title"
      className={allHealthy ? "rounded-lg border border-status-ok bg-status-ok-surface p-4" : "rounded-lg border bg-card p-5 md:p-6"}
    >
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <h2 id="readiness-summary-title" className="text-lg font-semibold">{t("admin.readiness.summary.title")}</h2>
            <StatusBadge status={allHealthy ? "healthy" : issues.some((issue) => issue.status === "failed") ? "failed" : "warning"} />
          </div>
          <p className="mt-1 font-medium">
            {t("admin.readiness.summary.requiredCount", { total: required.length, healthy: healthy.length, attention: required.length - healthy.length })}
          </p>
          <p className="mt-1 text-sm text-muted-foreground">
            {t("admin.readiness.summary.lastChecked", { time: formatCheckedAt(readiness.checked_at, lang, t("admin.readiness.summary.unknownTime")) })}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Link href="/setup" className="inline-flex h-9 items-center gap-2 rounded-md border bg-background px-4 text-sm font-medium transition-colors duration-200 hover:bg-accent motion-reduce:transition-none">
            <Settings2 aria-hidden="true" className="h-4 w-4" />
            {allHealthy ? t("admin.readiness.summary.openDetails") : t("admin.readiness.summary.resolve")}
          </Link>
          <Button type="button" variant="outline" disabled={checking} aria-busy={checking} onClick={onRecheck}>
            <RefreshCw aria-hidden="true" className={checking ? "h-4 w-4 animate-spin motion-reduce:animate-none" : "h-4 w-4"} />
            {checking ? t("admin.readiness.summary.checking") : t("admin.readiness.summary.recheckAll")}
          </Button>
        </div>
      </div>

      {!allHealthy && issues.length > 0 ? (
        <ul className="mt-5 grid gap-3 lg:grid-cols-3">
          {issues.map((issue) => {
            const translated = t(issue.message_key);
            return (
              <li key={issue.id} className="rounded-md border bg-background p-3">
                <StatusBadge status={issue.status} />
                <p className="mt-2 text-sm font-medium">{t(issue.title_key)}</p>
                <p className="mt-1 text-sm text-muted-foreground">{translated === issue.message_key ? issue.code : translated}</p>
                {issue.next_action ? <Link href={issue.next_action.href} className="mt-2 inline-flex text-sm font-medium text-primary underline">{t(issue.next_action.label_key)}</Link> : null}
              </li>
            );
          })}
        </ul>
      ) : null}

      {unapplied.length > 0 ? (
        <div className="mt-4 space-y-3">
          {unapplied.map((connection) => (
            <ApplyStateBanner
              key={connection.id}
              applied={connection.applied}
              affectedServices={connection.affected_services ?? [t(connection.title_key)]}
              operationHref={connection.operation_href}
            />
          ))}
        </div>
      ) : null}
    </section>
  );
}
