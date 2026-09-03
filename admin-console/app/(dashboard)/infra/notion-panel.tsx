"use client";
import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { getNotionConfig, updateNotionConfig, testNotionConnection, type NotionConfig, type NotionTestResult } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { StickyNote, Loader2 } from "lucide-react";

export function NotionPanel() {
  const { t } = useI18n();
  const [cfg, setCfg] = useState<NotionConfig | null>(null);
  const [url, setUrl] = useState("");
  const [key, setKey] = useState("");
  const [testRes, setTestRes] = useState<NotionTestResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);

  const fetchCfg = useCallback(async () => {
    setError(null);
    try {
      const res = await getNotionConfig();
      setCfg(res);
      setUrl(res.notion_api_url);
    } catch (e) {
      setError(e instanceof Error ? e.message : t("notion.loadFailed"));
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
      const res = await updateNotionConfig({
        notion_api_url: url,
        ...(key.trim() ? { api_key: key.trim() } : {}),
      });
      setCfg(res);
      setKey("");
      setMsg(t("notion.saved"));
    } catch (e) {
      setError(e instanceof Error ? e.message : t("notion.saveFailed"));
    } finally {
      setSaving(false);
    }
  };

  const test = async () => {
    setTesting(true);
    setError(null);
    try {
      const res = await testNotionConnection(key.trim() ? { api_key: key.trim() } : undefined);
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
        <h1 className="flex items-center gap-2 text-2xl font-semibold"><StickyNote className="h-6 w-6" /> {t("notion.title")}</h1>
        <p className="text-sm text-muted-foreground">{t("notion.subtitle")}</p>
      </div>
      {error && <Card className="border-red-500"><CardContent className="pt-4 text-sm text-red-600">{error}</CardContent></Card>}
      {msg && <Card className="border-green-500"><CardContent className="pt-4 text-sm text-green-700">{msg}</CardContent></Card>}

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            {t("notion.title")}
            {cfg && <Badge variant="secondary">source: {cfg.source}</Badge>}
            {cfg && (cfg.applied
              ? <Badge className="bg-green-600 text-white">{t("notion.appliedYes")}</Badge>
              : <Badge className="bg-amber-500 text-white">{t("notion.appliedNo")}</Badge>)}
          </CardTitle>
          {cfg?.note && <CardDescription>{cfg.note}</CardDescription>}
        </CardHeader>
        <CardContent className="space-y-3">
          <div>
            <Label>{t("notion.url")}</Label>
            <Input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://api.notion.com" />
          </div>
          <div>
            <Label>{t("notion.key")}</Label>
            <Input type="password" value={key} onChange={(e) => setKey(e.target.value)} placeholder={cfg?.api_key_set ? "•••••••• (registered)" : ""} />
            <p className="mt-1 text-xs text-muted-foreground">{t("notion.keyHint")}</p>
          </div>
          <div className="text-sm text-muted-foreground">{t("notion.keySet")}: {String(cfg?.api_key_set)}</div>
          <div className="flex gap-2">
            <Button onClick={save} disabled={saving}>{saving && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}{t("notion.save")}</Button>
            <Button variant="outline" onClick={test} disabled={testing}>{testing && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}{t("notion.test")}</Button>
          </div>
          {testRes && (
            <div className="text-sm">
              {testRes.ok
                ? <span className="text-green-700">OK · users: {testRes.user_count} · {testRes.latency_ms} ms</span>
                : <span className="text-red-600">FAIL · {testRes.error ?? testRes.status_code}</span>}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
