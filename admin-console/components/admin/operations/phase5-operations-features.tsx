"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { BadgeCheck, DatabaseBackup, ExternalLink, RefreshCw, ShieldAlert, Upload } from "lucide-react";
import { DataTable, EmptyState, ErrorState, FormField, Skeleton, StatusBadge, useTableQuery, useToast, useUrlFilter } from "@/components/admin";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { normalizeAdminError } from "@/lib/admin-api/client";
import { applyClientListQuery } from "@/lib/admin-api/list-query";
import type { AdminError, ColumnDef } from "@/lib/admin-api/types";
import {
  getBackupStatus,
  getLicenseStatus,
  getSecurityUpdates,
  getToken,
  getUpgradeStatus,
  triggerBackup,
  verifyLicense,
  type BackupRecord,
  type BackupStatusResponse,
  type LicenseStatusResponse,
  type SecurityUpdatesResponse,
  type UpgradeStatusResponse,
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

function statusFor(value: string): "healthy" | "warning" | "failed" | "unknown" {
  const normalized = value.toLowerCase();
  if (["completed", "valid", "installed", "idle"].includes(normalized)) return "healthy";
  if (["pending", "in_progress", "medium", "low"].includes(normalized)) return "warning";
  if (["failed", "invalid", "critical", "high", "expired"].includes(normalized)) return "failed";
  return "unknown";
}

export function BackupFeature() {
  useAuthRedirect();
  const { t, lang } = useI18n();
  const { toast } = useToast();
  const table = useTableQuery();
  const [backup, setBackup] = React.useState<BackupStatusResponse>();
  const [upgrade, setUpgrade] = React.useState<UpgradeStatusResponse>();
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<AdminError>();
  const [triggering, setTriggering] = React.useState(false);
  const refresh = React.useCallback(async (background = false) => {
    if (!background) setLoading(true);
    try { const [nextBackup, nextUpgrade] = await Promise.all([getBackupStatus(), getUpgradeStatus()]); setBackup(nextBackup); setUpgrade(nextUpgrade); setError(undefined); }
    catch (cause) { setError(normalizeAdminError(cause)); if (background) toast({ title: t("admin.lists.backgroundError"), variant: "error" }); }
    finally { setLoading(false); }
  }, [t, toast]);
  React.useEffect(() => { if (getToken()) void refresh(); }, [refresh]);
  const result = React.useMemo(() => applyClientListQuery(backup?.backups ?? [], table.query, [(row) => row.id, (row) => row.status, (row) => row.triggered_by], { id: (row) => row.id, status: (row) => row.expired ? "expired" : row.status, created: (row) => row.created_at, expires: (row) => row.expires_at, size: (row) => row.size_mb }), [backup?.backups, table.query]);
  const columns = React.useMemo<ColumnDef<BackupRecord>[]>(() => [{ id: "id", header: t("common.id"), sortable: true, cell: (row) => <span className="font-mono text-xs">{row.id}</span> }, { id: "status", header: t("common.status"), sortable: true, cell: (row) => <StatusBadge status={statusFor(row.expired ? "expired" : row.status)} label={row.expired ? t("admin.phase5.operations.backup.expired") : row.status} /> }, { id: "created", header: t("common.created"), sortable: true, cell: (row) => formatTime(row.created_at, lang) }, { id: "expires", header: t("admin.phase5.operations.backup.expires"), sortable: true, cell: (row) => formatTime(row.expires_at, lang) }, { id: "size", header: t("admin.phase5.operations.backup.size"), sortable: true, accessor: (row) => `${row.size_mb} MB` }, { id: "actor", header: t("admin.phase5.operations.backup.actor"), accessor: (row) => row.triggered_by ?? "—" }], [lang, t]);
  async function runBackup() {
    setTriggering(true);
    try { const resultBackup = await triggerBackup(); await refresh(true); toast({ title: t("admin.phase5.operations.backup.triggered"), description: resultBackup.backup.id, variant: "success" }); }
    catch (cause) { toast({ title: t("admin.phase5.operations.backup.failed"), description: cause instanceof Error ? cause.message : undefined, variant: "error" }); }
    finally { setTriggering(false); }
  }
  return <div className="space-y-6"><header className="flex flex-wrap items-start justify-between gap-3"><div><h1 className="flex items-center gap-2 text-2xl font-semibold"><DatabaseBackup aria-hidden="true" className="h-6 w-6" />{t("admin.phase5.operations.backup.title")}</h1><p className="mt-1 text-sm text-muted-foreground">{t("admin.phase5.operations.backup.description")}</p></div><div className="flex gap-2"><Button variant="outline" onClick={() => void refresh(true)}><RefreshCw aria-hidden="true" className="h-4 w-4" />{t("common.refresh")}</Button><Button onClick={() => void runBackup()} disabled={triggering}><Upload aria-hidden="true" className="h-4 w-4" />{triggering ? t("admin.phase5.operations.backup.triggering") : t("admin.phase5.operations.backup.trigger")}</Button></div></header>{upgrade ? <Card><CardHeader><CardTitle className="text-base">{t("admin.phase5.operations.backup.upgrade")}</CardTitle></CardHeader><CardContent className="grid gap-3 text-sm sm:grid-cols-3"><div><span className="text-muted-foreground">{t("admin.phase5.operations.backup.currentVersion")}</span><div className="font-mono">{upgrade.current_version}</div></div><div><span className="text-muted-foreground">{t("admin.phase5.operations.backup.availableVersion")}</span><div className="font-mono">{upgrade.available_version}</div></div><StatusBadge status={statusFor(upgrade.status)} label={upgrade.status} /></CardContent></Card> : null}<DataTable rows={result.rows} columns={columns} rowKey={(row) => row.id} loading={loading} error={error} onRetry={() => void refresh()} query={table.query} onQueryChange={table.onQueryChange} totalRows={result.totalRows} searchable ariaLabel={t("admin.phase5.operations.backup.history")} empty={{ title: t("admin.phase5.operations.backup.empty"), description: t("admin.phase5.operations.backup.emptyDescription"), primaryAction: { label: t("admin.phase5.operations.backup.trigger"), onClick: () => void runBackup() }, filtered: Boolean(table.query.search) }} /><p className="text-xs text-muted-foreground">{backup?.retention_policy ?? t("admin.phase5.operations.backup.retention")}</p></div>;
}

interface CveRow { key: string; version: string; available: boolean; releaseDate: string; id: string; severity: string; summary: string }

export function SecurityUpdatesFeature() {
  useAuthRedirect();
  const { t } = useI18n();
  const table = useTableQuery();
  const [severity, setSeverity] = useUrlFilter("severity", "all");
  const [data, setData] = React.useState<SecurityUpdatesResponse>();
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<AdminError>();
  const refresh = React.useCallback(async () => { setLoading(true); try { setData(await getSecurityUpdates()); setError(undefined); } catch (cause) { setError(normalizeAdminError(cause)); } finally { setLoading(false); } }, []);
  React.useEffect(() => { if (getToken()) void refresh(); }, [refresh]);
  const source = React.useMemo<CveRow[]>(() => (data?.updates ?? []).flatMap((update) => update.cves.map((cve) => ({ key: `${update.version}-${cve.id}`, version: update.version, available: update.available, releaseDate: update.release_date, id: cve.id, severity: cve.severity, summary: cve.summary }))).filter((row) => severity === "all" || row.severity.toLowerCase() === severity), [data?.updates, severity]);
  const result = React.useMemo(() => applyClientListQuery(source, table.query, [(row) => row.id, (row) => row.summary, (row) => row.version], { cve: (row) => row.id, severity: (row) => row.severity, version: (row) => row.version, released: (row) => row.releaseDate }), [source, table.query]);
  const columns = React.useMemo<ColumnDef<CveRow>[]>(() => [{ id: "cve", header: t("securityUpdates.cve"), sortable: true, cell: (row) => <a href={`https://nvd.nist.gov/vuln/detail/${encodeURIComponent(row.id)}`} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 font-mono text-xs underline" aria-label={t("admin.phase5.operations.security.openCve", { id: row.id })}>{row.id}<ExternalLink aria-hidden="true" className="h-3 w-3" /></a> }, { id: "severity", header: t("securityUpdates.severity"), sortable: true, cell: (row) => <StatusBadge status={statusFor(row.severity)} label={row.severity} /> }, { id: "version", header: t("admin.phase5.operations.security.version"), sortable: true, cell: (row) => <div><span className="font-mono">v{row.version}</span><div className="text-xs text-muted-foreground">{row.available ? t("admin.phase5.operations.security.available") : t("admin.phase5.operations.security.installed")}</div></div> }, { id: "summary", header: t("securityUpdates.summary"), accessor: (row) => row.summary }, { id: "released", header: t("admin.phase5.operations.security.released"), sortable: true, accessor: (row) => row.releaseDate }], [t]);
  return <div className="space-y-6"><header className="flex flex-wrap items-start justify-between gap-3"><div><h1 className="flex items-center gap-2 text-2xl font-semibold"><ShieldAlert aria-hidden="true" className="h-6 w-6" />{t("securityUpdates.title")}</h1><p className="mt-1 text-sm text-muted-foreground">{t("securityUpdates.subtitle")}</p></div><Button variant="outline" onClick={() => void refresh()}><RefreshCw aria-hidden="true" className="h-4 w-4" />{t("common.refresh")}</Button></header><div className="flex items-center gap-2"><label htmlFor="severity-filter" className="text-sm font-medium">{t("securityUpdates.severity")}</label><select id="severity-filter" value={severity} onChange={(event) => setSeverity(event.target.value)} className="h-9 rounded-md border bg-background px-3"><option value="all">{t("admin.phase5.all")}</option><option value="critical">Critical</option><option value="high">High</option><option value="medium">Medium</option><option value="low">Low</option></select></div><DataTable rows={result.rows} columns={columns} rowKey={(row) => row.key} loading={loading} error={error} onRetry={() => void refresh()} query={table.query} onQueryChange={table.onQueryChange} totalRows={result.totalRows} searchable ariaLabel={t("admin.phase5.operations.security.cveList")} empty={{ title: t("securityUpdates.noUpdates"), description: t("admin.phase5.filteredEmpty"), filtered: Boolean(table.query.search) || severity !== "all" }} /><p className="text-xs text-muted-foreground">{t("securityUpdates.listDesc", { version: data?.current_version ?? "—", count: data?.count ?? 0 })}</p></div>;
}

export function LicenseFeature() {
  useAuthRedirect();
  const { t, lang } = useI18n();
  const { toast } = useToast();
  const [status, setStatus] = React.useState<LicenseStatusResponse>();
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<string>();
  const [key, setKey] = React.useState("");
  const [fieldError, setFieldError] = React.useState<string>();
  const [verifying, setVerifying] = React.useState(false);
  const refresh = React.useCallback(async () => { setLoading(true); try { setStatus(await getLicenseStatus()); setError(undefined); } catch (cause) { setError(cause instanceof Error ? cause.message : t("common.fetchFailed")); } finally { setLoading(false); } }, [t]);
  React.useEffect(() => { if (getToken()) void refresh(); }, [refresh]);
  const expiresAt = status?.expires_at ? new Date(status.expires_at) : undefined;
  const daysLeft = expiresAt && !Number.isNaN(expiresAt.getTime()) ? Math.ceil((expiresAt.getTime() - Date.now()) / 86_400_000) : undefined;
  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!key.trim()) { setFieldError(t("license.licenseKeyRequired")); return; }
    setVerifying(true); setFieldError(undefined);
    try { const next = await verifyLicense(key.trim()); setKey(""); setStatus(next); toast({ title: t("admin.phase5.operations.license.verified"), variant: next.status === "valid" ? "success" : "warning" }); }
    catch (cause) { setKey(""); setFieldError(cause instanceof Error ? cause.message : t("common.verifyFailed")); toast({ title: t("common.verifyFailed"), variant: "error" }); }
    finally { setVerifying(false); }
  }
  return <div className="space-y-6"><header className="flex flex-wrap items-start justify-between gap-3"><div><h1 className="flex items-center gap-2 text-2xl font-semibold"><BadgeCheck aria-hidden="true" className="h-6 w-6" />{t("license.title")}</h1><p className="mt-1 text-sm text-muted-foreground">{t("license.subtitle")}</p></div><Button variant="outline" onClick={() => void refresh()}><RefreshCw aria-hidden="true" className="h-4 w-4" />{t("common.refresh")}</Button></header>{error ? <ErrorState title={t("common.fetchFailed")} description={error} retry={() => void refresh()} /> : null}{loading ? <Skeleton variant="card" rows={3} ariaLabel={t("admin.loading.content")} /> : status ? <Card><CardHeader><CardTitle className="flex flex-wrap items-center gap-2 text-base">{t("license.currentStatus")}<StatusBadge status={statusFor(status.status)} label={status.status} />{daysLeft !== undefined && daysLeft <= 30 ? <StatusBadge status={daysLeft < 0 ? "failed" : "warning"} label={daysLeft < 0 ? t("admin.phase5.operations.license.expired") : t("admin.phase5.operations.license.expiresSoon", { days: daysLeft })} /> : null}</CardTitle><CardDescription>{status.message}</CardDescription></CardHeader><CardContent><dl className="grid gap-3 text-sm sm:grid-cols-2"><div><dt className="text-muted-foreground">{t("license.edition")}</dt><dd>{status.edition} (BSL {status.bsl_version})</dd></div><div><dt className="text-muted-foreground">{t("license.holder")}</dt><dd>{status.holder ?? "—"}</dd></div><div><dt className="text-muted-foreground">{t("license.verifiedAt")}</dt><dd>{formatTime(status.verified_at, lang)}</dd></div><div><dt className="text-muted-foreground">{t("license.expiresAt")}</dt><dd>{formatTime(status.expires_at, lang)}</dd></div></dl><p className="mt-4 text-xs text-muted-foreground">{t("admin.phase5.operations.license.redactionNote")}</p></CardContent></Card> : <EmptyState title={t("license.noData")} description={t("license.currentStatusDesc")} />}<Card><CardHeader><CardTitle className="text-base">{t("license.verifyTitle")}</CardTitle><CardDescription>{t("license.verifyDesc")}</CardDescription></CardHeader><CardContent><form onSubmit={submit} className="flex flex-col gap-3 sm:flex-row sm:items-end"><div className="flex-1"><FormField id="license-key" label={t("license.licenseKeyLabel")} required error={fieldError} hint={t("admin.phase5.operations.license.inputHint")}><Input type="password" autoComplete="off" value={key} onChange={(event) => setKey(event.target.value)} /></FormField></div><Button type="submit" disabled={verifying}>{verifying ? t("common.verifying") : t("license.verifyBtn")}</Button></form></CardContent></Card></div>;
}
