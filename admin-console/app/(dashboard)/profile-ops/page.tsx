"use client";
import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { getToken, getProfileOpsStatus, postProfileBackfill, postProfileReset, type ProfileOpsStatus, type ProfileBackfillResult, type ProfileResetResult } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { UserCog, Loader2 } from "lucide-react";

export default function ProfileOpsPage() {
  const router = useRouter();
  const { t } = useI18n();
  const [tenant, setTenant] = useState("default");
  const [userId, setUserId] = useState("");
  const [status, setStatus] = useState<ProfileOpsStatus | null>(null);
  const [reason, setReason] = useState("");
  const [confirm, setConfirm] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [lastBackfill, setLastBackfill] = useState<ProfileBackfillResult | null>(null);
  const [lastReset, setLastReset] = useState<ProfileResetResult | null>(null);

  const fetchStatus = useCallback(async () => {
    setError(null);
    try {
      const s = await getProfileOpsStatus(tenant.trim() || undefined, userId.trim() || undefined);
      setStatus(s);
    } catch (e) {
      setError(e instanceof Error ? e.message : t("profileOps.loadFailed"));
    } finally {
      setLoading(false);
    }
  }, [tenant, userId, t]);

  useEffect(() => {
    if (!getToken()) { router.replace("/login"); return; }
    fetchStatus();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const reload = () => { setLoading(true); fetchStatus(); };

  const backfill = async () => {
    setBusy(true); setError(null); setMsg(null);
    try {
      const res = await postProfileBackfill({ tenant_id: tenant.trim() || "default", user_id: userId.trim() || "default", reason: reason.trim() || undefined });
      setLastBackfill(res);
      setMsg(`${t("profileOps.backfilled")}: ${res.job_id} (${t("profileOps.via")} ${res.via})`);
      reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : t("profileOps.actionFailed"));
    } finally { setBusy(false); }
  };

  const reset = async () => {
    setBusy(true); setError(null); setMsg(null);
    try {
      const res = await postProfileReset({ tenant_id: tenant.trim() || "default", user_id: userId.trim() || "default", confirm });
      setLastReset(res);
      setConfirm("");
      setMsg(`${t("profileOps.resetDone")} (${t("profileOps.via")} ${res.via ?? "?"})`);
      reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : t("profileOps.actionFailed"));
    } finally { setBusy(false); }
  };

  if (loading) return <div className="p-6 text-sm text-muted-foreground">{t("common.loading")}</div>;

  const resultJson = lastBackfill ?? lastReset ? JSON.stringify(lastBackfill ?? lastReset, null, 2) : null;

  return (
    <div className="space-y-6 p-6">
      <div>
        <h1 className="flex items-center gap-2 text-2xl font-semibold"><UserCog className="h-6 w-6" /> {t("profileOps.title")}</h1>
        <p className="text-sm text-muted-foreground">{t("profileOps.subtitle")}</p>
      </div>
      {error && <Card className="border-red-500"><CardContent className="pt-4 text-sm text-red-600">{error}</CardContent></Card>}
      {msg && <Card className="border-green-500"><CardContent className="pt-4 text-sm text-green-700">{msg}</CardContent></Card>}

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            {t("profileOps.profiles")}
            {status && <Badge variant="secondary">{t("profileOps.source")}: {status.source}</Badge>}
            {status && (status.profile_exists
              ? <Badge className="bg-green-600 text-white">{t("profileOps.profileExists")}</Badge>
              : <Badge variant="secondary">evidence: {status.evidence_count}</Badge>)}
          </CardTitle>
          {status?.note && <CardDescription>{status.note}</CardDescription>}
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex flex-wrap items-end gap-3">
            <div className="space-y-1">
              <Label htmlFor="po-tenant">{t("profileOps.tenant")}</Label>
              <Input id="po-tenant" value={tenant} onChange={(e) => setTenant(e.target.value)} className="w-48" />
            </div>
            <div className="space-y-1">
              <Label htmlFor="po-user">{t("profileOps.user")}</Label>
              <Input id="po-user" value={userId} onChange={(e) => setUserId(e.target.value)} className="w-48" placeholder="user id (optional)" />
            </div>
            <Button variant="outline" onClick={reload}>{t("common.refresh")}</Button>
          </div>
          {status && (
            <div className="flex flex-wrap gap-2 text-sm">
              <Badge variant="outline">{t("profileOps.profiles")}: {status.profile_count}</Badge>
              <Badge variant="outline">{t("profileOps.traits")}: {status.trait_count}</Badge>
              <Badge variant="outline">{t("profileOps.evidence")}: {status.evidence_count}</Badge>
              <Badge variant="outline">{t("profileOps.queueDepth")}: {status.worker_queue_depth}</Badge>
            </div>
          )}
          <div className="flex flex-wrap items-end gap-3">
            <div className="space-y-1">
              <Label htmlFor="po-reason">{t("profileOps.backfillReason")}</Label>
              <Input id="po-reason" value={reason} onChange={(e) => setReason(e.target.value)} className="w-64" />
            </div>
            <Button onClick={backfill} disabled={busy}>
              {busy && <Loader2 className="mr-1 h-4 w-4 animate-spin" />}{t("profileOps.backfill")}
            </Button>
          </div>
          <div className="flex flex-wrap items-end gap-3">
            <div className="space-y-1">
              <Label htmlFor="po-confirm">{t("profileOps.resetConfirmLabel")}</Label>
              <Input id="po-confirm" value={confirm} onChange={(e) => setConfirm(e.target.value)} className="w-48" placeholder="RESET" />
            </div>
            <Button variant="destructive" onClick={reset} disabled={busy || confirm !== "RESET"} title={confirm !== "RESET" ? t("profileOps.resetConfirmBad") : undefined}>
              {busy && <Loader2 className="mr-1 h-4 w-4 animate-spin" />}{t("profileOps.reset")}
            </Button>
          </div>
        </CardContent>
      </Card>

      {resultJson && (
        <Card>
          <CardHeader><CardTitle>{t("common.status")}</CardTitle></CardHeader>
          <CardContent><pre className="max-h-64 overflow-auto rounded bg-muted p-3 text-xs">{resultJson}</pre></CardContent>
        </Card>
      )}
    </div>
  );
}
