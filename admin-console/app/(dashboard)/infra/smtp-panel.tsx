"use client";
import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { getSmtpConfig, updateSmtpConfig, testSmtpConnection, type SmtpConfig, type SmtpTestResult } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { Mail, Loader2 } from "lucide-react";

export function SmtpPanel() {
  const { t } = useI18n();
  const [cfg, setCfg] = useState<SmtpConfig | null>(null);
  const [host, setHost] = useState("");
  const [port, setPort] = useState("587");
  const [user, setUser] = useState("");
  const [pass, setPass] = useState("");
  const [starttls, setStarttls] = useState(true);
  const [testRes, setTestRes] = useState<SmtpTestResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);

  const fetchCfg = useCallback(async () => {
    setError(null);
    try {
      const res = await getSmtpConfig();
      setCfg(res);
      setHost(res.smtp_host);
      setPort(String(res.smtp_port));
      setUser(res.smtp_user);
      setStarttls(res.use_starttls);
    } catch (e) {
      setError(e instanceof Error ? e.message : t("smtp.loadFailed"));
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
      const res = await updateSmtpConfig({
        smtp_host: host,
        smtp_port: Number(port),
        smtp_user: user,
        use_starttls: starttls,
        ...(pass.trim() ? { smtp_password: pass.trim() } : {}),
      });
      setCfg(res);
      setPass("");
      setMsg(t("smtp.saved"));
    } catch (e) {
      setError(e instanceof Error ? e.message : t("smtp.saveFailed"));
    } finally {
      setSaving(false);
    }
  };

  const test = async () => {
    setTesting(true);
    setError(null);
    try {
      const res = await testSmtpConnection(pass.trim() ? { smtp_password: pass.trim() } : undefined);
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
        <h1 className="flex items-center gap-2 text-2xl font-semibold"><Mail className="h-6 w-6" /> {t("smtp.title")}</h1>
        <p className="text-sm text-muted-foreground">{t("smtp.subtitle")}</p>
      </div>
      {error && <Card className="border-red-500"><CardContent className="pt-4 text-sm text-red-600">{error}</CardContent></Card>}
      {msg && <Card className="border-green-500"><CardContent className="pt-4 text-sm text-green-700">{msg}</CardContent></Card>}

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            {t("smtp.title")}
            {cfg && <Badge variant="secondary">source: {cfg.source}</Badge>}
            {cfg && (cfg.applied
              ? <Badge className="bg-green-600 text-white">{t("smtp.appliedYes")}</Badge>
              : <Badge className="bg-amber-500 text-white">{t("smtp.appliedNo")}</Badge>)}
          </CardTitle>
          {cfg?.note && <CardDescription>{cfg.note}</CardDescription>}
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="grid gap-3 md:grid-cols-2">
            <div>
              <Label>{t("smtp.host")}</Label>
              <Input value={host} onChange={(e) => setHost(e.target.value)} placeholder="smtp.example.com" />
            </div>
            <div>
              <Label>{t("smtp.port")}</Label>
              <Input value={port} onChange={(e) => setPort(e.target.value)} placeholder="587" inputMode="numeric" />
            </div>
          </div>
          <div>
            <Label>{t("smtp.user")}</Label>
            <Input value={user} onChange={(e) => setUser(e.target.value)} placeholder="user@example.com" />
          </div>
          <div>
            <Label>{t("smtp.pass")}</Label>
            <Input type="password" value={pass} onChange={(e) => setPass(e.target.value)} placeholder={cfg?.smtp_password_set ? "•••••••• (registered)" : ""} />
            <p className="mt-1 text-xs text-muted-foreground">{t("smtp.passHint")}</p>
          </div>
          <div className="flex items-center justify-between">
            <Label>STARTTLS</Label>
            <input type="checkbox" checked={starttls} onChange={(e) => setStarttls(e.target.checked)} aria-label="STARTTLS" />
          </div>
          <div className="text-sm text-muted-foreground">{t("smtp.passSet")}: {String(cfg?.smtp_password_set)}</div>
          <div className="flex gap-2">
            <Button onClick={save} disabled={saving}>{saving && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}{t("smtp.save")}</Button>
            <Button variant="outline" onClick={test} disabled={testing}>{testing && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}{t("smtp.test")}</Button>
          </div>
          {testRes && (
            <div className="text-sm">
              {testRes.ok
                ? <span className="text-green-700">OK · {testRes.target} · {testRes.latency_ms} ms · {t("smtp.noMail")}</span>
                : <span className="text-red-600">FAIL · {testRes.error}</span>}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
