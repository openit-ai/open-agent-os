"use client";
import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { getToken, listRuntimeSnapshots, getRuntimeSnapshot, createRuntimeSnapshot, publishRuntimeSnapshot, rollbackRuntimeSnapshot, getRuntimeStatus, getRuntimeAppliedStatus, getRuntimeConfig, type RuntimeConfigSnapshot, type RuntimeStatusResponse, type RuntimeAppliedStatusResponse } from "@/lib/api";
import { RefreshCw, UploadCloud, History, ShieldCheck, AlertTriangle, FileText, CheckCircle2, RotateCcw, Plus, Eye } from "lucide-react";
import { useI18n } from "@/lib/i18n";

function hashPrefix(h: string | null | undefined, len = 12): string {
  if (!h) return "-";
  return h.length > len ? `${h.slice(0, len)}…` : h;
}
function sigPrefix(s: string | null | undefined, len = 16): string {
  if (!s) return "-";
  return s.length > len ? `${s.slice(0, len)}…` : s;
}
function isFailClosed(msg: string | null): boolean {
  if (!msg) return false;
  const m = msg.toLowerCase();
  return m.includes("fail-closed") || m.includes("503") || m.includes("must be set in production") || m.includes("signing key");
}

function langLocale(): string {
  try {
    const v = typeof window !== "undefined" ? localStorage.getItem("oaos_lang") : null;
    return v === "ko" ? "ko-KR" : "en-US";
  } catch {
    return "en-US";
  }
}

export default function RuntimeConfigPage() {
  const router = useRouter();
  const { t } = useI18n();
  const [tenant, setTenant] = useState("default");
  const [snapshots, setSnapshots] = useState<RuntimeConfigSnapshot[]>([]);
  const [total, setTotal] = useState(0);
  const [selectedVersion, setSelectedVersion] = useState<number | null>(null);
  const [detail, setDetail] = useState<RuntimeConfigSnapshot | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);

  const [status, setStatus] = useState<RuntimeStatusResponse | null>(null);
  const [applied, setApplied] = useState<RuntimeAppliedStatusResponse | null>(null);
  const [publishedSnap, setPublishedSnap] = useState<RuntimeConfigSnapshot | null>(null);

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [actionMsg, setActionMsg] = useState<string | null>(null);
  const [actionErr, setActionErr] = useState<string | null>(null);

  const [expectedVersion, setExpectedVersion] = useState("");
  const [note, setNote] = useState("");
  const [creating, setCreating] = useState(false);
  const [publishing, setPublishing] = useState<number | null>(null);
  const [rollingBack, setRollingBack] = useState<number | null>(null);

  const fetchAll = useCallback(async () => {
    setError(null);
    setLoading(true);
    try {
      const [snapRes, st, ap] = await Promise.allSettled([
        listRuntimeSnapshots(tenant || "default"),
        getRuntimeStatus(tenant || "default"),
        getRuntimeAppliedStatus(tenant || "default"),
      ]);
      if (snapRes.status === "fulfilled") {
        const v = snapRes.value as unknown as { items: RuntimeConfigSnapshot[]; count: number };
        const items = Array.isArray((v as unknown as RuntimeConfigSnapshot[])) ? (v as unknown as RuntimeConfigSnapshot[]) : (v.items ?? []);
        setSnapshots(items.slice().sort((a, b) => a.version - b.version));
        setTotal(typeof v.count === "number" ? v.count : items.length);
        // auto-select latest published or latest
        if (items.length && selectedVersion == null) {
          const pub = items.find((x) => x.published);
          setSelectedVersion(pub ? pub.version : items[items.length - 1].version);
        }
      } else {
        setError(snapRes.reason instanceof Error ? snapRes.reason.message : t("runtimeConfig.fetchFailed"));
      }
      if (st.status === "fulfilled") setStatus(st.value as RuntimeStatusResponse);
      else setStatus(null);
      if (ap.status === "fulfilled") setApplied(ap.value as RuntimeAppliedStatusResponse);
      else setApplied(null);

      // try published snapshot fetch (404 is expected if none published)
      try {
        const pubSnap = await getRuntimeConfig(tenant || "default");
        setPublishedSnap(pubSnap as RuntimeConfigSnapshot);
      } catch {
        setPublishedSnap(null);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : t("runtimeConfig.fetchFailed"));
    } finally {
      setLoading(false);
    }
  }, [tenant, t, selectedVersion]);

  useEffect(() => {
    if (!getToken()) {
      router.replace("/login");
      return;
    }
    fetchAll();
  }, [fetchAll, router]);

  // detail fetch when selectedVersion changes
  useEffect(() => {
    if (selectedVersion == null) {
      setDetail(null);
      return;
    }
    let cancelled = false;
    async function load() {
      setDetailLoading(true);
      setDetailError(null);
      try {
        const d = await getRuntimeSnapshot(selectedVersion as number, tenant || "default");
        if (!cancelled) setDetail(d);
      } catch (e) {
        if (!cancelled) setDetailError(e instanceof Error ? e.message : t("runtimeConfig.fetchFailed"));
      } finally {
        if (!cancelled) setDetailLoading(false);
      }
    }
    load();
    return () => {
      cancelled = true;
    };
  }, [selectedVersion, tenant, t]);

  async function handleCreate() {
    setActionMsg(null);
    setActionErr(null);
    setCreating(true);
    try {
      const payload: { tenant_id?: string; expected_version?: number; note?: string } = {};
      if (tenant && tenant.trim() && tenant.trim() !== "default") payload.tenant_id = tenant.trim();
      else if (tenant.trim()) payload.tenant_id = tenant.trim();
      if (expectedVersion.trim()) {
        const n = Number(expectedVersion.trim());
        if (!Number.isInteger(n) || n < 1) throw new Error("expected_version must be integer >=1");
        payload.expected_version = n;
      }
      if (note.trim()) payload.note = note.trim();
      const created = await createRuntimeSnapshot(payload);
      setActionMsg(t("runtimeConfig.createSuccess", { version: String(created.version) }));
      setExpectedVersion("");
      setNote("");
      await fetchAll();
      setSelectedVersion(created.version);
    } catch (e) {
      const msg = e instanceof Error ? e.message : t("runtimeConfig.createFailed");
      setActionErr(msg);
    } finally {
      setCreating(false);
    }
  }

  async function handlePublish(version: number) {
    if (!confirm(t("runtimeConfig.confirmPublish", { version: String(version) }))) return;
    setActionMsg(null);
    setActionErr(null);
    setPublishing(version);
    try {
      const payload: { tenant_id?: string; version: number } = { version };
      if (tenant && tenant.trim()) payload.tenant_id = tenant.trim();
      const res = await publishRuntimeSnapshot(payload);
      setActionMsg(t("runtimeConfig.publishSuccess", { version: String(res.published_version ?? version) }));
      await fetchAll();
    } catch (e) {
      setActionErr(e instanceof Error ? e.message : t("runtimeConfig.publishFailed"));
    } finally {
      setPublishing(null);
    }
  }

  async function handleRollback(version: number) {
    if (!confirm(t("runtimeConfig.confirmRollback", { version: String(version) }))) return;
    setActionMsg(null);
    setActionErr(null);
    setRollingBack(version);
    try {
      const payload: { tenant_id?: string; version: number } = { version };
      if (tenant && tenant.trim()) payload.tenant_id = tenant.trim();
      const res = await rollbackRuntimeSnapshot(payload);
      setActionMsg(t("runtimeConfig.rollbackSuccess", { version: String(res.published_version ?? version) }));
      await fetchAll();
    } catch (e) {
      setActionErr(e instanceof Error ? e.message : t("runtimeConfig.rollbackFailed"));
    } finally {
      setRollingBack(null);
    }
  }

  const failClosedBanner = isFailClosed(error) || isFailClosed(actionErr) || isFailClosed(detailError);

  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-semibold"><FileText className="h-6 w-6" /> {t("runtimeConfig.title")}</h1>
          <p className="text-sm text-muted-foreground">{t("runtimeConfig.subtitle")}</p>
        </div>
        <Button variant="outline" size="sm" onClick={() => { setLoading(true); fetchAll(); }} disabled={loading}><RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} /> {t("runtimeConfig.refresh")}</Button>
      </div>

      {failClosedBanner && (
        <div className="rounded-md border border-amber-500/50 bg-amber-50 px-3 py-2 text-sm text-amber-900 dark:bg-amber-950/30 dark:text-amber-200" role="alert">
          <span className="flex items-center gap-2"><AlertTriangle className="h-4 w-4" /> {t("runtimeConfig.productionFailClosed")}</span>
        </div>
      )}

      {error && <div className="rounded-md border border-destructive/50 bg-destructive/10 px-3 py-2 text-sm text-destructive" role="alert">{error}</div>}
      {actionMsg && <div className="rounded-md border bg-card px-3 py-2 text-sm text-green-700 dark:text-green-300" role="status">{actionMsg}</div>}
      {actionErr && <div className="rounded-md border border-destructive/50 bg-destructive/10 px-3 py-2 text-sm text-destructive" role="alert">{actionErr}</div>}

      {/* Tenant + status row */}
      <div className="grid gap-4 lg:grid-cols-3">
        <Card className="lg:col-span-1">
          <CardHeader className="pb-3"><CardTitle className="text-base flex items-center gap-2"><ShieldCheck className="h-4 w-4" /> Tenant</CardTitle><CardDescription>{t("runtimeConfig.tenantHelp")}</CardDescription></CardHeader>
          <CardContent className="space-y-3">
            <div className="space-y-1">
              <Label className="text-xs">{t("runtimeConfig.tenantLabel")}</Label>
              <Input value={tenant} onChange={(e) => setTenant(e.target.value)} placeholder={t("runtimeConfig.tenantPlaceholder")} className="font-mono text-sm" />
            </div>
            <p className="text-xs text-muted-foreground">{t("runtimeConfig.secretNote")}</p>
            <p className="text-xs text-muted-foreground">{t("runtimeConfig.fallbackDescNote")}</p>
            <p className="text-xs text-muted-foreground">{t("runtimeConfig.l5Only")}</p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-3"><CardTitle className="text-base flex items-center gap-2"><CheckCircle2 className="h-4 w-4" /> {t("runtimeConfig.statusTitle")}</CardTitle><CardDescription>{t("runtimeConfig.statusDesc")}</CardDescription></CardHeader>
          <CardContent>
            {loading ? <p className="text-sm text-muted-foreground">{t("runtimeConfig.loading")}</p> : status ? (
              <div className="space-y-2 text-sm">
                <div className="flex justify-between"><span className="text-muted-foreground">{t("runtimeConfig.publishedVersion")}</span><span className="font-mono font-medium">{status.published_version ?? "-"}</span></div>
                <div className="flex justify-between"><span className="text-muted-foreground">{t("runtimeConfig.configHashPrefix")}</span><span className="font-mono text-xs" title={status.config_hash ?? ""}>{hashPrefix(status.config_hash)}</span></div>
                <div className="flex justify-between"><span className="text-muted-foreground">Signature valid</span>{status.signature_valid == null ? <Badge variant="secondary">n/a</Badge> : status.signature_valid ? <Badge variant="success">{t("runtimeConfig.signatureValid")}</Badge> : <Badge variant="danger">{t("runtimeConfig.signatureInvalid")}</Badge>}</div>
                <div className="flex justify-between"><span className="text-muted-foreground">{t("runtimeConfig.processIdentity")}</span><span className="font-mono text-xs truncate max-w-[150px]" title={status.process_identity ?? ""}>{status.process_identity ?? "-"}</span></div>
                {status.error && <div className="rounded border bg-amber-50 px-2 py-1 text-xs text-amber-900 dark:bg-amber-950/30 dark:text-amber-200">{status.error}</div>}
                {!status.has_snapshot && <div className="text-xs text-muted-foreground">{t("runtimeConfig.noPublished")}</div>}
              </div>
            ) : <p className="text-sm text-muted-foreground">{t("common.noData")}</p>}
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-3"><CardTitle className="text-base flex items-center gap-2"><History className="h-4 w-4" /> {t("runtimeConfig.appliedStatusTitle")}</CardTitle><CardDescription>{t("runtimeConfig.appliedDesc")}</CardDescription></CardHeader>
          <CardContent>
            {loading ? <p className="text-sm text-muted-foreground">{t("runtimeConfig.loading")}</p> : applied ? (
              <div className="space-y-2 text-sm">
                <div className="flex justify-between"><span className="text-muted-foreground">{t("runtimeConfig.publishedVersion")}</span><span className="font-mono font-medium">{applied.published_version ?? "-"}</span></div>
                <div className="flex justify-between"><span className="text-muted-foreground">{t("runtimeConfig.appliedVersion")}</span><span className="font-mono font-medium">{applied.applied_version ?? "-"}</span></div>
                <div className="flex justify-between"><span className="text-muted-foreground">{t("runtimeConfig.configHashPrefix")}</span><span className="font-mono text-xs" title={applied.config_hash ?? applied.published_hash ?? ""}>{hashPrefix(applied.config_hash ?? applied.published_hash)}</span></div>
                <div className="flex justify-between"><span className="text-muted-foreground">{t("runtimeConfig.processIdentity")}</span><span className="font-mono text-xs truncate max-w-[150px]" title={applied.process_identity ?? (applied.applied as unknown as { process_identity?: string })?.process_identity ?? ""}>{applied.process_identity ?? (applied.applied as unknown as { process_identity?: string })?.process_identity ?? "-"}</span></div>
                {applied.applied_at && <div className="flex justify-between"><span className="text-muted-foreground">Applied at</span><span className="font-mono text-xs">{new Date(applied.applied_at).toLocaleString(langLocale())}</span></div>}
                {applied.error && <div className="rounded border bg-destructive/10 px-2 py-1 text-xs text-destructive">{applied.error}</div>}
                {applied.cp_live && <details className="rounded border bg-muted/30 px-2 py-1"><summary className="cursor-pointer text-xs font-medium">CP live (proxy)</summary><pre className="mt-1 max-h-32 overflow-auto text-[11px] whitespace-pre-wrap break-all">{JSON.stringify(applied.cp_live, null, 2)}</pre></details>}
                {!applied.applied && <div className="text-xs text-muted-foreground">No applied record — Control Plane has not applied published config yet.</div>}
              </div>
            ) : <p className="text-sm text-muted-foreground">{t("common.noData")}</p>}
          </CardContent>
        </Card>
      </div>

      {/* Current published config summary */}
      {publishedSnap && (
        <Card className="border-primary/30">
          <CardHeader className="pb-2"><CardTitle className="text-sm flex items-center gap-2"><Eye className="h-4 w-4" /> Published snapshot v{publishedSnap.version} — {hashPrefix(publishedSnap.config_hash)} · {sigPrefix(publishedSnap.signature)}</CardTitle><CardDescription>Config hash prefix + signature prefix + created by {publishedSnap.created_by} · Secrets never displayed</CardDescription></CardHeader>
          <CardContent>
            <div className="flex flex-wrap gap-2 text-xs">
              <Badge variant="secondary">mode: {(publishedSnap.config?.runtime_mode as string) ?? "-"}</Badge>
              <Badge variant="outline">{t("runtimeConfig.infraCount")}: {Array.isArray(publishedSnap.config?.infra) ? (publishedSnap.config.infra as unknown[]).length : 0}</Badge>
              <Badge variant="outline">{t("runtimeConfig.providerCount")}: {Array.isArray(publishedSnap.config?.llm_providers) ? (publishedSnap.config.llm_providers as unknown[]).length : 0}</Badge>
              <Badge variant="outline">{t("runtimeConfig.mappingCount")}: {Array.isArray(publishedSnap.config?.user_mappings) ? (publishedSnap.config.user_mappings as unknown[]).length : 0}</Badge>
              {publishedSnap.published_by && <Badge variant="success">by {publishedSnap.published_by}</Badge>}
            </div>
          </CardContent>
        </Card>
      )}

      {/* Create snapshot */}
      <Card>
        <CardHeader className="pb-3"><CardTitle className="text-base flex items-center gap-2"><Plus className="h-4 w-4" /> {t("runtimeConfig.createSnapshot")}</CardTitle><CardDescription>L5 only · Optimistic concurrency via expected_version · Produces HMAC-signed snapshot (fail-closed in production if key missing)</CardDescription></CardHeader>
        <CardContent>
          <div className="grid gap-3 sm:grid-cols-3">
            <div className="space-y-1">
              <Label className="text-xs">{t("runtimeConfig.expectedVersion")}</Label>
              <Input value={expectedVersion} onChange={(e) => setExpectedVersion(e.target.value)} placeholder={t("runtimeConfig.expectedVersionPlaceholder")} inputMode="numeric" />
            </div>
            <div className="space-y-1 sm:col-span-2">
              <Label className="text-xs">{t("runtimeConfig.noteLabel")}</Label>
              <Input value={note} onChange={(e) => setNote(e.target.value)} placeholder={t("runtimeConfig.notePlaceholder")} />
            </div>
          </div>
          <div className="mt-3 flex items-center gap-2">
            <Button size="sm" onClick={handleCreate} disabled={creating}><UploadCloud className="mr-1 h-4 w-4" />{creating ? t("runtimeConfig.creating") : t("runtimeConfig.createSnapshot")}</Button>
            <span className="text-xs text-muted-foreground">{t("runtimeConfig.tenantLabel")}: <span className="font-mono">{tenant || "default"}</span></span>
          </div>
        </CardContent>
      </Card>

      {/* Snapshots list + detail */}
      <div className="grid gap-4 lg:grid-cols-5">
        <Card className="lg:col-span-3">
          <CardHeader className="pb-3"><CardTitle className="text-base flex items-center gap-2"><History className="h-4 w-4" /> {t("runtimeConfig.snapshotsTitle")}</CardTitle><CardDescription>{t("runtimeConfig.snapshotsDesc", { count: String(total) })}</CardDescription></CardHeader>
          <CardContent className="p-0">
            <div className="overflow-x-auto">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>{t("runtimeConfig.versionCol")}</TableHead>
                    <TableHead>{t("runtimeConfig.hashCol")}</TableHead>
                    <TableHead>{t("runtimeConfig.sigCol")}</TableHead>
                    <TableHead>{t("runtimeConfig.createdByCol")}</TableHead>
                    <TableHead>{t("runtimeConfig.createdAtCol")}</TableHead>
                    <TableHead>Status</TableHead>
                    <TableHead className="text-right">{t("common.actions")}</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {loading ? (
                    <TableRow><TableCell colSpan={7} className="text-center text-muted-foreground">{t("runtimeConfig.loading")}</TableCell></TableRow>
                  ) : snapshots.length === 0 ? (
                    <TableRow><TableCell colSpan={7} className="text-center text-muted-foreground">{t("runtimeConfig.noSnapshots")}</TableCell></TableRow>
                  ) : snapshots.map((s) => (
                    <TableRow key={`${s.tenant_id}:${s.version}`} className={selectedVersion === s.version ? "bg-accent/50" : "cursor-pointer hover:bg-accent/30"} onClick={() => setSelectedVersion(s.version)}>
                      <TableCell className="font-mono font-medium">v{s.version}{s.parent_version != null ? <span className="ml-1 text-[11px] text-muted-foreground">←{s.parent_version}</span> : null}</TableCell>
                      <TableCell className="font-mono text-xs" title={s.config_hash}>{hashPrefix(s.config_hash)}</TableCell>
                      <TableCell className="font-mono text-xs" title={s.signature}>{sigPrefix(s.signature)}</TableCell>
                      <TableCell className="text-xs truncate max-w-[120px]" title={s.created_by}>{s.created_by}</TableCell>
                      <TableCell className="text-xs" title={s.created_at}>{s.created_at ? new Date(s.created_at).toLocaleString(langLocale()) : "-"}</TableCell>
                      <TableCell>
                        <div className="flex flex-wrap gap-1">
                          {s.published ? <Badge variant="success">{t("runtimeConfig.publishedBadge")}</Badge> : <Badge variant="secondary">{t("runtimeConfig.draftBadge")}</Badge>}
                          {s.rollback_from != null && <Badge variant="warning">{t("runtimeConfig.rollbackFrom")} {s.rollback_from}</Badge>}
                        </div>
                      </TableCell>
                      <TableCell>
                        <div className="flex justify-end gap-1" onClick={(e) => e.stopPropagation()}>
                          <Button variant="outline" size="sm" onClick={() => setSelectedVersion(s.version)}><Eye className="h-3 w-3" /> Detail</Button>
                          <Button variant="default" size="sm" disabled={!!publishing || !!rollingBack} onClick={() => handlePublish(s.version)}>{publishing === s.version ? t("runtimeConfig.publishing") : t("runtimeConfig.publish")}</Button>
                          <Button variant="secondary" size="sm" disabled={!!publishing || !!rollingBack} onClick={() => handleRollback(s.version)}>{rollingBack === s.version ? t("runtimeConfig.rollingBack") : t("runtimeConfig.rollback")}</Button>
                        </div>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
            <p className="px-4 py-2 text-xs text-muted-foreground">{t("common.mobileScrollNote")}</p>
          </CardContent>
        </Card>

        <Card className="lg:col-span-2">
          <CardHeader className="pb-3"><CardTitle className="text-base">{t("runtimeConfig.detailTitle")}</CardTitle><CardDescription>{selectedVersion != null ? `v${selectedVersion} · tenant ${tenant || "default"}` : t("runtimeConfig.chooseVersion")}</CardDescription></CardHeader>
          <CardContent>
            {selectedVersion == null ? <p className="text-sm text-muted-foreground">{t("runtimeConfig.detailEmpty")}</p> : detailLoading ? <p className="text-sm text-muted-foreground">{t("runtimeConfig.loading")}</p> : detailError ? <div className="rounded-md border border-destructive/50 bg-destructive/10 px-3 py-2 text-sm text-destructive" role="alert">{detailError}{isFailClosed(detailError) && <div className="mt-1 text-xs font-medium">{t("runtimeConfig.productionFailClosed")}</div>}</div> : detail ? (
              <div className="space-y-3 text-sm">
                <div className="grid grid-cols-2 gap-2">
                  <div><span className="text-muted-foreground text-xs">Version</span><div className="font-mono font-bold">v{detail.version}</div></div>
                  <div><span className="text-muted-foreground text-xs">Tenant</span><div className="font-mono">{detail.tenant_id}</div></div>
                  <div><span className="text-muted-foreground text-xs">{t("runtimeConfig.configHashPrefix")}</span><div className="font-mono text-xs break-all" title={detail.config_hash}>{detail.config_hash}</div><div className="text-[11px] text-muted-foreground">{hashPrefix(detail.config_hash)}</div></div>
                  <div><span className="text-muted-foreground text-xs">{t("runtimeConfig.signaturePrefix")}</span><div className="font-mono text-xs break-all" title={detail.signature}>{detail.signature}</div><div className="text-[11px] text-muted-foreground">{sigPrefix(detail.signature)}</div></div>
                  <div><span className="text-muted-foreground text-xs">{t("runtimeConfig.createdByCol")}</span><div className="text-xs truncate">{detail.created_by}</div></div>
                  <div><span className="text-muted-foreground text-xs">{t("runtimeConfig.parentCol")}</span><div className="font-mono text-xs">{detail.parent_version ?? "-"}</div></div>
                  <div><span className="text-muted-foreground text-xs">{t("runtimeConfig.createdAtCol")}</span><div className="text-xs">{detail.created_at ? new Date(detail.created_at).toLocaleString(langLocale()) : "-"}</div></div>
                  <div><span className="text-muted-foreground text-xs">{t("runtimeConfig.publishedAt")}</span><div className="text-xs">{detail.published_at ? new Date(detail.published_at).toLocaleString(langLocale()) : "-"}</div></div>
                  {detail.published_by && <div><span className="text-muted-foreground text-xs">{t("runtimeConfig.publishedBy")}</span><div className="text-xs">{detail.published_by}</div></div>}
                  {detail.rollback_from != null && <div><span className="text-muted-foreground text-xs">{t("runtimeConfig.rollbackFrom")}</span><div className="font-mono text-xs">{detail.rollback_from}</div></div>}
                </div>
                <div className="flex flex-wrap gap-2">
                  {detail.published ? <Badge variant="success">{t("runtimeConfig.publishedBadge")}</Badge> : <Badge variant="secondary">{t("runtimeConfig.draftBadge")}</Badge>}
                  {detail.published_at && <Badge variant="outline">{detail.published_at.slice(0, 10)}</Badge>}
                </div>
                <div className="rounded border bg-muted/20 p-2">
                  <p className="text-xs font-medium mb-1">{t("runtimeConfig.counts")}</p>
                  <div className="flex flex-wrap gap-2 text-xs">
                    <Badge variant="outline">mode: {(detail.config?.runtime_mode as string) ?? "-"}</Badge>
                    <Badge variant="outline">infra: {Array.isArray(detail.config?.infra) ? (detail.config.infra as unknown[]).length : 0}</Badge>
                    <Badge variant="outline">providers: {Array.isArray(detail.config?.llm_providers) ? (detail.config.llm_providers as unknown[]).length : "-"}</Badge>
                    <Badge variant="outline">mappings: {Array.isArray(detail.config?.user_mappings) ? (detail.config.user_mappings as unknown[]).length : 0}</Badge>
                    <Badge variant="outline">fallback: {detail.config?.fallback ? "yes" : "no"}</Badge>
                  </div>
                  <p className="mt-2 text-[11px] text-muted-foreground">{t("runtimeConfig.secretNote")}</p>
                  <details className="mt-2">
                    <summary className="cursor-pointer text-xs font-medium">Config JSON (no secrets — hash/signature prefix only)</summary>
                    <pre className="mt-1 max-h-64 overflow-auto rounded bg-card p-2 text-[11px] whitespace-pre-wrap break-all border">{JSON.stringify(detail.config, null, 2)}</pre>
                  </details>
                </div>
                <div className="flex gap-2">
                  <Button size="sm" onClick={() => handlePublish(detail.version)} disabled={!!publishing || !!rollingBack}><UploadCloud className="mr-1 h-4 w-4" />{publishing === detail.version ? t("runtimeConfig.publishing") : t("runtimeConfig.publish")}</Button>
                  <Button size="sm" variant="secondary" onClick={() => handleRollback(detail.version)} disabled={!!publishing || !!rollingBack}><RotateCcw className="mr-1 h-4 w-4" />{rollingBack === detail.version ? t("runtimeConfig.rollingBack") : t("runtimeConfig.rollback")}</Button>
                </div>
              </div>
            ) : <p className="text-sm text-muted-foreground">{t("runtimeConfig.detailEmpty")}</p>}
          </CardContent>
        </Card>
      </div>
      <p className="text-xs text-muted-foreground">{t("runtimeConfig.secretNote")} · {t("runtimeConfig.productionFailClosed")}</p>
    </div>
  );
}
