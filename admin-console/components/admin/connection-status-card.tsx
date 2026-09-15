"use client";

import * as React from "react";
import { Clock3, Gauge, KeyRound, Layers3 } from "lucide-react";
import { StatusBadge } from "@/components/admin/status-badge";
import { useI18n } from "@/lib/i18n";
import type { ReadinessConnection } from "@/lib/admin-api/types";

function localizedTime(value: string | undefined, lang: string, fallback: string) {
  if (!value) return fallback;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? fallback : new Intl.DateTimeFormat(lang, { dateStyle: "medium", timeStyle: "short" }).format(date);
}

export interface ConnectionStatusCardProps {
  connection: ReadinessConnection;
}

export function ConnectionStatusCard({ connection }: ConnectionStatusCardProps) {
  const { t, lang } = useI18n();
  const title = t(connection.title_key);
  const description = t(connection.description_key);
  const message = t(connection.message_key);

  return (
    <article className="rounded-lg border bg-card p-5" aria-labelledby={`connection-${connection.id}`}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 id={`connection-${connection.id}`} className="font-semibold">{title}</h3>
          <p className="mt-1 text-sm text-muted-foreground">{description}</p>
        </div>
        <StatusBadge status={connection.status} size="md" />
      </div>

      <p className={connection.status === "healthy" ? "mt-4 text-sm text-status-ok-text" : connection.status === "warning" ? "mt-4 text-sm text-status-warn-text" : "mt-4 text-sm text-status-danger-text"}>
        {message === connection.message_key ? connection.code : message}
      </p>

      <dl className="mt-4 grid gap-3 text-sm sm:grid-cols-2">
        <div className="flex gap-2">
          <KeyRound aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
          <div><dt className="text-muted-foreground">{t("admin.connections.card.secret")}</dt><dd className="font-medium">{connection.secret_configured ? t("admin.connections.card.configured") : t("admin.connections.card.notConfigured")}</dd></div>
        </div>
        <div className="flex gap-2">
          <Layers3 aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
          <div><dt className="text-muted-foreground">{t("admin.connections.card.source")}</dt><dd className="font-medium">{connection.source}</dd></div>
        </div>
        <div className="flex gap-2">
          <Clock3 aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
          <div><dt className="text-muted-foreground">{t("admin.connections.card.lastChecked")}</dt><dd className="font-medium">{localizedTime(connection.checked_at, lang, t("admin.connections.card.neverChecked"))}</dd></div>
        </div>
        <div className="flex gap-2">
          <Gauge aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
          <div><dt className="text-muted-foreground">{t("admin.connections.card.latency")}</dt><dd className="font-medium">{connection.latency_ms == null ? t("admin.connections.card.notAvailable") : t("admin.connections.card.latencyValue", { latency: connection.latency_ms })}</dd></div>
        </div>
      </dl>
    </article>
  );
}
