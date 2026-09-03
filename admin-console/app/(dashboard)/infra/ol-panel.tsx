"use client";
import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { getOlConfig, updateOlConfig, testOlConnection, apiFetch, type OlConfig, type OlTestResult } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { BookOpen, Loader2 } from "lucide-react";

export function OlPanel() {
  const { t } = useI18n();
  const [cfg, setCfg] = useState<OlConfig | null>(null);
  const [url, setUrl] = useState("");
  const [key, setKey] = useState("");
  const [testRes, setTestRes] = useState<OlTestResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);

  const fetchCfg = useCallback(async () => {
    setError(null);
    try {
      const res = await getOlConfig();
      setCfg(res);
      setUrl(res.outline_url);
    } catch (e) {
      setError(e instanceof Error ? e.message : t("ol.loadFailed"));
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
      const res = await updateOlConfig({
        outline_url: url,
        ...(key.trim() ? { api_key: key.trim() } : {}),
      });
      setCfg(res);
      setKey("");
      setMsg(t("ol.saved"));
      // Best-effort: register into services table so it shows under 서비스현황.
      try {
        const u = new URL(url);
        if (u.hostname) {
          const listRes = await apiFetch<{ items: { service: string }[] } | { service: string }[]>("/v1/infra");
          const list = Array.isArray(listRes) ? listRes : (listRes.items ?? []);
          if (!list.some((it) => it.service === "outline")) {
            await apiFetch("/v1/infra", {
              method: "POST",
              body: JSON.stringify({
                service: "outline", host: u.hostname,
                port: u.port ? Number(u.port) : (u.protocol === "https:" ? 443 : 80),
                health_path: "/",
              }),
            });
          }
        }
      } catch { /* ignore registration errors */ }
    } catch (e) {
      setError(e instanceof Error ? e.message : t("ol.saveFailed"));
    } finally {
      setSaving(false);
    }
  };

  const test = async () => {
    setTesting(true);
    setError(null);
    try {
      const res = await testOlConnection(key.trim() ? { api_key: key.trim() } : undefined);
      setTestRes(res);
    } catch (e) {
      setError(e instanceof Error ? e.message : "test failed");
    } finally {
      setTesting(false);
    }
  };

  if (loading) return <div className="py-4 text-sm text-muted-foreground">{t("common.loading")}</div>;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="flex items-center gap-2 text-2xl font-semibold"><BookOpen className="h-6 w-6" /> {t("ol.title")}</h1>
        <p className="text-sm text-muted-foreground">{t("ol.subtitle")}</p>
      </div>
      {error && <Card className="border-red-500"><CardContent className="pt-4 text-sm text-red-600">{error}</CardContent></Card>}
      {msg && <Card className="border-green-500"><CardContent className="pt-4 text-sm text-green-700">{msg}</CardContent></Card>}

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            {t("ol.title")}
            {cfg && <Badge variant="secondary">source: {cfg.source}</Badge>}
            {cfg && (cfg.applied
              ? <Badge className="bg-green-600 text-white">{t("ol.appliedYes")}</Badge>
              : <Badge className="bg-amber-500 text-white">{t("ol.appliedNo")}</Badge>)}
          </CardTitle>
          {cfg?.note && <CardDescription>{cfg.note}</CardDescription>}
        </CardHeader>
        <CardContent className="space-y-3">
          <div>
            <Label>{t("ol.url")}</Label>
            <Input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://note.oaos.cloud" />
          </div>
          <div>
            <Label>{t("ol.key")}</Label>
            <Input type="password" value={key} onChange={(e) => setKey(e.target.value)} placeholder={cfg?.api_key_set ? "•••••••• (registered)" : ""} />
            <p className="mt-1 text-xs text-muted-foreground">{t("ol.keyHint")}</p>
          </div>
          <div className="text-sm text-muted-foreground">{t("ol.keySet")}: {String(cfg?.api_key_set)}</div>
          <div className="flex gap-2">
            <Button onClick={save} disabled={saving}>{saving && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}{t("ol.save")}</Button>
            <Button variant="outline" onClick={test} disabled={testing}>{testing && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}{t("ol.test")}</Button>
          </div>
          {testRes && (
            <div className="text-sm">
              {testRes.ok
                ? <span className="text-green-700">OK · {t("ol.collections")}: {testRes.collection_count} · {testRes.latency_ms} ms</span>
                : <span className="text-red-600">FAIL · {testRes.error ?? testRes.status_code}</span>}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
