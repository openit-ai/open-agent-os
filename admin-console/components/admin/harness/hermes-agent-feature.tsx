"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Bot, ExternalLink, Route } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { ErrorState, Skeleton, StatusBadge } from "@/components/admin";
import { getAcpConfig, getRuntimeMode, getToken, type AcpConfig, type RuntimeModeResponse } from "@/lib/api";
import { useI18n } from "@/lib/i18n";

export function HermesAgentFeature() {
  const router = useRouter();
  const { t } = useI18n();
  const [mode, setMode] = useState<RuntimeModeResponse | null>(null);
  const [acp, setAcp] = useState<AcpConfig | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [runtimeMode, acpConfig] = await Promise.all([getRuntimeMode(), getAcpConfig()]);
      setMode(runtimeMode);
      setAcp(acpConfig);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : t("admin.harness.hermesAgent.loadFailed"));
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    if (!getToken()) {
      router.replace("/login");
      return;
    }
    void load();
  }, [load, router]);

  if (loading && !mode && !acp) return <Skeleton variant="form" rows={4} ariaLabel={t("admin.loading.content")} />;
  if (error && !mode && !acp) return <ErrorState title={t("admin.harness.hermesAgent.loadFailed")} description={error} retry={() => void load()} />;

  const isHermes = mode?.mode === "hermes";
  const healthy = Boolean(isHermes && acp?.acp_enabled && acp.applied !== false);
  const status = healthy ? "healthy" : isHermes ? "warning" : "unknown";
  const statusLabel = healthy
    ? t("admin.harness.hermesAgent.active")
    : isHermes
      ? t("admin.harness.hermesAgent.attention")
      : t("admin.harness.hermesAgent.standby");
  const defaultModel = acp?.hermes_model || t("admin.harness.hermesAgent.agentDefaultModel");

  return (
    <div className="space-y-6">
      <header>
        <h1 className="flex items-center gap-2 text-2xl font-semibold"><Bot aria-hidden="true" className="h-6 w-6" />{t("admin.harness.hermesAgent.title")}</h1>
        <p className="mt-1 text-sm text-muted-foreground">{t("admin.harness.hermesAgent.description")}</p>
      </header>
      {error ? <p className="text-sm text-status-danger-text" role="alert">{error}</p> : null}

      <div className="grid gap-4 md:grid-cols-3">
        <Card>
          <CardHeader className="pb-2"><CardDescription>{t("admin.harness.hermesAgent.executionPath")}</CardDescription></CardHeader>
          <CardContent className="flex items-center gap-2 text-lg font-semibold"><Route aria-hidden="true" className="h-5 w-5" />{isHermes ? t("admin.harness.hermesAgent.hermesPath") : t("admin.harness.hermesAgent.externalPath")}</CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2"><CardDescription>{t("admin.harness.hermesAgent.status")}</CardDescription></CardHeader>
          <CardContent><StatusBadge status={status} label={statusLabel} size="md" /></CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2"><CardDescription>{t("admin.harness.hermesAgent.defaultModel")}</CardDescription></CardHeader>
          <CardContent className="break-words font-mono text-sm">{defaultModel}</CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">{t("admin.harness.hermesAgent.endpoint")}</CardTitle>
          <CardDescription>{t("admin.harness.hermesAgent.source")}: {acp?.source ?? "—"}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <p className="break-all font-mono text-sm">{acp?.hermes_base_url ?? "—"}</p>
          <p className="rounded-md border bg-muted/40 p-3 text-sm text-muted-foreground">{t("admin.harness.hermesAgent.acpBoundary")}</p>
          <div className="flex flex-wrap gap-2">
            <Button asChild variant="outline"><Link href="/control/acp">{t("admin.harness.hermesAgent.openAcp")}<ExternalLink aria-hidden="true" className="h-4 w-4" /></Link></Button>
            <Button asChild variant="outline"><Link href="/control/runtime">{t("admin.harness.hermesAgent.openRuntime")}<ExternalLink aria-hidden="true" className="h-4 w-4" /></Link></Button>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
