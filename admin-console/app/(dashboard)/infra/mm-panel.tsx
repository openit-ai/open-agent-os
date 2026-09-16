"use client";
import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { getMmConfig, updateMmConfig, getMmBridge, apiFetch, type MmConfig, type MmBridgeStatus } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { MessageSquare, Loader2 } from "lucide-react";
import { ConfigurationApplyState, Skeleton, TestConnectionButton } from "@/components/admin";

export function MmPanel() {
  const { t } = useI18n();
  const [cfg, setCfg] = useState<MmConfig | null>(null);
  const [url, setUrl] = useState("");
  const [botUsername, setBotUsername] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [token, setToken] = useState("");
  const [bridge, setBridge] = useState<MmBridgeStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);

  const fetchCfg = useCallback(async () => {
    setError(null);
    try {
      const res = await getMmConfig();
      setCfg(res);
      setUrl(res.mattermost_url);
      setBotUsername(res.bot_username);
      setDisplayName(res.default_display_name);
    } catch (e) {
      setError(e instanceof Error ? e.message : t("mm.loadFailed"));
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => { fetchCfg(); }, [fetchCfg]);

  const fetchBridge = useCallback(async () => {
    try {
      setBridge(await getMmBridge());
    } catch {
      setBridge(null);
    }
  }, []);

  useEffect(() => { fetchBridge(); }, [fetchBridge]);

  const save = async () => {
    setSaving(true);
    setError(null);
    setMsg(null);
    try {
      const res = await updateMmConfig({
        mattermost_url: url,
        bot_username: botUsername,
        default_display_name: displayName,
        ...(token.trim() ? { bot_token: token.trim() } : {}),
      });
      setCfg(res);
      setToken("");
      setMsg(t("mm.saved"));
      // Best-effort: register into services table so it shows under 서비스현황.
      try {
        const u = new URL(url);
        if (u.hostname) {
          const listRes = await apiFetch<{ items: { service: string }[] } | { service: string }[]>("/v1/infra");
          const list = Array.isArray(listRes) ? listRes : (listRes.items ?? []);
          if (!list.some((it) => it.service === "mattermost")) {
            await apiFetch("/v1/infra", {
              method: "POST",
              body: JSON.stringify({
                service: "mattermost", host: u.hostname,
                port: u.port ? Number(u.port) : (u.protocol === "https:" ? 443 : 80),
                health_path: "/api/v4/system/ping",
              }),
            });
          }
        }
      } catch { /* ignore registration errors */ }
    } catch (e) {
      setError(e instanceof Error ? e.message : t("mm.saveFailed"));
    } finally {
      setSaving(false);
    }
  };

  if (loading) return <Skeleton variant="form" rows={4} ariaLabel={t("admin.loading.content")} />;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="flex items-center gap-2 text-2xl font-semibold"><MessageSquare className="h-6 w-6" /> {t("mm.title")}</h1>
        <p className="text-sm text-muted-foreground">{t("mm.subtitle")}</p>
      </div>
      {error && <Card className="border-red-500"><CardContent className="pt-4 text-sm text-red-600">{error}</CardContent></Card>}
      {msg && <Card className="border-green-500"><CardContent className="pt-4 text-sm text-green-700">{msg}</CardContent></Card>}

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            {t("mm.title")}
            {cfg && <Badge variant="secondary">source: {cfg.source}</Badge>}
            {cfg && (cfg.applied
              ? <Badge className="bg-green-600 text-white">{t("mm.appliedYes")}</Badge>
              : <Badge className="bg-amber-500 text-white">{t("mm.appliedNo")}</Badge>)}
          </CardTitle>
          {cfg?.note && <CardDescription>{cfg.note}</CardDescription>}
        </CardHeader>
        <CardContent className="space-y-3">
          {cfg ? <ConfigurationApplyState value={cfg} service="Mattermost" /> : null}
          <div>
            <Label>{t("mm.url")}</Label>
            <Input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="http://127.0.0.1:8065" />
          </div>
          <div className="grid gap-3 md:grid-cols-2">
            <div>
              <Label>{t("mm.botUsername")}</Label>
              <Input value={botUsername} onChange={(e) => setBotUsername(e.target.value)} placeholder="agent" />
            </div>
            <div>
              <Label>{t("mm.displayName")}</Label>
              <Input value={displayName} onChange={(e) => setDisplayName(e.target.value)} placeholder="" />
            </div>
          </div>
          <div>
            <Label>{t("mm.token")}</Label>
            <Input type="password" value={token} onChange={(e) => setToken(e.target.value)} placeholder={cfg?.bot_token_set ? "•••••••• (registered)" : ""} />
            <p className="mt-1 text-xs text-muted-foreground">{t("mm.tokenHint")}</p>
          </div>
          <div className="flex gap-2">
            <Button onClick={save} disabled={saving}>{saving && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}{t("mm.save")}</Button>
            <TestConnectionButton connectionId="mattermost" configRevision={cfg?.config_revision} />
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            {t("mm.bridgeTitle")}
            {bridge && (bridge.active === "active"
              ? <Badge className="bg-green-600 text-white">active</Badge>
              : <Badge variant="secondary">{bridge.active}</Badge>)}
          </CardTitle>
          <CardDescription>{t("mm.bridgeDesc")}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="text-xs text-muted-foreground">
            installed: {String(bridge?.installed)} · configured: {String(bridge?.configured)}
          </div>
          <p className="text-sm text-muted-foreground">{t("admin.connections.mattermost.bridgeOperatorManaged")}</p>
        </CardContent>
      </Card>
    </div>
  );
}
