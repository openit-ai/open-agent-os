"use client";
import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { getAcpConfig, updateAcpConfig, testAcpConnection, type AcpConfig, type AcpTestResult } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { PlugZap, Loader2 } from "lucide-react";

export function AcpSection() {
  const { t } = useI18n();
  const [cfg, setCfg] = useState<AcpConfig | null>(null);
  const [baseUrl, setBaseUrl] = useState("");
  const [model, setModel] = useState("");
  const [enabled, setEnabled] = useState(false);
  const [testRes, setTestRes] = useState<AcpTestResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);

  const fetchCfg = useCallback(async () => {
    setError(null);
    try {
      const res = await getAcpConfig();
      setCfg(res);
      setBaseUrl(res.hermes_base_url);
      setModel(res.hermes_model);
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
      const res = await updateAcpConfig({ hermes_base_url: baseUrl, hermes_model: model, acp_enabled: enabled });
      setCfg(res);
      setMsg(t("acp.saved"));
    } catch (e) {
      setError(e instanceof Error ? e.message : t("acp.saveFailed"));
    } finally {
      setSaving(false);
    }
  };

  const test = async () => {
    setTesting(true);
    setError(null);
    try {
      const res = await testAcpConnection({ hermes_base_url: baseUrl || undefined });
      setTestRes(res);
    } catch (e) {
      setError(e instanceof Error ? e.message : "test failed");
    } finally {
      setTesting(false);
    }
  };

  if (loading) return <div className="p-6 text-sm text-muted-foreground">{t("common.loading")}</div>;

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
          <div>
            <Label>{t("acp.baseUrl")}</Label>
            <Input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder="http://127.0.0.1:8001" />
          </div>
          <div>
            <Label>{t("acp.model")}</Label>
            <Input value={model} onChange={(e) => setModel(e.target.value)} placeholder="qwen2.5" />
          </div>
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
            {t("acp.acpEnabled")}
          </label>
          <div className="text-sm text-muted-foreground">{t("acp.apiKeySet")}: {String(cfg?.api_key_set)}</div>
          <div className="flex gap-2">
            <Button onClick={save} disabled={saving}>{saving && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}{t("acp.save")}</Button>
            <Button variant="outline" onClick={test} disabled={testing}>{testing && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}{t("acp.test")}</Button>
          </div>
          {testRes && (
            <div className="text-sm">
              {testRes.ok
                ? <span className="text-green-700">OK · {testRes.target}{testRes.path} · HTTP {testRes.status_code} · {testRes.latency_ms} ms</span>
                : <span className="text-red-600">FAIL · {testRes.target} · {testRes.error}</span>}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
