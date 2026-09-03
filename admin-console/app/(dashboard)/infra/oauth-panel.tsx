"use client";
import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { getOAuthConfig, updateOAuthConfig, testOAuthConnection, type OAuthConfig, type OAuthTestResult } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { KeyRound, Loader2 } from "lucide-react";

export function OAuthPanel() {
  const { t } = useI18n();
  const [cfg, setCfg] = useState<OAuthConfig | null>(null);
  const [googleEnabled, setGoogleEnabled] = useState(false);
  const [msEnabled, setMsEnabled] = useState(false);
  const [testRes, setTestRes] = useState<OAuthTestResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);

  const fetchCfg = useCallback(async () => {
    setError(null);
    try {
      const res = await getOAuthConfig();
      setCfg(res);
      setGoogleEnabled(res.google_enabled);
      setMsEnabled(res.microsoft_enabled);
    } catch (e) {
      setError(e instanceof Error ? e.message : t("oauth.loadFailed"));
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
      const res = await updateOAuthConfig({ google_enabled: googleEnabled, microsoft_enabled: msEnabled });
      setCfg(res);
      setMsg(t("oauth.saved"));
    } catch (e) {
      setError(e instanceof Error ? e.message : t("oauth.saveFailed"));
    } finally {
      setSaving(false);
    }
  };

  const test = async () => {
    setTesting(true);
    setError(null);
    try {
      const res = await testOAuthConnection();
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
        <h1 className="flex items-center gap-2 text-2xl font-semibold"><KeyRound className="h-6 w-6" /> {t("oauth.title")}</h1>
        <p className="text-sm text-muted-foreground">{t("oauth.subtitle")}</p>
      </div>
      {error && <Card className="border-red-500"><CardContent className="pt-4 text-sm text-red-600">{error}</CardContent></Card>}
      {msg && <Card className="border-green-500"><CardContent className="pt-4 text-sm text-green-700">{msg}</CardContent></Card>}

      <Card className="border-amber-500">
        <CardContent className="pt-4 text-sm text-amber-700">{t("oauth.envNotice")}</CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            {t("oauth.title")}
            {cfg && <Badge variant="secondary">source: {cfg.source}</Badge>}
          </CardTitle>
          {cfg?.note && <CardDescription>{cfg.note}</CardDescription>}
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <Label>Google</Label>
              <input type="checkbox" checked={googleEnabled} onChange={(e) => setGoogleEnabled(e.target.checked)} aria-label="Google enabled" />
            </div>
            <div className="text-xs text-muted-foreground">
              client_id: {String(cfg?.google_client_id_set)} · secret: {String(cfg?.google_client_secret_set)}
            </div>
            <div className="text-xs text-muted-foreground break-all">redirect: {cfg?.google_redirect_uri}</div>
          </div>
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <Label>Microsoft</Label>
              <input type="checkbox" checked={msEnabled} onChange={(e) => setMsEnabled(e.target.checked)} aria-label="Microsoft enabled" />
            </div>
            <div className="text-xs text-muted-foreground">
              client_id: {String(cfg?.microsoft_client_id_set)} · secret: {String(cfg?.microsoft_client_secret_set)}
            </div>
            <div className="text-xs text-muted-foreground break-all">redirect: {cfg?.microsoft_redirect_uri}</div>
          </div>
          <div className="flex gap-2">
            <Button onClick={save} disabled={saving}>{saving && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}{t("oauth.save")}</Button>
            <Button variant="outline" onClick={test} disabled={testing}>{testing && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}{t("oauth.test")}</Button>
          </div>
          {testRes && (
            <div className="space-y-1 text-sm">
              {(["google", "microsoft"] as const).map((p) => {
                const r = testRes.providers?.[p];
                if (!r) return null;
                return (
                  <div key={p}>
                    {r.ok
                      ? <span className="text-green-700">{p}: OK · {r.latency_ms} ms</span>
                      : <span className="text-red-600">{p}: FAIL · {r.error ?? r.status_code}</span>}
                  </div>
                );
              })}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
