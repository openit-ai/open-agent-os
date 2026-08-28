"use client";
import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { getToken, getLicenseStatus, verifyLicense, type LicenseStatusResponse } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { BadgeCheck, RefreshCw } from "lucide-react";

export default function LicensePage() {
  const { t, lang } = useI18n();
  const router = useRouter();
  const [status, setStatus] = useState<LicenseStatusResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [verifyKey, setVerifyKey] = useState("");
  const [verifying, setVerifying] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  const fetchStatus = useCallback(async () => {
    setError(null);
    try {
      const res = await getLicenseStatus();
      setStatus(res);
    } catch (e) {
      setError(e instanceof Error ? e.message : t("common.fetchFailed"));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!getToken()) { router.replace("/login"); return; }
    fetchStatus();
  }, [fetchStatus, router]);

  async function handleVerify(e: React.FormEvent) {
    e.preventDefault();
    setMsg(null);
    if (!verifyKey.trim()) { setMsg(t("license.licenseKeyRequired")); return; }
    setVerifying(true);
    try {
      const res = await verifyLicense(verifyKey.trim());
      setStatus(res);
      setMsg(res.message);
    } catch (err) {
      const m = err instanceof Error ? err.message : t("common.verifyFailed");
      setMsg(m);
      await fetchStatus();
    } finally {
      setVerifying(false);
    }
  }

  const variant = status?.status === "valid" ? "success" as const : status?.status === "invalid" ? "danger" as const : "secondary" as const;

  return (
    <div className="space-y-4 max-w-[900px]">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-semibold"><BadgeCheck className="h-6 w-6" /> License</h1>
          <p className="text-sm text-muted-foreground">{t("license.subtitle")}</p>
        </div>
        <Button variant="outline" size="sm" onClick={() => { setLoading(true); fetchStatus(); }} disabled={loading}><RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} /> {t("common.refresh")}</Button>
      </div>

      {error && <div className="rounded-md border border-destructive/50 bg-destructive/10 px-3 py-2 text-sm text-destructive" role="alert">{error}</div>}
      {msg && <div className="rounded-md border bg-card px-3 py-2 text-sm" role="status">{msg}</div>}

      <Card>
        <CardHeader className="pb-3"><CardTitle className="text-base">{t("license.currentStatus")}</CardTitle><CardDescription>{t("license.currentStatusDesc")}</CardDescription></CardHeader>
        <CardContent>
          {loading ? <p className="text-sm text-muted-foreground">{t("common.loading")}</p> : status ? (
            <div className="space-y-3 text-sm">
              <div className="flex items-center gap-2"><span className="text-muted-foreground">{t("license.status")}</span><Badge variant={variant}>{status.status}</Badge><span className="text-xs text-muted-foreground">{status.message}</span></div>
              <div className="grid gap-2 sm:grid-cols-2">
                <div><span className="text-muted-foreground">{t("license.edition")}</span><div className="font-medium">{status.edition} (BSL {status.bsl_version})</div></div>
                <div><span className="text-muted-foreground">{t("license.holder")}</span><div className="font-medium">{status.holder ?? "-"}</div></div>
                <div className="sm:col-span-2"><span className="text-muted-foreground">{t("license.licenseKey")}</span><div className="font-mono text-xs break-all">{status.license_key ?? "-"}</div></div>
                <div><span className="text-muted-foreground">{t("license.verifiedAt")}</span><div className="text-xs">{status.verified_at ? new Date(status.verified_at).toLocaleString(lang === "ko" ? "ko-KR" : "en-US") : "-"}</div></div>
                <div><span className="text-muted-foreground">{t("license.expiresAt")}</span><div className="text-xs">{status.expires_at ? new Date(status.expires_at).toLocaleString(lang === "ko" ? "ko-KR" : "en-US") : "-"}</div></div>
              </div>
            </div>
          ) : <p className="text-sm text-muted-foreground">{t("license.noData")}</p>}
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-3"><CardTitle className="text-base">{t("license.verifyTitle")}</CardTitle><CardDescription>{t("license.verifyDesc")}</CardDescription></CardHeader>
        <CardContent>
          <form onSubmit={handleVerify} className="flex flex-col gap-3 sm:flex-row sm:items-end">
            <div className="flex-1 space-y-1">
              <Label htmlFor="license_key">{t("license.licenseKeyLabel")}</Label>
              <Input id="license_key" placeholder={t("license.licenseKeyPlaceholder")} value={verifyKey} onChange={(e) => setVerifyKey(e.target.value)} />
            </div>
            <Button type="submit" disabled={verifying}>{verifying ? t("common.verifying") : t("license.verifyBtn")}</Button>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
