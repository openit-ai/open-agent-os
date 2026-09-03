"use client";
import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { getMmConfig, updateMmConfig, testMmConnection, getMmBridge, mmBridgeAction, apiFetch, type MmConfig, type MmTestResult, type MmBridgeStatus } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { MessageSquare, Loader2 } from "lucide-react";

export function MmPanel() {
  const { t } = useI18n();
  const [cfg, setCfg] = useState<MmConfig | null>(null);
  const [url, setUrl] = useState("");
  const [botUsername, setBotUsername] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [token, setToken] = useState("");
  const [testRes, setTestRes] = useState<MmTestResult | null>(null);
  const [bridge, setBridge] = useState<MmBridgeStatus | null>(null);
  const [bridging, setBridging] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
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

  const bridgeAct = async (action: "start" | "stop" | "restart") => {
    setBridging(action);
    setError(null);
    try {
      const res = await mmBridgeAction(action);
      setBridge(res);
      setMsg(`${action}: ${res.active}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : `${action} failed`);
    } finally {
      setBridging(null);
    }
  };

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

  const test = async () => {
    setTesting(true);
    setError(null);
    try {
      const res = await testMmConnection(token.trim() ? { bot_token: token.trim() } : undefined);
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
            <Button variant="outline" onClick={test} disabled={testing}>{testing && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}{t("mm.test")}</Button>
          </div>
          {testRes && (
            <div className="text-sm">
              {testRes.ok
                ? <span className="text-green-700">OK · @{testRes.bot_username} · {testRes.bot_user_id} · {testRes.latency_ms} ms</span>
                : <span className="text-red-600">FAIL · {testRes.error ?? testRes.status_code}</span>}
            </div>
          )}
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
          <div className="flex gap-2">
            <Button onClick={() => bridgeAct("start")} disabled={bridging !== null}>
              {bridging === "start" && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}{t("mm.bridge_start")}
            </Button>
            <Button variant="outline" onClick={() => bridgeAct("restart")} disabled={bridging !== null}>
              {bridging === "restart" && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}{t("mm.bridge_restart")}
            </Button>
            <Button variant="destructive" onClick={() => bridgeAct("stop")} disabled={bridging !== null}>
              {bridging === "stop" && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}{t("mm.bridge_stop")}
            </Button>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
