"use client";
import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { getAcpConfig, updateAcpConfig, type AcpConfig } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { PlugZap, Loader2 } from "lucide-react";
import { ConfigurationApplyState, Skeleton, TestConnectionButton } from "@/components/admin";

export function AcpSection() {
  const { t } = useI18n();
  const [cfg, setCfg] = useState<AcpConfig | null>(null);
  const [baseUrl, setBaseUrl] = useState("");
  const [enabled, setEnabled] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);

  const fetchCfg = useCallback(async () => {
    setError(null);
    try {
      const res = await getAcpConfig();
      setCfg(res);
      setBaseUrl(res.hermes_base_url);
      setEnabled(res.acp_enabled);
    } catch (e) {
      setError(e instanceof Error ? e.message : t("acp.loadFailed"));
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => { fetchCfg(); }, [fetchCfg]);

  const save = async () => {
    setSaving(true);
    setError(null);
    setMsg(null);
    try {
      const res = await updateAcpConfig({ hermes_base_url: baseUrl, acp_enabled: enabled });
      setCfg(res);
      setMsg(t("acp.saved"));
    } catch (e) {
      setError(e instanceof Error ? e.message : t("acp.saveFailed"));
    } finally {
      setSaving(false);
    }
  };

  if (loading) return <Skeleton variant="form" rows={3} ariaLabel={t("admin.loading.content")} />;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="flex items-center gap-2 text-2xl font-semibold"><PlugZap className="h-6 w-6" /> {t("acp.title")}</h1>
        <p className="text-sm text-muted-foreground">{t("acp.subtitle")}</p>
      </div>
      {error && <Card className="border-red-500"><CardContent className="pt-4 text-sm text-red-600">{error}</CardContent></Card>}
      {msg && <Card className="border-green-500"><CardContent className="pt-4 text-sm text-green-700">{msg}</CardContent></Card>}

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            {t("acp.title")}
            {cfg && <Badge variant="secondary">source: {cfg.source}</Badge>}
            {cfg && (cfg.applied
              ? <Badge className="bg-green-600 text-white">{t("acp.appliedYes")}</Badge>
              : <Badge className="bg-amber-500 text-white">{t("acp.appliedNo")}</Badge>)}
          </CardTitle>
          {cfg?.note && <CardDescription>{cfg.note}</CardDescription>}
        </CardHeader>
        <CardContent className="space-y-3">
          {cfg ? <ConfigurationApplyState value={cfg} service="ACP / Hermes" /> : null}
          <div>
            <Label>{t("acp.baseUrl")}</Label>
            <Input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder="http://127.0.0.1:8001" />
          </div>
          <p className="text-xs text-muted-foreground">{t("acp.modelAuto")}</p>
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
            {t("acp.acpEnabled")}
          </label>
          <div className="text-sm text-muted-foreground">{t("acp.apiKeySet")}: {String(cfg?.api_key_set)}</div>
          <div className="flex gap-2">
            <Button onClick={save} disabled={saving}>{saving && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}{t("acp.save")}</Button>
            <TestConnectionButton connectionId="acp" configRevision={cfg?.config_revision} />
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
