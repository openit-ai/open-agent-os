"use client";
import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { getToken, getQuotaLimits, updateQuotaLimits, getQuotaUsage, type QuotaLimits, type QuotaUsage } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { Gauge, Loader2 } from "lucide-react";

export default function QuotaPage() {
  const router = useRouter();
  const { t } = useI18n();
  const [tenant, setTenant] = useState("default");
  const [limits, setLimits] = useState<QuotaLimits | null>(null);
  const [usage, setUsage] = useState<QuotaUsage | null>(null);
  const [daily, setDaily] = useState("100");
  const [perMin, setPerMin] = useState("10");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);

  const fetchAll = useCallback(async (tid: string) => {
    setError(null);
    try {
      const [l, u] = await Promise.all([getQuotaLimits(tid), getQuotaUsage(tid)]);
      setLimits(l);
      setDaily(String(l.daily_limit));
      setPerMin(String(l.per_minute_limit));
      setUsage(u);
    } catch (e) {
      setError(e instanceof Error ? e.message : t("quota.loadFailed"));
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    if (!getToken()) { router.replace("/login"); return; }
    fetchAll(tenant);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const reload = () => { setLoading(true); fetchAll(tenant); };

  const save = async () => {
    setSaving(true);
    setError(null);
    setMsg(null);
    try {
      const res = await updateQuotaLimits({
        tenant_id: tenant.trim() || "default",
        daily_limit: Number(daily),
        per_minute_limit: Number(perMin),
      });
      setLimits(res);
      setMsg(t("quota.saved"));
      const u = await getQuotaUsage(tenant.trim() || "default");
      setUsage(u);
    } catch (e) {
      setError(e instanceof Error ? e.message : t("quota.saveFailed"));
    } finally {
      setSaving(false);
    }
  };

  if (loading) return <div className="p-6 text-sm text-muted-foreground">{t("common.loading")}</div>;

  const usageJson = usage?.usage ? JSON.stringify(usage.usage, null, 2) : null;

  return (
    <div className="space-y-6 p-6">
      <div>
        <h1 className="flex items-center gap-2 text-2xl font-semibold"><Gauge className="h-6 w-6" /> {t("quota.title")}</h1>
        <p className="text-sm text-muted-foreground">{t("quota.subtitle")}</p>
      </div>
      {error && <Card className="border-red-500"><CardContent className="pt-4 text-sm text-red-600">{error}</CardContent></Card>}
      {msg && <Card className="border-green-500"><CardContent className="pt-4 text-sm text-green-700">{msg}</CardContent></Card>}

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            {t("quota.tenant")}
            {limits && <Badge variant="secondary">source: {limits.source}</Badge>}
            {limits && (limits.overridden
              ? <Badge className="bg-amber-500 text-white">{t("quota.overridden")}</Badge>
              : <Badge variant="secondary">{t("quota.defaults")}: {limits.defaults.daily_limit}/{limits.defaults.per_minute_limit}</Badge>)}
          </CardTitle>
          {limits?.note && <CardDescription>{limits.note}</CardDescription>}
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex flex-wrap items-end gap-3">
            <div className="space-y-1">
              <Label htmlFor="quota-tenant">{t("quota.tenant")}</Label>
              <Input id="quota-tenant" value={tenant} onChange={(e) => setTenant(e.target.value)} className="w-48" />
            </div>
            <Button variant="outline" onClick={reload}>{t("common.refresh")}</Button>
          </div>
          <div className="flex flex-wrap items-end gap-3">
            <div className="space-y-1">
              <Label htmlFor="quota-daily">{t("quota.daily")}</Label>
              <Input id="quota-daily" inputMode="numeric" value={daily} onChange={(e) => setDaily(e.target.value)} className="w-36" />
            </div>
            <div className="space-y-1">
              <Label htmlFor="quota-permin">{t("quota.perMinute")}</Label>
              <Input id="quota-permin" inputMode="numeric" value={perMin} onChange={(e) => setPerMin(e.target.value)} className="w-36" />
            </div>
            <Button onClick={save} disabled={saving}>
              {saving && <Loader2 className="mr-1 h-4 w-4 animate-spin" />}{t("quota.save")}
            </Button>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>{t("quota.usage")}</CardTitle>
          {usage?.note && <CardDescription>{usage.note}</CardDescription>}
        </CardHeader>
        <CardContent>
          {usageJson
            ? <pre className="max-h-96 overflow-auto rounded bg-muted p-3 text-xs">{usageJson}</pre>
            : <p className="text-sm text-muted-foreground">{t("common.noData")}</p>}
        </CardContent>
      </Card>
    </div>
  );
}
