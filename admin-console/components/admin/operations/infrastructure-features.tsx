"use client";

import * as React from "react";
import { Activity, Pencil, Plus, RefreshCw, Server, Trash2 } from "lucide-react";
import { ConfirmDialog, ErrorState, Skeleton, StatusBadge } from "@/components/admin";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { apiFetch } from "@/lib/api";
import { useI18n } from "@/lib/i18n";

export interface InfrastructureRow {
  id: string;
  service?: string;
  name?: string;
  display_name?: string;
  host: string;
  port: number;
  health_path?: string;
  probe_type?: string;
  source?: "db" | "live" | "both";
  status?: "healthy" | "unhealthy" | "unknown";
  latency_ms?: number | null;
  last_check?: string | null;
  db_exists?: boolean;
}

interface InfrastructureResponse {
  items?: InfrastructureRow[];
  registry?: InfrastructureRow[];
  probed_at?: string;
}

function rowsFrom(response: InfrastructureResponse | InfrastructureRow[]) {
  return Array.isArray(response) ? response : response.items ?? response.registry ?? [];
}

function statusFor(row: InfrastructureRow): "healthy" | "warning" | "failed" | "unknown" {
  if (row.status === "healthy") return "healthy";
  if (row.status === "unhealthy") return "failed";
  if (row.status === "unknown") return "warning";
  return "unknown";
}

export function OperationsHealthFeature() {
  const { t, lang } = useI18n();
  const [rows, setRows] = React.useState<InfrastructureRow[]>([]);
  const [checkedAt, setCheckedAt] = React.useState<string>();
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<string>();

  const refresh = React.useCallback(async () => {
    try {
      const response = await apiFetch<InfrastructureResponse>("/v1/infra/unified");
      setRows(rowsFrom(response));
      setCheckedAt(response.probed_at);
      setError(undefined);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : t("admin.operations.health.loadFailed"));
    } finally {
      setLoading(false);
    }
  }, [t]);

  React.useEffect(() => {
    void refresh();
    const interval = window.setInterval(refresh, 15_000);
    return () => window.clearInterval(interval);
  }, [refresh]);

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-start justify-between gap-3"><div><h1 className="flex items-center gap-2 text-2xl font-semibold"><Activity aria-hidden="true" className="h-6 w-6" />{t("admin.operations.health.title")}</h1><p className="mt-1 text-sm text-muted-foreground">{t("admin.operations.health.description")}</p></div><Button variant="outline" onClick={() => void refresh()}><RefreshCw aria-hidden="true" className="h-4 w-4" />{t("common.refresh")}</Button></header>
      {error ? <ErrorState title={t("admin.operations.health.loadFailed")} description={error} retry={() => void refresh()} /> : null}
      {loading ? <Skeleton variant="table" rows={5} /> : (
        <Card><CardHeader><CardTitle className="text-base">{t("admin.operations.health.observedServices")}{checkedAt ? <span className="ml-2 text-xs font-normal text-muted-foreground">{new Intl.DateTimeFormat(lang, { dateStyle: "medium", timeStyle: "short" }).format(new Date(checkedAt))}</span> : null}</CardTitle></CardHeader><CardContent className="p-0"><Table><TableHeader><TableRow><TableHead>{t("infra.service")}</TableHead><TableHead>{t("infra.status")}</TableHead><TableHead>{t("infra.latency")}</TableHead><TableHead>{t("infra.source")}</TableHead><TableHead>{t("infra.lastCheck")}</TableHead></TableRow></TableHeader><TableBody>{rows.length === 0 ? <TableRow><TableCell colSpan={5} className="text-center text-muted-foreground">{t("infra.noData")}</TableCell></TableRow> : rows.map((row) => <TableRow key={row.id}><TableCell><span className="font-medium">{row.display_name || row.service || row.name}</span><span className="ml-2 font-mono text-xs text-muted-foreground">{row.host}:{row.port}</span></TableCell><TableCell><StatusBadge status={statusFor(row)} label={row.status ?? "unknown"} /></TableCell><TableCell>{row.latency_ms == null ? "-" : `${row.latency_ms} ms`}</TableCell><TableCell>{row.source ?? "-"}</TableCell><TableCell className="text-xs">{row.last_check ? new Intl.DateTimeFormat(lang, { dateStyle: "short", timeStyle: "short" }).format(new Date(row.last_check)) : "-"}</TableCell></TableRow>)}</TableBody></Table></CardContent></Card>
      )}
    </div>
  );
}

const serviceOptions = ["control-plane", "memory", "admin-api", "admin-console", "nginx", "mattermost", "hermes", "outline", "postgres", "redis", "execution-gateway", "security"];

export function ServicesRegistryFeature() {
  const { t } = useI18n();
  const [rows, setRows] = React.useState<InfrastructureRow[]>([]);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<string>();
  const [editing, setEditing] = React.useState<InfrastructureRow>();
  const [deleting, setDeleting] = React.useState<InfrastructureRow>();
  const [service, setService] = React.useState(serviceOptions[0]);
  const [host, setHost] = React.useState("");
  const [port, setPort] = React.useState("");
  const [healthPath, setHealthPath] = React.useState("/health");
  const [saving, setSaving] = React.useState(false);

  const refresh = React.useCallback(async () => {
    try {
      const response = await apiFetch<InfrastructureResponse | InfrastructureRow[]>("/v1/infra");
      setRows(rowsFrom(response));
      setError(undefined);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : t("admin.operations.services.loadFailed"));
    } finally {
      setLoading(false);
    }
  }, [t]);

  React.useEffect(() => { void refresh(); }, [refresh]);

  function resetForm() {
    setEditing(undefined); setService(serviceOptions[0]); setHost(""); setPort(""); setHealthPath("/health");
  }

  function startEdit(row: InfrastructureRow) {
    setEditing(row); setService(row.service || row.name || serviceOptions[0]); setHost(row.host); setPort(String(row.port)); setHealthPath(row.health_path || "/health");
  }

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    const parsedPort = Number(port);
    if (!host.trim() || !Number.isInteger(parsedPort) || parsedPort < 1 || parsedPort > 65535) {
      setError(t("admin.operations.services.invalidTarget")); return;
    }
    setSaving(true); setError(undefined);
    try {
      const payload = { service, host: host.trim(), port: parsedPort, health_path: healthPath.trim() || "/health" };
      await apiFetch(editing ? `/v1/infra/${editing.id}` : "/v1/infra", { method: editing ? "PATCH" : "POST", body: JSON.stringify(payload) });
      resetForm(); await refresh();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : t("admin.operations.services.saveFailed"));
    } finally { setSaving(false); }
  }

  async function remove() {
    if (!deleting) return;
    await apiFetch(`/v1/infra/${deleting.id}`, { method: "DELETE" });
    setDeleting(undefined); await refresh();
  }

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-start justify-between gap-3"><div><h1 className="flex items-center gap-2 text-2xl font-semibold"><Server aria-hidden="true" className="h-6 w-6" />{t("admin.operations.services.title")}</h1><p className="mt-1 text-sm text-muted-foreground">{t("admin.operations.services.description")}</p></div><Button variant="outline" onClick={() => void refresh()}><RefreshCw aria-hidden="true" className="h-4 w-4" />{t("common.refresh")}</Button></header>
      {error ? <ErrorState title={t("admin.operations.services.actionFailed")} description={error} retry={() => void refresh()} compact /> : null}
      <Card><CardHeader><CardTitle className="text-base">{editing ? t("admin.operations.services.edit") : t("admin.operations.services.register")}</CardTitle></CardHeader><CardContent><form onSubmit={submit} className="grid gap-4 md:grid-cols-2 lg:grid-cols-5"><div><Label htmlFor="service">{t("infra.service")}</Label><select id="service" value={service} onChange={(event) => setService(event.target.value)} className="mt-1 flex h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm">{serviceOptions.map((option) => <option key={option}>{option}</option>)}</select></div><div><Label htmlFor="host">{t("infra.host")}</Label><Input id="host" value={host} onChange={(event) => setHost(event.target.value)} /></div><div><Label htmlFor="port">{t("infra.port")}</Label><Input id="port" inputMode="numeric" value={port} onChange={(event) => setPort(event.target.value)} /></div><div><Label htmlFor="health-path">{t("infra.healthPath")}</Label><Input id="health-path" value={healthPath} onChange={(event) => setHealthPath(event.target.value)} /></div><div className="flex items-end gap-2"><Button type="submit" disabled={saving}><Plus aria-hidden="true" className="h-4 w-4" />{editing ? t("infra.update") : t("infra.register")}</Button>{editing ? <Button type="button" variant="outline" onClick={resetForm}>{t("infra.cancel")}</Button> : null}</div></form></CardContent></Card>
      {loading ? <Skeleton variant="table" rows={5} /> : <Card><CardContent className="p-0"><Table><TableHeader><TableRow><TableHead>{t("infra.service")}</TableHead><TableHead>{t("admin.operations.services.target")}</TableHead><TableHead>{t("infra.healthPath")}</TableHead><TableHead className="text-right">{t("common.actions")}</TableHead></TableRow></TableHeader><TableBody>{rows.length === 0 ? <TableRow><TableCell colSpan={4} className="text-center text-muted-foreground">{t("infra.noData")}</TableCell></TableRow> : rows.map((row) => <TableRow key={row.id}><TableCell className="font-medium">{row.display_name || row.service || row.name}</TableCell><TableCell className="font-mono text-xs">{row.host}:{row.port}</TableCell><TableCell className="font-mono text-xs">{row.health_path || "-"}</TableCell><TableCell><div className="flex justify-end gap-1"><Button type="button" size="icon" variant="ghost" aria-label={t("common.edit")} onClick={() => startEdit(row)}><Pencil aria-hidden="true" className="h-4 w-4" /></Button><Button type="button" size="icon" variant="ghost" aria-label={t("common.delete")} onClick={() => setDeleting(row)}><Trash2 aria-hidden="true" className="h-4 w-4" /></Button></div></TableCell></TableRow>)}</TableBody></Table></CardContent></Card>}
      <ConfirmDialog open={Boolean(deleting)} title={t("admin.operations.services.deleteTitle")} description={t("admin.operations.services.deleteDescription")} targetLabel={deleting?.display_name || deleting?.service || deleting?.name} confirmLabel={t("common.delete")} tone="danger" onConfirm={remove} onOpenChange={(open) => { if (!open) setDeleting(undefined); }} />
    </div>
  );
}
