"use client";
import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { getSetupStatus, postSetupChecks, postSetupComplete, getSetupEffective, type SetupStatus, type SetupChecks, type SetupEffective } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { CheckCircle2, XCircle, Loader2 } from "lucide-react";

function CheckBadge({ ok }: { ok?: boolean }) {
  if (ok === undefined) return <Badge variant="secondary">—</Badge>;
  return ok
    ? <Badge className="bg-green-600 text-white">OK</Badge>
    : <Badge className="bg-red-600 text-white">FAIL</Badge>;
}

export function SetupTab() {
  const { t } = useI18n();
  const [status, setStatus] = useState<SetupStatus | null>(null);
  const [effective, setEffective] = useState<SetupEffective | null>(null);
  const [checks, setChecks] = useState<SetupChecks | null>(null);
  const [loading, setLoading] = useState(true);
  const [checking, setChecking] = useState(false);
  const [completing, setCompleting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dbUrl, setDbUrl] = useState("");
  const [redisUrl, setRedisUrl] = useState("");
  const [hermesUrl, setHermesUrl] = useState("");

  const fetchStatus = useCallback(async () => {
    setError(null);
    try {
      const res = await getSetupStatus();
      setStatus(res);
    } catch (e) {
      setError(e instanceof Error ? e.message : "load failed");
    } finally {
      setLoading(false);
    }
    try {
      const eff = await getSetupEffective();
      setEffective(eff);
    } catch {
      // unauthenticated (before login) — manual input still works
    }
  }, []);

  useEffect(() => { fetchStatus(); }, [fetchStatus]);

  const runChecks = async () => {
    setChecking(true);
    setError(null);
    try {
      const res = await postSetupChecks({
        ...(dbUrl.trim() ? { db_url: dbUrl.trim() } : {}),
        ...(redisUrl.trim() ? { redis_url: redisUrl.trim() } : {}),
        ...(hermesUrl.trim() ? { hermes_url: hermesUrl.trim() } : {}),
      });
      setChecks(res);
    } catch (e) {
      setError(e instanceof Error ? e.message : "checks failed");
    } finally {
      setChecking(false);
    }
  };

  const complete = async () => {
    setCompleting(true);
    setError(null);
    try {
      await postSetupComplete();
      await fetchStatus();
    } catch (e) {
      setError(e instanceof Error ? e.message : "complete failed");
    } finally {
      setCompleting(false);
    }
  };

  if (loading) return <div className="py-4 text-sm text-muted-foreground">{t("common.loading")}</div>;

  return (
    <div className="space-y-6">
      {error && <Card className="border-red-500"><CardContent className="pt-4 text-sm text-red-600">{error}</CardContent></Card>}
      <Card>
        <CardHeader><CardTitle>{t("setup.step")} 1 — {t("setup.check")}</CardTitle></CardHeader>
        <CardContent className="space-y-3">
          <div className="flex items-center gap-2 text-sm">
            {status?.setup_completed
              ? <><CheckCircle2 className="h-4 w-4 text-green-600" /> {t("setup.completed")}</>
              : <><XCircle className="h-4 w-4 text-amber-600" /> {t("setup.notCompleted")}</>}
            <span className="text-muted-foreground">· has_admin: {String(status?.has_admin)}</span>
          </div>
          {effective && (
            <div className="rounded-md bg-muted p-3 text-xs">
              <div className="mb-1 font-medium">{t("setup.detected")}</div>
              <div>DB: {effective.db.configured ? `${effective.db.driver}://${effective.db.user}@${effective.db.host}${effective.db.port ? `:${effective.db.port}` : ""}/${effective.db.database}` : "—"}</div>
              <div>Redis: {effective.redis.configured ? `${effective.redis.host}:${effective.redis.port}/${effective.redis.db}` : "—"}</div>
              <div>ACP: {effective.hermes.base_url || "—"}{effective.hermes.model ? ` · ${effective.hermes.model}` : ""} · acp_enabled={String(effective.hermes.acp_enabled)}</div>
            </div>
          )}
          <div className="grid gap-3 md:grid-cols-3">
            <div>
              <Label>{t("setup.dbUrl")}</Label>
              <Input value={dbUrl} onChange={(e) => setDbUrl(e.target.value)} placeholder="postgresql+psycopg://…" />
            </div>
            <div>
              <Label>{t("setup.redisUrl")}</Label>
              <Input value={redisUrl} onChange={(e) => setRedisUrl(e.target.value)} placeholder="redis://127.0.0.1:6379/0" />
            </div>
            <div>
              <Label>{t("setup.hermesUrl")}</Label>
              <Input value={hermesUrl} onChange={(e) => setHermesUrl(e.target.value)} placeholder="http://127.0.0.1:8001" />
            </div>
          </div>
          <div className="flex items-center gap-2">
            <Button onClick={runChecks} disabled={checking}>
              {checking && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}{t("setup.runChecks")}
            </Button>
          </div>
          {checks && (
            <div className="grid gap-2 md:grid-cols-3">
              {(["db", "redis", "hermes"] as const).map((k) => (
                <Card key={k}>
                  <CardContent className="flex items-center justify-between pt-4 text-sm">
                    <span className="font-medium">{k}</span>
                    <CheckBadge ok={checks[k].ok} />
                  </CardContent>
                  <CardContent className="text-xs text-muted-foreground">
                    {checks[k].target && <div className="break-all font-mono">{checks[k].target}</div>}
                    {checks[k].latency_ms !== undefined && `${checks[k].latency_ms} ms`}
                    {checks[k].error && <div className="break-all text-red-600">{checks[k].error}</div>}
                    {(checks[k] as { status_code?: number }).status_code !== undefined && <div>HTTP {(checks[k] as { status_code?: number }).status_code}</div>}
                  </CardContent>
                </Card>
              ))}
            </div>
          )}
          <p className="text-xs text-muted-foreground">{t("setup.saveNote")}</p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader><CardTitle>{t("setup.step")} 2 — {t("setup.complete")}</CardTitle></CardHeader>
        <CardContent className="space-y-3">
          {effective && (
            <div className="rounded-md bg-muted p-3 text-xs">
              <div className="mb-1 font-medium">{t("setup.appliedValues")}</div>
              <div>DB: {effective.db.configured ? `${effective.db.driver}://${effective.db.user}@${effective.db.host}${effective.db.port ? `:${effective.db.port}` : ""}/${effective.db.database}` : "—"}</div>
              <div>Redis: {effective.redis.configured ? `${effective.redis.host}:${effective.redis.port}/${effective.redis.db}` : "—"}</div>
              <div>ACP: {effective.hermes.base_url || "—"}{effective.hermes.model ? ` · ${effective.hermes.model}` : ` · ${t("acp.modelAutoShort")}`} · acp_enabled={String(effective.hermes.acp_enabled)}</div>
              <div>{t("setup.completedState")}: {String(status?.setup_completed)}</div>
            </div>
          )}
          <Button onClick={complete} disabled={completing || status?.setup_completed}>
            {completing && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}{t("setup.complete")}
          </Button>
        </CardContent>
      </Card>
    </div>
  );
}

