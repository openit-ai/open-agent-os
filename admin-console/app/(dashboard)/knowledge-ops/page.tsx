"use client";
import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { getToken, getKnowledgeOpsStatus, postKnowledgeSync, type KnowledgeOpsStatus, type KnowledgeSyncResult } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { Database, Loader2 } from "lucide-react";

export default function KnowledgeOpsPage() {
  const router = useRouter();
  const { t } = useI18n();
  const [tenant, setTenant] = useState("default");
  const [connector, setConnector] = useState("notion");
  const [dryRun, setDryRun] = useState(true);
  const [status, setStatus] = useState<KnowledgeOpsStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [last, setLast] = useState<KnowledgeSyncResult | null>(null);

  const fetchStatus = useCallback(async () => {
    setError(null);
    try {
      const s = await getKnowledgeOpsStatus(tenant.trim() || undefined);
      setStatus(s);
      if (s.known_connectors?.length && !s.known_connectors.includes(connector)) {
        setConnector(s.known_connectors[0]);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : t("knowledgeOps.loadFailed"));
    } finally {
      setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tenant, t]);

  useEffect(() => {
    if (!getToken()) { router.replace("/login"); return; }
    fetchStatus();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const reload = () => { setLoading(true); fetchStatus(); };

  const sync = async () => {
    setBusy(true); setError(null); setMsg(null);
    try {
      const res = await postKnowledgeSync({ connector, tenant_id: tenant.trim() || "default", dry_run: dryRun });
      setLast(res);
      setMsg(res.dry_run ? t("knowledgeOps.syncPlanned") : `${t("knowledgeOps.syncEnqueued")}: ${res.job_id} (${t("knowledgeOps.via")} ${res.via})`);
      if (!res.dry_run) reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : t("knowledgeOps.actionFailed"));
    } finally { setBusy(false); }
  };

  if (loading) return <div className="p-6 text-sm text-muted-foreground">{t("common.loading")}</div>;

  const cps = status?.checkpoints ?? [];
  const resultJson = last ? JSON.stringify(last, null, 2) : null;

  return (
    <div className="space-y-6 p-6">
      <div>
        <h1 className="flex items-center gap-2 text-2xl font-semibold"><Database className="h-6 w-6" /> {t("knowledgeOps.title")}</h1>
        <p className="text-sm text-muted-foreground">{t("knowledgeOps.subtitle")}</p>
      </div>
      {error && <Card className="border-red-500"><CardContent className="pt-4 text-sm text-red-600">{error}</CardContent></Card>}
      {msg && <Card className="border-green-500"><CardContent className="pt-4 text-sm text-green-700">{msg}</CardContent></Card>}

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            {t("knowledgeOps.checkpoints")}
            {status && <Badge variant="secondary">{t("knowledgeOps.source")}: {status.source}</Badge>}
            <Badge variant="outline">{t("knowledgeOps.documents")}: {status?.document_count ?? 0}</Badge>
          </CardTitle>
          {status?.note && <CardDescription>{status.note}</CardDescription>}
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex flex-wrap items-end gap-3">
            <div className="space-y-1">
              <Label htmlFor="ko-tenant">{t("knowledgeOps.tenant")}</Label>
              <Input id="ko-tenant" value={tenant} onChange={(e) => setTenant(e.target.value)} className="w-48" />
            </div>
            <Button variant="outline" onClick={reload}>{t("common.refresh")}</Button>
          </div>
          <div className="flex flex-wrap gap-2 text-sm">
            <span className="text-muted-foreground">{t("knowledgeOps.synced")}:</span>
            {(status?.synced_connectors ?? []).length
              ? status!.synced_connectors.map((c) => <Badge key={c} className="bg-green-600 text-white">{c}</Badge>)
              : <Badge variant="secondary">—</Badge>}
            <span className="ml-3 text-muted-foreground">{t("knowledgeOps.pending")}:</span>
            {(status?.pending_connectors ?? []).length
              ? status!.pending_connectors.map((c) => <Badge key={c} className="bg-amber-500 text-white">{c}</Badge>)
              : <Badge variant="secondary">—</Badge>}
          </div>
          {cps.length > 0 && (
            <pre className="max-h-48 overflow-auto rounded bg-muted p-3 text-xs">{JSON.stringify(cps, null, 2)}</pre>
          )}
          <div className="flex flex-wrap items-end gap-3">
            <div className="space-y-1">
              <Label htmlFor="ko-connector">{t("knowledgeOps.connector")}</Label>
              <Input id="ko-connector" value={connector} onChange={(e) => setConnector(e.target.value)} className="w-48" list="ko-connectors" />
              <datalist id="ko-connectors">
                {(status?.known_connectors ?? ["notion", "outline"]).map((c) => <option key={c} value={c} />)}
              </datalist>
            </div>
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={dryRun} onChange={(e) => setDryRun(e.target.checked)} />
              {t("knowledgeOps.dryRun")}
            </label>
            <Button onClick={sync} disabled={busy}>
              {busy && <Loader2 className="mr-1 h-4 w-4 animate-spin" />}{t("knowledgeOps.sync")}
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
