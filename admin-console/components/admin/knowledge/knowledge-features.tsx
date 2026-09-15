"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { Database, Play, RefreshCw, Save } from "lucide-react";
import { DataTable, ErrorState, FormField, Skeleton, StatusBadge, useTableQuery, useToast } from "@/components/admin";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { normalizeAdminError } from "@/lib/admin-api/client";
import { applyClientListQuery } from "@/lib/admin-api/list-query";
import type { AdminError, ColumnDef } from "@/lib/admin-api/types";
import {
  getEmbeddingConfig,
  getKnowledgeOpsStatus,
  getToken,
  postKnowledgeSync,
  updateEmbeddingConfig,
  type EmbeddingConfig,
  type KnowledgeCheckpoint,
  type KnowledgeOpsStatus,
  type KnowledgeSyncResult,
} from "@/lib/api";
import { useI18n } from "@/lib/i18n";

function useAuthRedirect() {
  const router = useRouter();
  React.useEffect(() => { if (!getToken()) router.replace("/login"); }, [router]);
}

function formatTime(value: string | null | undefined, lang: string) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat(lang, { dateStyle: "medium", timeStyle: "short" }).format(date);
}

export function KnowledgeOperationsFeature() {
  useAuthRedirect();
  const { t, lang } = useI18n();
  const { toast } = useToast();
  const table = useTableQuery();
  const [tenant, setTenant] = React.useState("default");
  const [connector, setConnector] = React.useState("notion");
  const [dryRun, setDryRun] = React.useState(true);
  const [status, setStatus] = React.useState<KnowledgeOpsStatus>();
  const [lastResult, setLastResult] = React.useState<KnowledgeSyncResult>();
  const [loading, setLoading] = React.useState(true);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<AdminError>();
  const refresh = React.useCallback(async (background = false) => {
    if (!background) setLoading(true);
    try {
      const next = await getKnowledgeOpsStatus(tenant.trim() || undefined);
      setStatus(next); setError(undefined);
      if (next.known_connectors.length && !next.known_connectors.includes(connector)) setConnector(next.known_connectors[0]);
    } catch (cause) {
      setError(normalizeAdminError(cause));
      if (background) toast({ title: t("admin.lists.backgroundError"), variant: "error" });
    } finally { setLoading(false); }
  }, [connector, t, tenant, toast]);
  // The tenant filter is applied through the explicit refresh button.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  React.useEffect(() => { if (getToken()) void refresh(); }, []);
  const result = React.useMemo(() => applyClientListQuery(status?.checkpoints ?? [], table.query, [(row) => row.tenant_id, (row) => row.source_system, (row) => row.cursor], { source: (row) => row.source_system, synced: (row) => row.last_sync_at, updated: (row) => row.updated_at }), [status?.checkpoints, table.query]);
  const columns = React.useMemo<ColumnDef<KnowledgeCheckpoint>[]>(() => [{ id: "source", header: t("knowledgeOps.connector"), accessor: (row) => row.source_system, sortable: true }, { id: "tenant", header: t("knowledgeOps.tenant"), accessor: (row) => row.tenant_id }, { id: "cursor", header: t("admin.phase5.knowledge.operations.cursor"), cell: (row) => <span className="font-mono text-xs">{row.cursor ?? "—"}</span> }, { id: "synced", header: t("admin.phase5.knowledge.operations.lastSync"), sortable: true, cell: (row) => formatTime(row.last_sync_at, lang) }, { id: "updated", header: t("admin.phase5.knowledge.operations.updated"), sortable: true, cell: (row) => formatTime(row.updated_at, lang) }], [lang, t]);
  async function sync() {
    setBusy(true);
    try {
      const next = await postKnowledgeSync({ connector, tenant_id: tenant.trim() || "default", dry_run: dryRun });
      setLastResult(next);
      toast({ title: next.dry_run ? t("knowledgeOps.syncPlanned") : t("knowledgeOps.syncEnqueued"), description: next.job_id, variant: next.dry_run ? "info" : "success" });
      if (!next.dry_run) await refresh(true);
    } catch (cause) {
      toast({ title: t("knowledgeOps.actionFailed"), description: cause instanceof Error ? cause.message : undefined, variant: "error" });
    } finally { setBusy(false); }
  }
  return <div className="space-y-6"><header><h1 className="flex items-center gap-2 text-2xl font-semibold"><Database aria-hidden="true" className="h-6 w-6" />{t("knowledgeOps.title")}</h1><p className="mt-1 text-sm text-muted-foreground">{t("knowledgeOps.subtitle")}</p></header><Card><CardHeader><CardTitle className="text-base">{t("admin.phase5.knowledge.operations.runTitle")}</CardTitle><CardDescription>{t("admin.phase5.knowledge.operations.runDescription")}</CardDescription></CardHeader><CardContent className="grid gap-4 md:grid-cols-3"><FormField id="knowledge-tenant" label={t("knowledgeOps.tenant")}><Input value={tenant} onChange={(event) => setTenant(event.target.value)} /></FormField><FormField id="knowledge-connector" label={t("knowledgeOps.connector")}><Input value={connector} list="knowledge-connectors" onChange={(event) => setConnector(event.target.value)} /></FormField><datalist id="knowledge-connectors">{(status?.known_connectors ?? ["notion", "outline"]).map((item) => <option key={item} value={item} />)}</datalist><label className="flex items-center gap-2 self-end pb-2 text-sm"><input type="checkbox" checked={dryRun} onChange={(event) => setDryRun(event.target.checked)} />{t("knowledgeOps.dryRun")}</label><div className="flex gap-2 md:col-span-3"><Button onClick={() => void sync()} disabled={busy}><Play aria-hidden="true" className="h-4 w-4" />{t("knowledgeOps.sync")}</Button><Button variant="outline" onClick={() => void refresh()} disabled={loading}><RefreshCw aria-hidden="true" className="h-4 w-4" />{t("common.refresh")}</Button></div></CardContent></Card>{lastResult ? <Card><CardHeader><CardTitle className="flex items-center gap-2 text-base">{t("admin.phase5.knowledge.operations.lastResult")}<StatusBadge status={lastResult.dry_run || lastResult.enqueued ? "healthy" : "warning"} label={lastResult.dry_run ? t("knowledgeOps.dryRun") : lastResult.enqueued ? t("knowledgeOps.syncEnqueued") : t("admin.common.status.warning")} /></CardTitle></CardHeader><CardContent><dl className="grid gap-2 text-sm sm:grid-cols-2"><dt>{t("common.id")}</dt><dd className="font-mono">{lastResult.job_id ?? "—"}</dd><dt>{t("knowledgeOps.via")}</dt><dd>{lastResult.via ?? "—"}</dd></dl></CardContent></Card> : null}<DataTable rows={result.rows} columns={columns} rowKey={(row) => `${row.tenant_id}-${row.source_system}`} loading={loading} error={error} onRetry={() => void refresh()} query={table.query} onQueryChange={table.onQueryChange} totalRows={result.totalRows} searchable ariaLabel={t("admin.phase5.knowledge.operations.history")} empty={{ title: t("admin.phase5.knowledge.operations.empty"), description: t("admin.phase5.knowledge.operations.emptyDescription"), filtered: Boolean(table.query.search) }} /><div className="flex flex-wrap gap-2 text-sm"><StatusBadge status={(status?.pending_connectors.length ?? 0) > 0 ? "warning" : "healthy"} label={`${t("knowledgeOps.pending")}: ${status?.pending_connectors.length ?? 0}`} /><span className="rounded-md border px-2 py-1">{t("knowledgeOps.documents")}: {status?.document_count ?? 0}</span></div></div>;
}

const EMBEDDING_DEFAULTS: Record<string, { model: string; dim: number; apiUrl: string }> = {
  ollama: { model: "mxbai-embed-large", dim: 1024, apiUrl: "http://127.0.0.1:11434" },
  "openai-compatible": { model: "text-embedding-3-small", dim: 1536, apiUrl: "" },
  fake: { model: "fake-embedding", dim: 64, apiUrl: "" },
};

export function EmbeddingFeature() {
  useAuthRedirect();
  const { t } = useI18n();
  const { toast } = useToast();
  const [config, setConfig] = React.useState<EmbeddingConfig>();
  const [provider, setProvider] = React.useState("ollama");
  const [model, setModel] = React.useState("");
  const [dimension, setDimension] = React.useState("1024");
  const [apiUrl, setApiUrl] = React.useState("");
  const [loading, setLoading] = React.useState(true);
  const [saving, setSaving] = React.useState(false);
  const [error, setError] = React.useState<string>();
  const [fieldErrors, setFieldErrors] = React.useState<Record<string, string>>({});
  const refresh = React.useCallback(async () => { setLoading(true); try { const next = await getEmbeddingConfig(); setConfig(next); setProvider(next.provider || "ollama"); setModel(next.model || EMBEDDING_DEFAULTS[next.provider]?.model || ""); setDimension(String(next.dim || EMBEDDING_DEFAULTS[next.provider]?.dim || 1024)); setApiUrl(next.api_url || EMBEDDING_DEFAULTS[next.provider]?.apiUrl || ""); setError(undefined); } catch (cause) { setError(cause instanceof Error ? cause.message : t("embedding.loadFailed")); } finally { setLoading(false); } }, [t]);
  React.useEffect(() => { if (getToken()) void refresh(); }, [refresh]);
  function chooseProvider(nextProvider: string) { const defaults = EMBEDDING_DEFAULTS[nextProvider]; setProvider(nextProvider); if (defaults) { setModel(defaults.model); setDimension(String(defaults.dim)); setApiUrl(defaults.apiUrl); } }
  async function save(event: React.FormEvent) {
    event.preventDefault();
    const nextErrors: Record<string, string> = {};
    const parsedDimension = Number(dimension);
    if (!model.trim()) nextErrors.model = t("admin.phase5.knowledge.embedding.modelRequired");
    if (!Number.isInteger(parsedDimension) || parsedDimension <= 0) nextErrors.dimension = t("admin.phase5.knowledge.embedding.dimensionInvalid");
    if (apiUrl.trim()) { try { const parsed = new URL(apiUrl.trim()); if (!['http:', 'https:'].includes(parsed.protocol)) throw new Error(); } catch { nextErrors.apiUrl = t("admin.phase5.knowledge.embedding.urlInvalid"); } }
    setFieldErrors(nextErrors); if (Object.keys(nextErrors).length) return;
    setSaving(true);
    try { const next = await updateEmbeddingConfig({ provider, model: model.trim(), dim: parsedDimension, api_url: apiUrl.trim() }); setConfig(next); toast({ title: t("embedding.saved"), description: next.applied ? t("embedding.appliedYes") : t("embedding.appliedNo"), variant: next.applied ? "success" : "warning" }); }
    catch (cause) { setError(cause instanceof Error ? cause.message : t("embedding.saveFailed")); toast({ title: t("embedding.saveFailed"), variant: "error" }); }
    finally { setSaving(false); }
  }
  return <div className="space-y-6"><header><h1 className="flex items-center gap-2 text-2xl font-semibold"><Database aria-hidden="true" className="h-6 w-6" />{t("embedding.title")}</h1><p className="mt-1 text-sm text-muted-foreground">{t("embedding.subtitle")}</p></header>{error ? <ErrorState compact title={t("embedding.loadFailed")} description={error} retry={() => void refresh()} /> : null}{loading ? <Skeleton variant="form" rows={4} /> : <Card><CardHeader><CardTitle className="flex flex-wrap items-center gap-2 text-base">{t("admin.phase5.knowledge.embedding.configuration")}<StatusBadge status={config?.applied ? "healthy" : "warning"} label={config?.applied ? t("embedding.appliedYes") : t("embedding.appliedNo")} /></CardTitle><CardDescription>{config?.note ?? t("admin.phase5.knowledge.embedding.defaultsDescription")}</CardDescription></CardHeader><CardContent><form onSubmit={save} className="grid gap-4 md:grid-cols-2"><FormField id="embedding-provider" label={t("embedding.provider")} hint={t("admin.phase5.knowledge.embedding.candidateHint")}><select value={provider} onChange={(event) => chooseProvider(event.target.value)} className="h-9 w-full rounded-md border bg-background px-3">{Object.keys(EMBEDDING_DEFAULTS).map((item) => <option key={item}>{item}</option>)}</select></FormField><FormField id="embedding-model" label={t("embedding.model")} required error={fieldErrors.model}><Input value={model} onChange={(event) => setModel(event.target.value)} /></FormField><FormField id="embedding-dimension" label={t("embedding.dim")} required error={fieldErrors.dimension}><Input inputMode="numeric" value={dimension} onChange={(event) => setDimension(event.target.value)} /></FormField><FormField id="embedding-url" label={t("embedding.apiUrl")} error={fieldErrors.apiUrl} hint={t("admin.phase5.knowledge.embedding.urlHint")}><Input value={apiUrl} onChange={(event) => setApiUrl(event.target.value)} /></FormField><div className="flex gap-2 md:col-span-2"><Button type="submit" disabled={saving}><Save aria-hidden="true" className="h-4 w-4" />{t("embedding.save")}</Button><Button type="button" variant="outline" onClick={() => void refresh()}>{t("common.refresh")}</Button></div></form></CardContent></Card>}</div>;
}
