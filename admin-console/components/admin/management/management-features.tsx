"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Flag, KeyRound, Link2, Lock, Pencil, Plus, RefreshCw, Trash2, UserCog, Users } from "lucide-react";
import {
  ConfirmDialog,
  DataTable,
  Dialog,
  EmptyState,
  ErrorState,
  FormField,
  Skeleton,
  StatusBadge,
  useTableQuery,
  useToast,
} from "@/components/admin";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { normalizeAdminError } from "@/lib/admin-api/client";
import { applyClientListQuery } from "@/lib/admin-api/list-query";
import type { AdminError, ColumnDef } from "@/lib/admin-api/types";
import {
  createMapping,
  deleteMapping,
  deleteUser,
  deriveAgentId,
  deriveEmployeePrincipal,
  getCredentialsStatus,
  getFeatureFlags,
  getMe,
  getProfileOpsStatus,
  getRotationGuide,
  getSecretsStatus,
  getToken,
  listMappings,
  listUsers,
  postProfileBackfill,
  postProfileReset,
  registerUser,
  resolveMmUser,
  syncPreview,
  toggleFeatureFlag,
  updateMapping,
  type AdminUserPublic,
  type CredentialProviderStatus,
  type CredentialsStatusResponse,
  type FeatureFlag as FeatureFlagRecord,
  type FeatureFlagsResponse,
  type ProfileBackfillResult,
  type ProfileOpsStatus,
  type ProfileResetResult,
  type RotationGuide,
  type SecretStatusItem,
  type SecretsStatus,
  type SyncPreviewItem,
  type UserMapping,
} from "@/lib/api";
import { useI18n } from "@/lib/i18n";

function formatTime(value: string | null | undefined, lang: string) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat(lang, { dateStyle: "medium", timeStyle: "short" }).format(date);
}

function statusFor(value: string | boolean): "healthy" | "warning" | "failed" | "unknown" {
  const normalized = String(value).toLowerCase();
  if (["true", "active", "enabled", "configured", "mapped", "verified"].includes(normalized)) return "healthy";
  if (["pending", "expired", "sync_pending", "rotation_needed"].includes(normalized)) return "warning";
  if (["false", "inactive", "revoked", "error", "missing"].includes(normalized)) return "failed";
  return "unknown";
}

function useAuthRedirect() {
  const router = useRouter();
  React.useEffect(() => {
    if (!getToken()) router.replace("/login");
  }, [router]);
}

type UserDeleteTarget = { kind: "account"; user: AdminUserPublic } | { kind: "mapping"; mapping: UserMapping };

export function UsersFeature() {
  useAuthRedirect();
  const { t, lang } = useI18n();
  const { toast } = useToast();
  const table = useTableQuery();
  const [tab, setTab] = React.useState("accounts");
  const [me, setMe] = React.useState<AdminUserPublic | null>(null);
  const [users, setUsers] = React.useState<AdminUserPublic[]>([]);
  const [mappings, setMappings] = React.useState<UserMapping[]>([]);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<AdminError>();
  const [accountDialog, setAccountDialog] = React.useState(false);
  const [mappingDialog, setMappingDialog] = React.useState(false);
  const [editingMapping, setEditingMapping] = React.useState<UserMapping>();
  const [deleting, setDeleting] = React.useState<UserDeleteTarget>();
  const [preview, setPreview] = React.useState<SyncPreviewItem[]>();
  const [previewOpen, setPreviewOpen] = React.useState(false);
  const [saving, setSaving] = React.useState(false);
  const [formError, setFormError] = React.useState<string>();
  const [email, setEmail] = React.useState("");
  const [displayName, setDisplayName] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [role, setRole] = React.useState<"L5" | "L4">("L4");
  const [mmUsername, setMmUsername] = React.useState("");
  const [mmUserId, setMmUserId] = React.useState("");
  const [employeePrincipal, setEmployeePrincipal] = React.useState("");
  const [mappingName, setMappingName] = React.useState("");

  const refresh = React.useCallback(async (background = false) => {
    if (!background) setLoading(true);
    try {
      const [meResult, usersResult, mappingsResult] = await Promise.all([getMe(), listUsers(), listMappings()]);
      setMe(meResult);
      setUsers(Array.isArray(usersResult) ? usersResult : usersResult.users ?? []);
      setMappings(Array.isArray(mappingsResult) ? mappingsResult : mappingsResult.mappings ?? mappingsResult.items ?? []);
      setError(undefined);
    } catch (cause) {
      setError(normalizeAdminError(cause));
      if (background) toast({ title: t("admin.lists.backgroundError"), variant: "error" });
    } finally {
      setLoading(false);
    }
  }, [t, toast]);

  React.useEffect(() => { if (getToken()) void refresh(); }, [refresh]);

  const visibleUsers = React.useMemo(() => applyClientListQuery(users, table.query, [(row) => row.email, (row) => row.display_name, (row) => row.role], {
    email: (row) => row.email, name: (row) => row.display_name, role: (row) => row.role, created: (row) => row.created_at,
  }), [table.query, users]);
  const visibleMappings = React.useMemo(() => applyClientListQuery(mappings, table.query, [(row) => row.mm_username, (row) => row.mm_user_id, (row) => row.employee_principal, (row) => row.agent_id], {
    username: (row) => row.mm_username, principal: (row) => row.employee_principal, status: (row) => row.status, created: (row) => row.created_at,
  }), [mappings, table.query]);

  function resetAccountForm() {
    setEmail(""); setDisplayName(""); setPassword(""); setRole("L4"); setFormError(undefined);
  }

  function resetMappingForm() {
    setEditingMapping(undefined); setMmUsername(""); setMmUserId(""); setEmployeePrincipal(""); setMappingName(""); setFormError(undefined);
  }

  async function saveAccount(event: React.FormEvent) {
    event.preventDefault();
    if (!email.trim() || !displayName.trim() || !password) { setFormError(t("users.validationRequired")); return; }
    if (password.length < 8) { setFormError(t("users.validationPassword")); return; }
    setSaving(true); setFormError(undefined);
    try {
      await registerUser({ email: email.trim(), display_name: displayName.trim(), password, role });
      setAccountDialog(false); resetAccountForm(); await refresh(true);
      toast({ title: t("admin.phase5.users.accountCreated"), variant: "success" });
    } catch (cause) { setFormError(cause instanceof Error ? cause.message : t("common.error")); }
    finally { setSaving(false); }
  }

  async function saveMapping(event: React.FormEvent) {
    event.preventDefault();
    const username = mmUsername.trim();
    if (!editingMapping && !username) { setFormError(t("users.validationMmUsername")); return; }
    const principal = employeePrincipal.trim() || deriveEmployeePrincipal(username, mmUserId.trim());
    if (!principal.startsWith("employee:")) { setFormError(t("users.validationPrincipal")); return; }
    setSaving(true); setFormError(undefined);
    try {
      if (editingMapping) {
        await updateMapping(editingMapping.id, { display_name: mappingName.trim() || null, employee_principal: principal });
      } else {
        let resolvedId = mmUserId.trim();
        let resolvedUsername = username;
        if (!resolvedId) {
          const resolved = await resolveMmUser(username);
          resolvedId = resolved.mm_user_id;
          resolvedUsername = resolved.mm_username || username;
        }
        await createMapping({ mm_user_id: resolvedId, mm_username: resolvedUsername, employee_principal: principal, display_name: mappingName.trim() || undefined });
      }
      setMappingDialog(false); resetMappingForm(); await refresh(true);
      toast({ title: t("admin.phase5.users.mappingSaved"), variant: "success" });
    } catch (cause) { setFormError(cause instanceof Error ? cause.message : t("common.error")); }
    finally { setSaving(false); }
  }

  async function confirmDelete() {
    if (!deleting) return;
    if (deleting.kind === "account") await deleteUser(deleting.user.id);
    else await deleteMapping(deleting.mapping.id);
    setDeleting(undefined); await refresh(true);
    toast({ title: t("admin.phase5.users.deleted"), variant: "success" });
  }

  async function openPreview() {
    setSaving(true);
    try {
      const result = await syncPreview();
      setPreview(Array.isArray(result) ? result : result.preview ?? result.items ?? []);
      setPreviewOpen(true);
    } catch (cause) { toast({ title: t("users.lookupFailed"), description: cause instanceof Error ? cause.message : undefined, variant: "error" }); }
    finally { setSaving(false); }
  }

  const userColumns = React.useMemo<ColumnDef<AdminUserPublic>[]>(() => [
    { id: "email", header: t("users.tableEmail"), accessor: (row) => row.email, sortable: true },
    { id: "name", header: t("users.tableName"), accessor: (row) => row.display_name, sortable: true },
    { id: "role", header: t("users.tableRole"), sortable: true, cell: (row) => <StatusBadge status={row.role === "L5" ? "healthy" : "unknown"} label={row.role} /> },
    { id: "created", header: t("users.tableCreated"), sortable: true, cell: (row) => formatTime(row.created_at, lang) },
  ], [lang, t]);
  const mappingColumns = React.useMemo<ColumnDef<UserMapping>[]>(() => [
    { id: "username", header: t("users.tableUsername"), accessor: (row) => row.mm_username ?? row.mm_user_id, sortable: true },
    { id: "principal", header: t("users.tablePrincipal"), sortable: true, cell: (row) => <div><div className="font-mono text-xs">{row.employee_principal}</div><div className="font-mono text-xs text-muted-foreground">{row.agent_id}</div></div> },
    { id: "status", header: t("users.tableStatus"), sortable: true, cell: (row) => <StatusBadge status={statusFor(row.status)} label={row.status} /> },
    { id: "created", header: t("users.tableCreatedCol"), sortable: true, cell: (row) => formatTime(row.created_at, lang) },
  ], [lang, t]);

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-start justify-between gap-3"><div><h1 className="flex items-center gap-2 text-2xl font-semibold"><Users aria-hidden="true" className="h-6 w-6" />{t("users.title")}</h1><p className="mt-1 text-sm text-muted-foreground">{t("admin.phase5.users.description")}</p></div><Button variant="outline" onClick={() => void refresh(true)} disabled={loading}><RefreshCw aria-hidden="true" className="h-4 w-4" />{t("common.refresh")}</Button></header>
      <Tabs value={tab} onValueChange={(next) => { setTab(next); table.onQueryChange({ ...table.query, search: undefined, page: 1 }); }}>
        <TabsList><TabsTrigger value="accounts"><Users aria-hidden="true" className="mr-1 h-4 w-4" />{t("users.tabsAdmin")}</TabsTrigger><TabsTrigger value="mappings"><Link2 aria-hidden="true" className="mr-1 h-4 w-4" />{t("users.tabsMapping")}</TabsTrigger></TabsList>
        <TabsContent value="accounts" className="space-y-4">
          <div className="flex justify-end"><Button onClick={() => { resetAccountForm(); setAccountDialog(true); }} disabled={me?.role !== "L5"}><Plus aria-hidden="true" className="h-4 w-4" />{t("users.register")}</Button></div>
          <DataTable rows={visibleUsers.rows} columns={userColumns} rowKey={(row) => row.id} loading={loading} error={error} onRetry={() => void refresh()} query={table.query} onQueryChange={table.onQueryChange} totalRows={visibleUsers.totalRows} searchable searchPlaceholderKey="admin.phase5.users.searchAccounts" ariaLabel={t("users.userList")} empty={{ title: t("users.noUsers"), description: t("users.adminRegisterDesc"), filtered: Boolean(table.query.search) }} rowActions={(row) => [{ id: "delete", label: t("common.delete"), icon: Trash2, tone: "danger", disabled: me?.role !== "L5" || me.id === row.id, onSelect: () => setDeleting({ kind: "account", user: row }) }]} />
        </TabsContent>
        <TabsContent value="mappings" className="space-y-4">
          <div className="flex flex-wrap justify-end gap-2"><Button variant="outline" onClick={() => void openPreview()} disabled={saving}>{t("users.syncPreview")}</Button><Button onClick={() => { resetMappingForm(); setMappingDialog(true); }} disabled={me?.role !== "L5"}><Plus aria-hidden="true" className="h-4 w-4" />{t("users.createMapping")}</Button></div>
          <DataTable rows={visibleMappings.rows} columns={mappingColumns} rowKey={(row) => row.id} loading={loading} error={error} onRetry={() => void refresh()} query={table.query} onQueryChange={table.onQueryChange} totalRows={visibleMappings.totalRows} searchable searchPlaceholderKey="admin.phase5.users.searchMappings" ariaLabel={t("users.mappingList")} empty={{ title: t("users.noMappings"), description: t("users.oneOneNote"), filtered: Boolean(table.query.search) }} rowActions={(row) => [{ id: "edit", label: t("common.edit"), icon: Pencil, disabled: me?.role !== "L5", onSelect: () => { setEditingMapping(row); setMmUsername(row.mm_username ?? ""); setMmUserId(row.mm_user_id); setEmployeePrincipal(row.employee_principal); setMappingName(row.display_name ?? ""); setFormError(undefined); setMappingDialog(true); } }, { id: "delete", label: t("common.delete"), icon: Trash2, tone: "danger", disabled: me?.role !== "L5", onSelect: () => setDeleting({ kind: "mapping", mapping: row }) }]} />
        </TabsContent>
      </Tabs>

      <Dialog open={accountDialog} onOpenChange={setAccountDialog} title={t("users.adminRegisterTitle")} description={t("users.adminRegisterDesc")}>
        <form onSubmit={saveAccount} className="space-y-4"><FormField id="account-email" label={t("users.email")} required error={formError && !email.trim() ? formError : undefined}><Input type="email" value={email} onChange={(event) => setEmail(event.target.value)} /></FormField><FormField id="account-name" label={t("users.displayName")} required><Input value={displayName} onChange={(event) => setDisplayName(event.target.value)} /></FormField><FormField id="account-password" label={t("users.password")} required error={formError}><Input type="password" autoComplete="new-password" value={password} onChange={(event) => setPassword(event.target.value)} /></FormField><FormField id="account-role" label={t("users.role")}><select value={role} onChange={(event) => setRole(event.target.value as "L5" | "L4")} className="h-9 w-full rounded-md border bg-background px-3"><option value="L4">{t("users.roleL4")}</option><option value="L5">{t("users.roleL5")}</option></select></FormField><div className="flex justify-end gap-2"><Button type="button" variant="outline" onClick={() => setAccountDialog(false)}>{t("common.cancel")}</Button><Button type="submit" disabled={saving}>{t("users.register")}</Button></div></form>
      </Dialog>
      <Dialog open={mappingDialog} onOpenChange={setMappingDialog} title={editingMapping ? t("admin.phase5.users.editMapping") : t("users.mappingTitle")} description={t("users.mappingDesc")}>
        <form onSubmit={saveMapping} className="space-y-4"><FormField id="mapping-username" label={t("users.mmUsername")} required={!editingMapping} error={formError}><Input value={mmUsername} disabled={Boolean(editingMapping)} onChange={(event) => setMmUsername(event.target.value)} /></FormField><FormField id="mapping-name" label={t("users.displayName")}><Input value={mappingName} onChange={(event) => setMappingName(event.target.value)} /></FormField><FormField id="mapping-principal" label={t("users.employeePrincipal")} hint={!employeePrincipal && mmUsername ? `${deriveEmployeePrincipal(mmUsername, mmUserId)} → ${deriveAgentId(deriveEmployeePrincipal(mmUsername, mmUserId))}` : undefined}><Input value={employeePrincipal} onChange={(event) => setEmployeePrincipal(event.target.value)} /></FormField><div className="flex justify-end gap-2"><Button type="button" variant="outline" onClick={() => setMappingDialog(false)}>{t("common.cancel")}</Button><Button type="submit" disabled={saving}>{t("common.save")}</Button></div></form>
      </Dialog>
      <Dialog open={previewOpen} onOpenChange={setPreviewOpen} title={t("users.syncPreviewTitle")}>
        {preview?.length ? <div className="max-h-96 overflow-auto space-y-2">{preview.map((item) => <div key={item.mm_user_id} className="rounded-md border p-3 text-sm"><div className="font-medium">{item.mm_username}</div><div className="font-mono text-xs text-muted-foreground">{item.employee_principal} → {item.agent_id}</div><StatusBadge status={item.already_mapped ? "healthy" : statusFor(item.status)} label={item.status} /></div>)}</div> : <EmptyState title={t("users.syncPreviewEmpty")} description={t("users.oneOneNote")} />}
      </Dialog>
      <ConfirmDialog open={Boolean(deleting)} onOpenChange={(open) => { if (!open) setDeleting(undefined); }} title={t("users.deleteConfirm")} description={t("admin.phase5.users.deleteDescription")} targetLabel={deleting?.kind === "account" ? deleting.user.email : deleting?.mapping.mm_username ?? deleting?.mapping.mm_user_id} confirmLabel={t("common.delete")} tone="danger" onConfirm={confirmDelete} />
    </div>
  );
}

export function CredentialsFeature() {
  useAuthRedirect();
  const { t, lang } = useI18n();
  const { toast } = useToast();
  const table = useTableQuery();
  const [data, setData] = React.useState<CredentialsStatusResponse>();
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<AdminError>();
  const [statusFilter, setStatusFilter] = React.useState("all");
  const refresh = React.useCallback(async (background = false) => {
    if (!background) setLoading(true);
    try { setData(await getCredentialsStatus()); setError(undefined); }
    catch (cause) { setError(normalizeAdminError(cause)); if (background) toast({ title: t("admin.lists.backgroundError"), variant: "error" }); }
    finally { setLoading(false); }
  }, [t, toast]);
  React.useEffect(() => { if (getToken()) void refresh(); }, [refresh]);
  const providers = React.useMemo(() => applyClientListQuery(data?.providers ?? [], table.query, [(row) => row.provider], { provider: (row) => row.provider, total: (row) => row.total, active: (row) => row.active }), [data?.providers, table.query]);
  const recentSource = React.useMemo(() => (data?.recent ?? []).filter((row) => statusFilter === "all" || row.status.toLowerCase() === statusFilter), [data?.recent, statusFilter]);
  const recent = React.useMemo(() => applyClientListQuery(recentSource, table.query, [(row) => row.id, (row) => row.user_id, (row) => row.agent_id, (row) => row.provider, (row) => row.scope], { created: (row) => row.created_at, provider: (row) => row.provider, status: (row) => row.status }), [recentSource, table.query]);
  const providerColumns = React.useMemo<ColumnDef<CredentialProviderStatus>[]>(() => [{ id: "provider", header: t("credentials.provider"), accessor: (row) => row.provider, sortable: true }, { id: "active", header: t("credentials.active"), accessor: (row) => row.active, sortable: true, align: "end" }, { id: "revoked", header: t("credentials.revoked"), accessor: (row) => row.revoked, align: "end" }, { id: "expired", header: t("credentials.expired"), accessor: (row) => row.expired, align: "end" }, { id: "total", header: t("credentials.total"), accessor: (row) => row.total, sortable: true, align: "end" }], [t]);
  const recentColumns = React.useMemo<ColumnDef<CredentialsStatusResponse["recent"][number]>[]>(() => [{ id: "id", header: t("common.id"), cell: (row) => <span className="font-mono text-xs">{row.id}</span> }, { id: "provider", header: t("credentials.provider"), accessor: (row) => row.provider, sortable: true }, { id: "scope", header: t("credentials.scope"), accessor: (row) => row.scope }, { id: "status", header: t("common.status"), sortable: true, cell: (row) => <StatusBadge status={statusFor(row.status)} label={row.status} /> }, { id: "created", header: t("credentials.created"), sortable: true, cell: (row) => formatTime(row.created_at, lang) }], [lang, t]);
  return <div className="space-y-6"><header className="flex flex-wrap items-start justify-between gap-3"><div><h1 className="flex items-center gap-2 text-2xl font-semibold"><KeyRound aria-hidden="true" className="h-6 w-6" />{t("credentials.title")}</h1><p className="mt-1 text-sm text-muted-foreground">{t("credentials.subtitle")}</p></div><Button variant="outline" onClick={() => void refresh(true)}><RefreshCw aria-hidden="true" className="h-4 w-4" />{t("common.refresh")}</Button></header><div className="grid gap-3 sm:grid-cols-4">{[["total", data?.total], ["active", data?.active], ["revoked", data?.revoked], ["expired", data?.expired]].map(([label, value]) => <Card key={String(label)}><CardHeader className="pb-2"><CardTitle className="text-sm">{t(`credentials.${label}`)}</CardTitle></CardHeader><CardContent className="text-2xl font-semibold">{loading ? "—" : value ?? 0}</CardContent></Card>)}</div><Card><CardHeader><CardTitle className="text-base">{t("credentials.providerStatus")}</CardTitle></CardHeader><CardContent><DataTable rows={providers.rows} columns={providerColumns} rowKey={(row) => row.provider} loading={loading} error={error} onRetry={() => void refresh()} query={table.query} onQueryChange={table.onQueryChange} totalRows={providers.totalRows} searchable ariaLabel={t("credentials.providerStatus")} empty={{ title: t("credentials.noProviderData"), description: t("credentials.noProviderDataDesc"), filtered: Boolean(table.query.search) }} /></CardContent></Card><Card><CardHeader><div className="flex flex-wrap items-center justify-between gap-3"><div><CardTitle className="text-base">{t("credentials.recentList")}</CardTitle><CardDescription>{t("credentials.recentListDesc")}</CardDescription></div><label className="text-sm">{t("common.status")} <select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)} className="ml-2 h-9 rounded-md border bg-background px-2"><option value="all">{t("admin.phase5.all")}</option><option value="active">{t("credentials.active")}</option><option value="revoked">{t("credentials.revoked")}</option><option value="expired">{t("credentials.expired")}</option></select></label></div></CardHeader><CardContent><DataTable rows={recent.rows} columns={recentColumns} rowKey={(row) => row.id} loading={loading} error={error} onRetry={() => void refresh()} query={table.query} onQueryChange={table.onQueryChange} totalRows={recent.totalRows} searchable ariaLabel={t("credentials.recentList")} empty={{ title: t("credentials.noRecent"), description: t("admin.phase5.filteredEmpty"), filtered: Boolean(table.query.search) || statusFilter !== "all" }} /></CardContent></Card><p className="text-xs text-muted-foreground">{t("admin.phase5.credentials.redactionNote")}</p></div>;
}

export function SecretsFeature() {
  useAuthRedirect();
  const { t } = useI18n();
  const table = useTableQuery();
  const [status, setStatus] = React.useState<SecretsStatus>();
  const [guide, setGuide] = React.useState<RotationGuide>();
  const [checked, setChecked] = React.useState<Record<string, boolean>>({});
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<AdminError>();
  const refresh = React.useCallback(async () => { setLoading(true); try { const [nextStatus, nextGuide] = await Promise.all([getSecretsStatus(), getRotationGuide()]); setStatus(nextStatus); setGuide(nextGuide); setError(undefined); } catch (cause) { setError(normalizeAdminError(cause)); } finally { setLoading(false); } }, []);
  React.useEffect(() => { if (getToken()) void refresh(); }, [refresh]);
  const result = React.useMemo(() => applyClientListQuery(status?.items ?? [], table.query, [(row) => row.name, (row) => row.reason], { name: (row) => row.name, configured: (row) => row.configured, rotation: (row) => row.rotation_needed }), [status?.items, table.query]);
  const columns = React.useMemo<ColumnDef<SecretStatusItem>[]>(() => [{ id: "name", header: t("admin.phase5.secrets.reference"), accessor: (row) => row.name, sortable: true }, { id: "configured", header: t("common.status"), sortable: true, cell: (row) => <StatusBadge status={row.configured ? (row.rotation_needed ? "warning" : "healthy") : "failed"} label={row.configured ? (row.rotation_needed ? t("secrets.rotationNeeded") : t("secrets.configured")) : t("secrets.notConfigured")} /> }, { id: "rotation", header: t("secrets.rotationNeeded"), sortable: true, cell: (row) => row.rotation_needed ? t("admin.phase5.yes") : t("admin.phase5.no") }, { id: "reason", header: t("admin.phase5.secrets.guidance"), accessor: (row) => row.reason }], [t]);
  return <div className="space-y-6"><header className="flex flex-wrap items-start justify-between gap-3"><div><h1 className="flex items-center gap-2 text-2xl font-semibold"><Lock aria-hidden="true" className="h-6 w-6" />{t("secrets.title")}</h1><p className="mt-1 text-sm text-muted-foreground">{t("secrets.subtitle")}</p></div><Button variant="outline" onClick={() => void refresh()}><RefreshCw aria-hidden="true" className="h-4 w-4" />{t("common.refresh")}</Button></header><DataTable rows={result.rows} columns={columns} rowKey={(row) => row.name} loading={loading} error={error} onRetry={() => void refresh()} query={table.query} onQueryChange={table.onQueryChange} totalRows={result.totalRows} searchable ariaLabel={t("secrets.title")} empty={{ title: t("admin.phase5.secrets.empty"), description: t("secrets.noExecution"), filtered: Boolean(table.query.search) }} />{guide ? <Card><CardHeader><CardTitle>{t("secrets.guide")}</CardTitle><CardDescription>{guide.overview}</CardDescription></CardHeader><CardContent className="space-y-4"><ol className="list-decimal space-y-2 pl-5">{guide.steps.map((step) => <li key={step.order}><span className="font-medium">{step.title}</span><p className="text-sm text-muted-foreground">{step.detail}</p></li>)}</ol><fieldset><legend className="font-medium">{t("secrets.checklist")}</legend><div className="mt-2 space-y-2">{guide.checklist.map((item) => <label key={item.id} className="flex items-center gap-2"><input type="checkbox" checked={Boolean(checked[item.id])} onChange={() => setChecked((current) => ({ ...current, [item.id]: !current[item.id] }))} />{item.label}</label>)}</div></fieldset><p className="text-xs text-muted-foreground">{t("secrets.noExecution")}</p></CardContent></Card> : null}</div>;
}

interface FlagHistory { id: string; name: string; enabled: boolean; actor: string; changedAt: string }

export function FeatureFlagsFeature() {
  useAuthRedirect();
  const { t, lang } = useI18n();
  const { toast } = useToast();
  const table = useTableQuery();
  const [data, setData] = React.useState<FeatureFlagsResponse>();
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<AdminError>();
  const [pending, setPending] = React.useState<FeatureFlagRecord>();
  const [history, setHistory] = React.useState<FlagHistory[]>([]);
  const refresh = React.useCallback(async () => { setLoading(true); try { setData(await getFeatureFlags()); setError(undefined); } catch (cause) { setError(normalizeAdminError(cause)); } finally { setLoading(false); } }, []);
  React.useEffect(() => { if (getToken()) void refresh(); }, [refresh]);
  const result = React.useMemo(() => applyClientListQuery(data?.flags ?? [], table.query, [(row) => row.name, (row) => row.description], { name: (row) => row.name, enabled: (row) => row.enabled, default: (row) => row.default }), [data?.flags, table.query]);
  const columns = React.useMemo<ColumnDef<FeatureFlagRecord>[]>(() => [{ id: "name", header: t("admin.phase5.flags.name"), sortable: true, cell: (row) => <div><span className="font-mono text-xs">{row.name}</span><p className="text-xs text-muted-foreground">{row.description}</p></div> }, { id: "enabled", header: t("common.status"), sortable: true, cell: (row) => <StatusBadge status={row.enabled ? "healthy" : "unknown"} label={row.enabled ? t("featureFlags.enabled") : t("featureFlags.disabled")} /> }, { id: "default", header: t("featureFlags.default"), sortable: true, accessor: (row) => String(row.default) }, { id: "metadata", header: t("admin.phase5.flags.source"), cell: (row) => [row.overridden ? t("featureFlags.overridden") : null, row.custom ? t("featureFlags.custom") : null].filter(Boolean).join(", ") || "—" }], [t]);
  async function confirmToggle() { if (!pending) return; const resultFlag = await toggleFeatureFlag(pending.name, !pending.enabled); setHistory((current) => [{ id: `${pending.name}-${Date.now()}`, name: pending.name, enabled: resultFlag.enabled, actor: "current-admin", changedAt: new Date().toISOString() }, ...current]); setPending(undefined); await refresh(); toast({ title: t("featureFlags.saved"), variant: "success" }); }
  return <div className="space-y-6"><header><h1 className="flex items-center gap-2 text-2xl font-semibold"><Flag aria-hidden="true" className="h-6 w-6" />{t("featureFlags.title")}</h1><p className="mt-1 text-sm text-muted-foreground">{t("featureFlags.subtitle")}</p></header><DataTable rows={result.rows} columns={columns} rowKey={(row) => row.name} loading={loading} error={error} onRetry={() => void refresh()} query={table.query} onQueryChange={table.onQueryChange} totalRows={result.totalRows} searchable ariaLabel={t("featureFlags.title")} empty={{ title: t("admin.phase5.flags.empty"), description: t("admin.phase5.filteredEmpty"), filtered: Boolean(table.query.search) }} rowActions={(row) => [{ id: "toggle", label: row.enabled ? t("featureFlags.disabled") : t("featureFlags.enabled"), onSelect: () => setPending(row) }]} />{history.length ? <Card><CardHeader><CardTitle className="text-base">{t("admin.phase5.flags.history")}</CardTitle></CardHeader><CardContent className="space-y-2">{history.map((item) => <div key={item.id} className="flex flex-wrap justify-between gap-2 border-b py-2 text-sm"><span className="font-mono">{item.name}</span><StatusBadge status={item.enabled ? "healthy" : "unknown"} label={item.enabled ? t("featureFlags.enabled") : t("featureFlags.disabled")} /><span>{item.actor}</span><span className="text-muted-foreground">{formatTime(item.changedAt, lang)}</span></div>)}</CardContent></Card> : null}<ConfirmDialog open={Boolean(pending)} onOpenChange={(open) => { if (!open) setPending(undefined); }} title={t("admin.phase5.flags.confirmTitle")} description={t("admin.phase5.flags.confirmDescription")} targetLabel={pending?.name} consequence={data?.runtime_wired === false ? t("featureFlags.subtitle") : undefined} confirmLabel={pending?.enabled ? t("featureFlags.disabled") : t("featureFlags.enabled")} onConfirm={confirmToggle} /></div>;
}

export function ProfileOperationsFeature() {
  useAuthRedirect();
  const { t } = useI18n();
  const { toast } = useToast();
  const [tenant, setTenant] = React.useState("default");
  const [userId, setUserId] = React.useState("");
  const [reason, setReason] = React.useState("");
  const [status, setStatus] = React.useState<ProfileOpsStatus>();
  const [result, setResult] = React.useState<ProfileBackfillResult | ProfileResetResult>();
  const [loading, setLoading] = React.useState(true);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string>();
  const [resetOpen, setResetOpen] = React.useState(false);
  const refresh = React.useCallback(async () => { setLoading(true); try { setStatus(await getProfileOpsStatus(tenant.trim() || undefined, userId.trim() || undefined)); setError(undefined); } catch (cause) { setError(cause instanceof Error ? cause.message : t("profileOps.loadFailed")); } finally { setLoading(false); } }, [t, tenant, userId]);
  // Filters are intentionally applied only after the explicit refresh action.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  React.useEffect(() => { if (getToken()) void refresh(); }, []);
  async function backfill() { setBusy(true); try { const next = await postProfileBackfill({ tenant_id: tenant.trim() || "default", user_id: userId.trim() || "default", reason: reason.trim() || undefined }); setResult(next); await refresh(); toast({ title: t("profileOps.backfilled"), description: next.job_id, variant: "success" }); } catch (cause) { setError(cause instanceof Error ? cause.message : t("profileOps.actionFailed")); toast({ title: t("profileOps.actionFailed"), variant: "error" }); } finally { setBusy(false); } }
  async function reset() { setBusy(true); try { const next = await postProfileReset({ tenant_id: tenant.trim() || "default", user_id: userId.trim() || "default", confirm: "RESET" }); setResult(next); await refresh(); toast({ title: t("profileOps.resetDone"), variant: "success" }); } catch (cause) { setError(cause instanceof Error ? cause.message : t("profileOps.actionFailed")); toast({ title: t("profileOps.actionFailed"), variant: "error" }); throw cause; } finally { setBusy(false); } }
  return <div className="space-y-6"><header><h1 className="flex items-center gap-2 text-2xl font-semibold"><UserCog aria-hidden="true" className="h-6 w-6" />{t("profileOps.title")}</h1><p className="mt-1 text-sm text-muted-foreground">{t("profileOps.subtitle")}</p></header>{error ? <ErrorState compact title={t("profileOps.actionFailed")} description={error} retry={() => void refresh()} /> : null}<Card><CardHeader><CardTitle className="text-base">{t("profileOps.profiles")}</CardTitle></CardHeader><CardContent className="space-y-4">{loading ? <Skeleton variant="form" rows={4} ariaLabel={t("admin.loading.content")} /> : <><div className="grid gap-4 md:grid-cols-2"><FormField id="profile-tenant" label={t("profileOps.tenant")}><Input value={tenant} onChange={(event) => setTenant(event.target.value)} /></FormField><FormField id="profile-user" label={t("profileOps.user")}><Input value={userId} onChange={(event) => setUserId(event.target.value)} /></FormField></div><div className="flex flex-wrap gap-2"><StatusBadge status={status?.profile_exists ? "healthy" : "unknown"} label={status?.profile_exists ? t("profileOps.profileExists") : t("admin.phase5.profile.noProfile")} /><span className="rounded-md border px-2 py-1 text-sm">{t("profileOps.profiles")}: {status?.profile_count ?? 0}</span><span className="rounded-md border px-2 py-1 text-sm">{t("profileOps.traits")}: {status?.trait_count ?? 0}</span><span className="rounded-md border px-2 py-1 text-sm">{t("profileOps.evidence")}: {status?.evidence_count ?? 0}</span></div><div className="flex flex-wrap items-end gap-3"><div className="min-w-64 flex-1"><FormField id="profile-reason" label={t("profileOps.backfillReason")}><Input value={reason} onChange={(event) => setReason(event.target.value)} /></FormField></div><Button onClick={() => void backfill()} disabled={busy}>{t("profileOps.backfill")}</Button><Button variant="destructive" onClick={() => setResetOpen(true)} disabled={busy}>{t("profileOps.reset")}</Button><Button variant="outline" onClick={() => void refresh()}>{t("common.refresh")}</Button></div></>}</CardContent></Card>{result ? <Card><CardHeader><CardTitle className="text-base">{t("admin.phase5.profile.lastResult")}</CardTitle></CardHeader><CardContent><dl className="grid gap-2 text-sm sm:grid-cols-2">{"job_id" in result ? <><dt>{t("common.id")}</dt><dd className="font-mono">{result.job_id}</dd></> : null}<dt>{t("profileOps.via")}</dt><dd>{result.via ?? "—"}</dd></dl><Link href="/control/audit?resource=profile" className="mt-4 inline-block text-sm font-medium underline">{t("admin.phase5.profile.auditLink")}</Link></CardContent></Card> : null}<ConfirmDialog open={resetOpen} onOpenChange={setResetOpen} title={t("profileOps.reset")} description={t("admin.phase5.profile.resetDescription")} targetLabel={`${tenant.trim() || "default"} / ${userId.trim() || "default"}`} consequence={t("admin.phase5.profile.resetConsequence")} confirmLabel={t("profileOps.reset")} tone="danger" requireText="RESET" pending={busy} onConfirm={reset} /></div>;
}
