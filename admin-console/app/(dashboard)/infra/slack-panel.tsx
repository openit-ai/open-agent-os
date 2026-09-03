"use client";
import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { getSlackConfig, updateSlackConfig, testSlackConnection, type SlackConfig, type SlackTestResult } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { Hash, Loader2 } from "lucide-react";

export function SlackPanel() {
  const { t } = useI18n();
  const [cfg, setCfg] = useState<SlackConfig | null>(null);
  const [webhook, setWebhook] = useState("");
  const [channel, setChannel] = useState("");
  const [testRes, setTestRes] = useState<SlackTestResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);

  const fetchCfg = useCallback(async () => {
    setError(null);
    try {
      const res = await getSlackConfig();
      setCfg(res);
      setChannel(res.channel);
    } catch (e) {
      setError(e instanceof Error ? e.message : t("slack.loadFailed"));
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
      const res = await updateSlackConfig({
        channel,
        ...(webhook.trim() ? { webhook_url: webhook.trim() } : {}),
      });
      setCfg(res);
      setWebhook("");
      setMsg(t("slack.saved"));
    } catch (e) {
      setError(e instanceof Error ? e.message : t("slack.saveFailed"));
    } finally {
      setSaving(false);
    }
  };

  const test = async () => {
    setTesting(true);
    setError(null);
    try {
      const res = await testSlackConnection(webhook.trim() ? { webhook_url: webhook.trim() } : undefined);
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
        <h1 className="flex items-center gap-2 text-2xl font-semibold"><Hash className="h-6 w-6" /> {t("slack.title")}</h1>
        <p className="text-sm text-muted-foreground">{t("slack.subtitle")}</p>
      </div>
      {error && <Card className="border-red-500"><CardContent className="pt-4 text-sm text-red-600">{error}</CardContent></Card>}
      {msg && <Card className="border-green-500"><CardContent className="pt-4 text-sm text-green-700">{msg}</CardContent></Card>}

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            {t("slack.title")}
            {cfg && <Badge variant="secondary">source: {cfg.source}</Badge>}
            {cfg && (cfg.applied
              ? <Badge className="bg-green-600 text-white">{t("slack.appliedYes")}</Badge>
              : <Badge className="bg-amber-500 text-white">{t("slack.appliedNo")}</Badge>)}
          </CardTitle>
          {cfg?.note && <CardDescription>{cfg.note}</CardDescription>}
        </CardHeader>
        <CardContent className="space-y-3">
          <div>
            <Label>{t("slack.webhook")}</Label>
            <Input type="password" value={webhook} onChange={(e) => setWebhook(e.target.value)} placeholder={cfg?.webhook_url_set ? "•••••••• (registered)" : "https://hooks.slack.com/services/..."} />
            <p className="mt-1 text-xs text-muted-foreground">{t("slack.webhookHint")}</p>
          </div>
          <div>
            <Label>{t("slack.channel")}</Label>
            <Input value={channel} onChange={(e) => setChannel(e.target.value)} placeholder="#alerts" />
          </div>
          <div className="text-sm text-muted-foreground">{t("slack.webhookSet")}: {String(cfg?.webhook_url_set)}</div>
          <div className="flex gap-2">
            <Button onClick={save} disabled={saving}>{saving && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}{t("slack.save")}</Button>
            <Button variant="outline" onClick={test} disabled={testing}>{testing && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}{t("slack.test")}</Button>
          </div>
          {testRes && (
            <div className="text-sm">
              {testRes.ok
                ? <span className="text-green-700">OK · {testRes.latency_ms} ms</span>
                : <span className="text-red-600">FAIL · {testRes.error ?? testRes.status_code}</span>}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
