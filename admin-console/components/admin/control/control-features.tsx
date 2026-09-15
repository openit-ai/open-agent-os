"use client";

/**
 * Admin control group features (policy / approvals / audit).
 *
 * Screen bodies live here so the route files stay thin, matching the
 * management / operations / knowledge groups. Each feature reads its own data
 * and query state; the route supplies the Suspense boundary because
 * `useTableQuery` calls `useSearchParams()`, which Next.js 15 requires under
 * one during prerendering.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { AlertTriangle, CheckCircle2, ClipboardCheck, FileEdit, FlaskConical, History, RefreshCw, RotateCcw, ScrollText, Shield, ShieldCheck, Upload } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { ConfirmDialog, DataTable, ErrorState, FormField, useBackgroundQueryToast, useTableQuery, useToast, useUrlFilter } from "@/components/admin";
import { applyClientListQuery } from "@/lib/admin-api/list-query";
import { normalizeAdminError } from "@/lib/admin-api/client";
import { adminKeys } from "@/lib/admin-api/keys";
import { useI18n } from "@/lib/i18n";
import {
  approvePolicy,
  decideApproval,
  getAuditCheckpoint,
  getAuditEvents,
  getPendingApprovals,
  getPolicyBundles,
  getPolicyDraft,
  getPolicyHistory,
  getToken,
  publishPolicy,
  rollbackPolicy,
  simulatePolicy,
  upsertPolicyDraft,
  validatePolicy,
  verifyAuditChain,
  type ApprovalDecisionType,
  type ApprovalRequestItem,
  type PolicyBundle,
  type PolicyDraftBundle,
  type PolicyRule,
} from "@/lib/api";


const EVALUATION_ORDER_FALLBACK = [
  "explicit_deny",
  "security_boundary_deny",
  "personal_delegation",
  "persistent_user_grant",
  "group_grant",
  "default_bundle",
  "jit_approval",
  "default_deny",
];

const SOURCE_OPTIONS = [
  "explicit_deny",
  "security_boundary_deny",
  "personal_delegation",
  "persistent_user_grant",
  "group_grant",
  "default_bundle",
  "jit_approval",
  "default_deny",
];

const EFFECT_OPTIONS: PolicyRule["effect"][] = ["ALLOW", "DENY", "APPROVAL_REQUIRED"];

/** Evaluation-order sources are translated at render time via `admin.policy.source.*`. */
const SOURCE_KEYS: Record<string, string> = {
  explicit_deny: "explicit_deny",
  security_boundary_deny: "security_boundary_deny",
  personal_delegation: "personal_delegation",
  persistent_user_grant: "persistent_user_grant",
  group_grant: "group_grant",
  default_bundle: "default_bundle",
  jit_approval: "jit_approval",
  default_deny: "default_deny",
};

function decisionVariant(d: string) {
  if (d === "DENY") return "danger" as const;
  if (d === "ALLOW") return "success" as const;
  if (d === "APPROVAL_REQUIRED") return "warning" as const;
  return "secondary" as const;
}

function orderIndex(source: string, order: string[]) {
  const idx = order.indexOf(source);
  return idx >= 0 ? idx + 1 : 99;
}

function newEmptyRule(): PolicyRule {
  return { id: "", source: "default_bundle", action: "*", resource_pattern: "*", effect: "ALLOW", priority: 100 };
}

export function PolicyFeature() {
  const router = useRouter();
  const { t } = useI18n();
  const table = useTableQuery();
  const { toast } = useToast();
  const [activeTab, setActiveTab] = useState("bundles");
  const [bundles, setBundles] = useState<PolicyBundle[]>([]);
  const [evalOrder, setEvalOrder] = useState<string[]>(EVALUATION_ORDER_FALLBACK);
  const [draft, setDraft] = useState<PolicyDraftBundle | null>(null);
  const [history, setHistory] = useState<PolicyDraftBundle[]>([]);
  const [activeVersion, setActiveVersion] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [actionMsg, setActionMsg] = useState<string | null>(null);

  // Draft editing
  const [editRules, setEditRules] = useState<PolicyRule[]>([]);
  const [editName, setEditName] = useState("Default Policy Bundle");
  const [editBundleId, setEditBundleId] = useState("default-bundle-v1");
  const [allowRemoveMandatory, setAllowRemoveMandatory] = useState(false);
  const [jsonDraft, setJsonDraft] = useState("");
  const [validateResult, setValidateResult] = useState<{ ok: boolean; errors: string[] } | null>(null);
  const [saving, setSaving] = useState(false);

  // Simulate
  const [simAction, setSimAction] = useState("read");
  const [simResource, setSimResource] = useState("doc:public/*");
  const [simUseDraft, setSimUseDraft] = useState(true);
  const [simResult, setSimResult] = useState<{ decision: string; source: string; reason: string; matched_rule: PolicyRule | null } | null>(null);
  const [simLoading, setSimLoading] = useState(false);
  const [simError, setSimError] = useState<string | null>(null);
  const [confirmAction, setConfirmAction] = useState<{ kind: "publish" | "rollback"; version?: string } | null>(null);

  const fetchAll = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [bRes, dRes, hRes] = await Promise.all([getPolicyBundles(), getPolicyDraft(), getPolicyHistory()]);
      setBundles(bRes.bundles ?? []);
      if (bRes.evaluation_order?.length) setEvalOrder(bRes.evaluation_order);
      setDraft(dRes.draft ?? null);
      setHistory(hRes.items ?? []);
      setActiveVersion(hRes.active_version ?? bRes.active_version ?? null);
      // init edit from draft if exists else from published bundle
      const src = dRes.draft?.rules ?? bRes.bundles?.[0]?.rules ?? [];
      if (editRules.length === 0 && src.length > 0) {
        // only init once to avoid overwriting user edits on refresh
        setEditRules(src as PolicyRule[]);
        setJsonDraft(JSON.stringify(src, null, 2));
      }
      if (dRes.draft) {
        setEditName(dRes.draft.name);
        setEditBundleId(dRes.draft.id);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : t("common.fetchFailed"));
    } finally {
      setLoading(false);
    }
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!getToken()) {
      router.replace("/login");
      return;
    }
    fetchAll();
  }, [fetchAll, router]);

  // Keep jsonDraft synced when editRules changes via table edits
  function syncJson(rules: PolicyRule[]) {
    setEditRules(rules);
    try { setJsonDraft(JSON.stringify(rules, null, 2)); } catch { /* ignore */ }
  }

  async function handleValidate() {
    setValidateResult(null);
    setActionMsg(null);
    // parse json if user edited raw
    let rules: PolicyRule[] = editRules;
    if (jsonDraft.trim()) {
      try {
        const parsed = JSON.parse(jsonDraft);
        if (Array.isArray(parsed)) rules = parsed;
      } catch (e) {
        setValidateResult({ ok: false, errors: [e instanceof Error ? e.message : t("admin.policy.draft.invalidJson")] });
        return;
      }
    }
    try {
      const res = await validatePolicy(rules, allowRemoveMandatory);
      setValidateResult({ ok: res.ok ?? res.valid, errors: res.errors ?? [] });
      // keep table in sync
      setEditRules(rules);
    } catch (e) {
      setValidateResult({ ok: false, errors: [e instanceof Error ? e.message : String(e)] });
    }
  }

  async function handleSaveDraft() {
    setSaving(true);
    setActionMsg(null);
    setValidateResult(null);
    try {
      let rules: PolicyRule[] = editRules;
      if (jsonDraft.trim()) {
        try { const p = JSON.parse(jsonDraft); if (Array.isArray(p)) rules = p; } catch (e) { throw new Error(e instanceof Error ? e.message : t("admin.policy.draft.invalidJson")); }
      }
      if (!rules.length) throw new Error(t("admin.policy.draft.emptyRules"));
      const res = await upsertPolicyDraft({ rules, name: editName, bundle_id: editBundleId, allow_remove_mandatory: allowRemoveMandatory });
      setDraft(res.draft);
      setActionMsg(t("admin.policy.draft.saved", { status: res.draft.status, version: res.draft.version }));
      await fetchAll();
      setActiveTab("bundles");
    } catch (e) {
      setActionMsg(e instanceof Error ? e.message : String(e));
    } finally { setSaving(false); }
  }

  async function handleSimulate() {
    setSimLoading(true); setSimError(null); setSimResult(null);
    try {
      const res = await simulatePolicy({ action: simAction, resource: simResource, use_draft: simUseDraft });
      setSimResult(res.result);
    } catch (e) { setSimError(e instanceof Error ? e.message : String(e)); }
    finally { setSimLoading(false); }
  }

  async function handleApprove() {
    setActionMsg(null);
    try { const r = await approvePolicy("default"); setActionMsg(t("admin.policy.draft.approved", { status: r.status })); await fetchAll(); }
    catch (e) { setActionMsg(e instanceof Error ? e.message : String(e)); }
  }

  async function handlePublish() {
    setActionMsg(null);
    try { const r = await publishPolicy("default"); setActionMsg(t("admin.policy.draft.published", { version: r.active_version })); toast({ title: t("admin.policy.draft.published", { version: r.active_version }), variant: "success" }); await fetchAll(); }
    catch (e) { setActionMsg(e instanceof Error ? e.message : String(e)); toast({ title: t("admin.policy.draft.publishFailed"), description: e instanceof Error ? e.message : undefined, variant: "error" }); throw e; }
  }

  async function handleRollback(v: string) {
    setActionMsg(null);
    try { const r = await rollbackPolicy(v, "default"); setActionMsg(t("admin.policy.draft.rolledBack", { version: v, active: r.active_version })); toast({ title: t("admin.policy.draft.rolledBack", { version: v, active: r.active_version }), variant: "success" }); await fetchAll(); }
    catch (e) { setActionMsg(e instanceof Error ? e.message : String(e)); toast({ title: t("admin.policy.draft.rollbackFailed"), description: e instanceof Error ? e.message : undefined, variant: "error" }); throw e; }
  }

  // Policy history has no list query parameters; preserve the API and paginate the immutable client snapshot.
  const visibleHistory = useMemo(() => applyClientListQuery(
    history,
    table.query,
    [(row) => row.version, (row) => row.name, (row) => row.created_by, (row) => row.status],
    { version: (row) => row.version, status: (row) => row.status, created: (row) => row.created_at ?? "", actor: (row) => row.created_by ?? "" },
  ), [history, table.query]);

  return (
    <div className="mx-auto w-full max-w-[1200px] space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-semibold">
            <Shield aria-hidden="true" className="h-6 w-6" />
            {t("admin.policy.title")}
          </h1>
          <p className="mt-1 text-sm text-muted-foreground">{t("admin.policy.subtitle")}</p>
        </div>
        <Button variant="outline" size="sm" onClick={fetchAll} disabled={loading}>
          <RefreshCw aria-hidden="true" className={`mr-1 h-4 w-4 ${loading ? "animate-spin" : ""}`} />
          {t("common.refresh")}
        </Button>
      </div>

      {error && <p className="rounded-md bg-[#DC2626]/10 p-3 text-sm text-[#DC2626]" role="alert">{error}</p>}
      {actionMsg && <p className="rounded-md border bg-card p-3 text-sm" role="status">{actionMsg}</p>}
      {activeVersion && <p className="text-xs text-muted-foreground">{t("admin.policy.active.publishedVersion")} <span className="font-mono font-medium text-foreground">{activeVersion}</span>{draft ? <> · {t("admin.policy.active.draftSuffix", { status: draft.status, version: draft.version })}</> : ` · ${t("admin.policy.active.noDraft")}`}</p>}

      <Tabs value={activeTab} onValueChange={setActiveTab}>
        <TabsList>
          <TabsTrigger value="bundles">{t("admin.policy.tab.published")}</TabsTrigger>
          <TabsTrigger value="draft"><FileEdit className="mr-1 h-3.5 w-3.5" />{t("admin.policy.tab.draft")}</TabsTrigger>
          <TabsTrigger value="simulate"><FlaskConical className="mr-1 h-3.5 w-3.5" />{t("admin.policy.tab.simulate")}</TabsTrigger>
          <TabsTrigger value="history"><History className="mr-1 h-3.5 w-3.5" />{t("admin.policy.tab.history")}</TabsTrigger>
        </TabsList>

        <TabsContent value="bundles">
          {/* Section 25 fixed order */}
          <Card className="mb-4">
            <CardHeader>
              <CardTitle className="text-base">{t("policy.section25Title")}</CardTitle>
              <CardDescription>{t("policy.section25Desc")}</CardDescription>
            </CardHeader>
            <CardContent className="space-y-3">
              <div className="flex flex-wrap gap-2">
                {evalOrder.map((src, idx) => {
                  const isExplicitDeny = src === "explicit_deny";
                  const isPersonal = src === "personal_delegation";
                  return (
                    <div key={src} className={`flex items-center gap-1.5 rounded-md border px-2.5 py-1 text-xs font-medium ${isExplicitDeny ? "border-[#DC2626] bg-[#DC2626] text-white" : isPersonal ? "border-[#22C55E] bg-[#22C55E]/10 text-[#16A34A]" : "bg-muted"}`}>
                      <span className="flex h-5 w-5 items-center justify-center rounded-full bg-background text-[11px] font-bold text-foreground">{idx + 1}</span>
                      {SOURCE_KEYS[src] ? t(`admin.policy.source.${SOURCE_KEYS[src]}`) : src}
                    </div>
                  );
                })}
              </div>
              <div className="flex flex-wrap gap-2 pt-1">
                <Badge variant="danger" className="gap-1"><AlertTriangle className="h-3 w-3" />{t("policy.explicitDenyOverride")}</Badge>
                <Badge variant="success" className="gap-1">{t("policy.personalDelegationNote")}</Badge>
              </div>
              <p className="text-xs text-muted-foreground">{t("policy.sortingNote")}</p>
            </CardContent>
          </Card>

          {loading ? (
            <Card><CardContent className="pt-6 text-center text-sm text-muted-foreground">{t("policy.loading")}</CardContent></Card>
          ) : bundles.length === 0 ? (
            <Card><CardContent className="pt-6 text-center text-sm text-muted-foreground">{t("admin.policy.published.missing")}</CardContent></Card>
          ) : (
            bundles.map((bundle) => (
              <Card key={bundle.id} className="overflow-hidden">
                <CardHeader className="pb-3">
                  <CardTitle className="flex flex-wrap items-center gap-2 text-base">{bundle.name}<Badge variant="outline">{bundle.id}</Badge><Badge variant="secondary">v{bundle.version}</Badge>{bundle.status && <Badge variant={bundle.status === "published" ? "success" : "secondary"}>{bundle.status}</Badge>}</CardTitle>
                  <CardDescription className="flex flex-wrap gap-2"><span>{t("policy.tenant")} <span className="font-mono font-medium text-foreground">{bundle.tenant_id}</span></span><span>·</span><span>{t("policy.bundleRules", { count: String(bundle.rules?.length ?? 0) })}</span></CardDescription>
                </CardHeader>
                <CardContent className="p-0">
                  <div className="w-full overflow-auto">
                    <Table>
                      <TableHeader><TableRow><TableHead className="whitespace-nowrap">{t("policy.colOrder")}</TableHead><TableHead>{t("policy.colSource")}</TableHead><TableHead>{t("policy.colAction")}</TableHead><TableHead>{t("policy.colResource")}</TableHead><TableHead>{t("policy.colDecision")}</TableHead><TableHead>{t("policy.colPriority")}</TableHead></TableRow></TableHeader>
                      <TableBody>
                        {[...(bundle.rules ?? [])].sort((a, b) => { const ao = orderIndex(a.source, evalOrder); const bo = orderIndex(b.source, evalOrder); if (ao !== bo) return ao - bo; if (a.priority !== b.priority) return a.priority - b.priority; return a.id.localeCompare(b.id); }).map((rule: PolicyRule) => {
                          const isExplicitDeny = rule.source === "explicit_deny";
                          const isPersonal = rule.source === "personal_delegation";
                          return (
                            <TableRow key={rule.id} className={isExplicitDeny ? "bg-[#DC2626]/10 hover:bg-[#DC2626]/15" : isPersonal ? "bg-[#22C55E]/5" : ""}>
                              <TableCell className="whitespace-nowrap text-xs font-medium">{orderIndex(rule.source, evalOrder)}<span className="ml-1 text-muted-foreground">· {rule.id}</span></TableCell>
                              <TableCell><Badge variant={isExplicitDeny ? "danger" : isPersonal ? "success" : "secondary"} className="whitespace-nowrap">{SOURCE_KEYS[rule.source] ? t(`admin.policy.source.${SOURCE_KEYS[rule.source]}`) : rule.source}</Badge></TableCell>
                              <TableCell className="font-mono text-xs">{rule.action}</TableCell>
                              <TableCell className="font-mono text-xs">{rule.resource_pattern}</TableCell>
                              <TableCell><Badge variant={decisionVariant(rule.effect)}>{rule.effect}</Badge></TableCell>
                              <TableCell className="text-xs">{rule.priority ?? 0}</TableCell>
                            </TableRow>
                          );
                        })}
                      </TableBody>
                    </Table>
                  </div>
                  <div className="border-t bg-muted/20 p-3"><p className="text-xs text-muted-foreground">{t("policy.explicitDenyHint")}</p></div>
                </CardContent>
              </Card>
            ))
          )}
          {draft && (
            <Card className="mt-4 border-dashed">
              <CardHeader className="pb-2"><CardTitle className="text-sm flex items-center gap-2"><FileEdit className="h-4 w-4" />{t("admin.policy.draft.sectionTitle", { status: draft.status })} <Badge variant={draft.status === "approved" ? "success" : "warning"}>{draft.status}</Badge><span className="font-mono text-xs text-muted-foreground">{draft.version}</span></CardTitle>
                <CardDescription>{t("admin.policy.draft.createdBy", { actor: draft.created_by ?? "-" })} {draft.created_at ? new Date(draft.created_at).toLocaleString() : ""}{draft.approved_by ? ` · ${t("admin.policy.draft.approvedBy", { actor: draft.approved_by })}` : ""}</CardDescription>
              </CardHeader>
              <CardContent className="flex flex-wrap gap-2">
                <Button size="sm" variant="outline" onClick={handleValidate}><CheckCircle2 className="h-4 w-4" />{t("admin.policy.draft.validate")}</Button>
                <Button size="sm" variant="secondary" onClick={handleApprove}>{t("admin.policy.draft.approve")}</Button>
                <Button size="sm" onClick={() => setConfirmAction({ kind: "publish" })}><Upload className="h-4 w-4" />{t("admin.policy.draft.publish")}</Button>
                <Button size="sm" variant="outline" onClick={() => setActiveTab("draft")}>{t("admin.policy.draft.editDraft")}</Button>
              </CardContent>
            </Card>
          )}
          <p className="mt-3 text-xs text-muted-foreground">{t("policy.dataNote")}</p>
        </TabsContent>

        <TabsContent value="draft">
          <Card>
            <CardHeader><CardTitle className="text-base">{t("admin.policy.draft.title")}</CardTitle><CardDescription>{t("admin.policy.draft.description")}</CardDescription></CardHeader>
            <CardContent className="space-y-4">
              <div className="grid gap-3 sm:grid-cols-3">
                <div className="space-y-1"><Label>{t("admin.policy.draft.bundleId")}</Label><Input value={editBundleId} onChange={(e) => setEditBundleId(e.target.value)} placeholder="default-bundle-v1" /></div>
                <div className="space-y-1"><Label>{t("admin.policy.draft.name")}</Label><Input value={editName} onChange={(e) => setEditName(e.target.value)} placeholder="Default Policy Bundle" /></div>
                <div className="flex items-end gap-2">
                  <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={allowRemoveMandatory} onChange={(e) => setAllowRemoveMandatory(e.target.checked)} /> {t("admin.policy.draft.allowRemoveMandatory")}</label>
                </div>
              </div>

              <div className="space-y-2">
                <div className="flex items-center justify-between">
                  <Label>{t("admin.policy.draft.rulesTable")}</Label>
                  <Button size="sm" variant="outline" onClick={() => syncJson([...editRules, { ...newEmptyRule(), id: `rule-${Date.now()}` }])}>{t("admin.policy.draft.addRule")}</Button>
                </div>
                <div className="overflow-auto rounded-md border">
                  <Table>
                    <TableHeader><TableRow><TableHead>{t("admin.policy.col.id")}</TableHead><TableHead>{t("policy.colSource")}</TableHead><TableHead>{t("admin.policy.col.action")}</TableHead><TableHead>{t("admin.policy.col.resourcePattern")}</TableHead><TableHead>{t("admin.policy.col.effect")}</TableHead><TableHead>{t("policy.colPriority")}</TableHead><TableHead></TableHead></TableRow></TableHeader>
                    <TableBody>
                      {editRules.length === 0 ? <TableRow><TableCell colSpan={7} className="text-center text-sm text-muted-foreground">{t("admin.policy.draft.noRules")}</TableCell></TableRow> : editRules.map((r, idx) => (
                        <TableRow key={idx}>
                          <TableCell><Input value={r.id} onChange={(e) => { const c = [...editRules]; c[idx] = { ...c[idx], id: e.target.value }; setEditRules(c); }} className="h-7 min-w-[120px] font-mono text-xs" placeholder="deny-external-export" /></TableCell>
                          <TableCell><select value={r.source} onChange={(e) => { const c = [...editRules]; c[idx] = { ...c[idx], source: e.target.value }; setEditRules(c); }} className="h-7 rounded-md border bg-background px-2 text-xs">{SOURCE_OPTIONS.map((o) => <option key={o} value={o}>{o}</option>)}</select></TableCell>
                          <TableCell><Input value={r.action} onChange={(e) => { const c = [...editRules]; c[idx] = { ...c[idx], action: e.target.value }; setEditRules(c); }} className="h-7 min-w-[80px] font-mono text-xs" placeholder="*" /></TableCell>
                          <TableCell><Input value={r.resource_pattern} onChange={(e) => { const c = [...editRules]; c[idx] = { ...c[idx], resource_pattern: e.target.value }; setEditRules(c); }} className="h-7 min-w-[120px] font-mono text-xs" placeholder="*" /></TableCell>
                          <TableCell><select value={r.effect} onChange={(e) => { const c = [...editRules]; c[idx] = { ...c[idx], effect: e.target.value as PolicyRule["effect"] }; setEditRules(c); }} className="h-7 rounded-md border bg-background px-2 text-xs">{EFFECT_OPTIONS.map((o) => <option key={o} value={o}>{o}</option>)}</select></TableCell>
                          <TableCell><Input type="number" value={String(r.priority ?? 0)} onChange={(e) => { const c = [...editRules]; c[idx] = { ...c[idx], priority: Number(e.target.value) }; setEditRules(c); }} className="h-7 w-20 text-xs" /></TableCell>
                          <TableCell><Button variant="ghost" size="sm" onClick={() => syncJson(editRules.filter((_, i) => i !== idx))}>Remove</Button></TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </div>
              </div>

              <div className="space-y-1">
                <Label>{t("admin.policy.draft.rawJson")}</Label>
                <textarea value={jsonDraft} onChange={(e) => setJsonDraft(e.target.value)} rows={8} className="w-full rounded-md border bg-muted/20 p-3 font-mono text-xs" placeholder='[{"id":"deny-external-export","source":"explicit_deny","action":"*","resource_pattern":"external:*","effect":"DENY","priority":10}]' />
                <div className="flex gap-2">
                  <Button size="sm" variant="outline" onClick={() => { try { const p = JSON.parse(jsonDraft); if (Array.isArray(p)) setEditRules(p as PolicyRule[]); setActionMsg(t("admin.policy.draft.loadedFromJson")); } catch (e) { setActionMsg(e instanceof Error ? e.message : String(e)); } }}>{t("admin.policy.draft.loadJson")}</Button>
                  <Button size="sm" variant="outline" onClick={() => setJsonDraft(JSON.stringify(editRules, null, 2))}>{t("admin.policy.draft.tableToJson")}</Button>
                </div>
              </div>

              <div className="flex flex-wrap gap-2">
                <Button variant="outline" onClick={handleValidate}><CheckCircle2 className="h-4 w-4" />{t("admin.policy.draft.validate")}</Button>
                <Button onClick={handleSaveDraft} disabled={saving}>{saving ? t("admin.policy.draft.saving") : t("admin.policy.draft.saveDraft")}</Button>
                <Button variant="secondary" onClick={handleApprove}>{t("admin.policy.draft.approve")}</Button>
                <Button onClick={() => setConfirmAction({ kind: "publish" })}><Upload className="h-4 w-4" />{t("admin.policy.draft.publish")}</Button>
              </div>

              {validateResult && (
                <div className={`rounded-md border p-3 text-sm ${validateResult.ok ? "border-[#22C55E] bg-[#22C55E]/10" : "border-[#DC2626] bg-[#DC2626]/10"}`}>
                  <p className="font-medium">{validateResult.ok ? t("admin.policy.draft.validationPassed") : t("admin.policy.draft.validationFailed")}</p>
                  {validateResult.errors.length > 0 && <ul className="mt-1 list-disc pl-5 text-xs">{validateResult.errors.map((er, i) => <li key={i}>{er}</li>)}</ul>}
                </div>
              )}
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="simulate">
          <Card>
            <CardHeader><CardTitle className="text-base flex items-center gap-2"><FlaskConical className="h-4 w-4" />{t("admin.policy.simulate.title")}</CardTitle><CardDescription>{t("admin.policy.simulate.description")}</CardDescription></CardHeader>
            <CardContent className="space-y-4">
              <div className="grid gap-3 sm:grid-cols-3">
                <div className="space-y-1"><Label>{t("admin.policy.simulate.action")}</Label><Input value={simAction} onChange={(e) => setSimAction(e.target.value)} placeholder="read / write / external:export" /></div>
                <div className="space-y-1"><Label>{t("admin.policy.simulate.resource")}</Label><Input value={simResource} onChange={(e) => setSimResource(e.target.value)} placeholder="doc:public/* / external:share" /></div>
                <div className="flex items-end gap-2">
                  <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={simUseDraft} onChange={(e) => setSimUseDraft(e.target.checked)} /> {t("admin.policy.simulate.useDraft")}</label>
                  <Button onClick={handleSimulate} disabled={simLoading}>{simLoading ? t("admin.policy.simulate.running") : t("admin.policy.simulate.run")}</Button>
                </div>
              </div>
              {simError && <p className="text-sm text-[#DC2626]" role="alert">{simError}</p>}
              {simResult && (
                <div className="rounded-md border bg-muted/30 p-3 text-sm">
                  <div className="flex flex-wrap items-center gap-2">{t("admin.policy.simulate.decision")} <Badge variant={decisionVariant(simResult.decision)}>{simResult.decision}</Badge> {t("admin.policy.simulate.source")} <Badge variant="secondary">{simResult.source}</Badge></div>
                  <p className="mt-1 text-xs text-muted-foreground">{simResult.reason}</p>
                  {simResult.matched_rule && <pre className="mt-2 overflow-auto rounded bg-background p-2 text-xs">{JSON.stringify(simResult.matched_rule, null, 2)}</pre>}
                </div>
              )}
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="history">
          <Card>
            <CardHeader><CardTitle className="text-base flex items-center gap-2"><History className="h-4 w-4" />{t("admin.policy.history.title")}</CardTitle><CardDescription>{t("admin.policy.history.description")}</CardDescription></CardHeader>
            <CardContent>
              <DataTable
                rows={visibleHistory.rows}
                columns={[
                  { id: "version", header: t("admin.policy.col.version"), cell: (row) => <span className="font-mono">v{row.version}{row.version === activeVersion ? <Badge variant="success" className="ml-2">{t("admin.policy.history.activeBadge")}</Badge> : null}</span>, sortable: true },
                  { id: "status", header: t("admin.policy.col.status"), cell: (row) => <Badge variant={row.status === "published" ? "success" : row.status === "approved" ? "warning" : "secondary"}>{row.status}</Badge>, sortable: true },
                  { id: "bundle", header: t("admin.policy.col.bundle"), cell: (row) => <>{row.name}<span className="ml-1 font-mono text-muted-foreground">{row.id}</span></> },
                  { id: "rules", header: t("admin.policy.col.rules"), accessor: (row) => row.rules?.length ?? 0 },
                  { id: "actor", header: t("admin.policy.col.createdBy"), accessor: (row) => row.created_by, sortable: true },
                  { id: "created", header: t("admin.policy.col.createdAt"), cell: (row) => row.created_at ? new Date(row.created_at).toLocaleString() : "—", sortable: true },
                ]}
                rowKey={(row) => row.id_row ?? row.id + row.version}
                loading={loading}
                error={error ? normalizeAdminError(new Error(error)) : undefined}
                onRetry={fetchAll}
                query={table.query}
                onQueryChange={table.onQueryChange}
                totalRows={visibleHistory.totalRows}
                searchable
                rowActions={(row) => row.status === "published" ? [{ id: "rollback", label: t("admin.policy.action.rollback"), icon: RotateCcw, tone: "danger", onSelect: () => setConfirmAction({ kind: "rollback", version: row.version }) }] : []}
                empty={{ title: t("admin.policy.history.emptyTitle"), description: t("admin.policy.history.emptyDescription"), filtered: Boolean(table.query.search) }}
                ariaLabel={t("admin.policy.history.ariaLabel")}
              />
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>
      <ConfirmDialog
        open={Boolean(confirmAction)}
        title={confirmAction?.kind === "publish" ? t("admin.policy.confirm.publishTitle") : t("admin.policy.confirm.rollbackTitle")}
        description={confirmAction?.kind === "publish"
          ? t("admin.policy.confirm.publishDescription")
          : t("admin.policy.confirm.rollbackDescription", { version: confirmAction?.version ?? "" })}
        targetLabel={confirmAction?.version ?? draft?.version}
        consequence={t("admin.lists.highRiskConsequence")}
        confirmLabel={confirmAction?.kind === "publish" ? t("admin.policy.confirm.publishConfirm") : t("admin.policy.confirm.rollbackConfirm")}
        tone="danger"
        requireText={confirmAction?.kind === "publish" ? (draft?.version ?? "PUBLISH") : confirmAction?.version}
        onOpenChange={(open) => { if (!open) setConfirmAction(null); }}
        onConfirm={() => confirmAction?.kind === "publish" ? handlePublish() : confirmAction?.version ? handleRollback(confirmAction.version) : undefined}
      />
    </div>
  );
}


function formatTime(value?: string | null) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

export function ApprovalsFeature() {
  const router = useRouter();
  const { t } = useI18n();
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const table = useTableQuery();
  const [risk, setRisk] = useUrlFilter("risk", "all");
  const [groupId, setGroupId] = useState("default-group");

  useEffect(() => { if (!getToken()) router.replace("/login"); }, [router]);

  const approvals = useQuery({
    queryKey: adminKeys.list("approvals", table.query),
    queryFn: getPendingApprovals,
  });
  const allRows = useMemo(() => approvals.data?.pending ?? [], [approvals.data]);
  useBackgroundQueryToast(approvals.error, allRows.length > 0, t("admin.lists.backgroundError"));

  const decision = useMutation({
    mutationFn: (input: { id: string; decision: ApprovalDecisionType }) => decideApproval({
      approval_id: input.id,
      decision: input.decision,
      group_id: input.decision === "APPROVED_GROUP_ALWAYS" ? groupId.trim() : undefined,
    }),
    onSuccess: async (_data, input) => {
      toast({ title: t("approvals.done", { decision: input.decision }), variant: "success" });
      await queryClient.invalidateQueries({ queryKey: adminKeys.lists("approvals") });
    },
    onError: (error) => toast({ title: t("approvals.actionFailed"), description: error instanceof Error ? error.message : undefined, variant: "error" }),
  });

  const filteredByRisk = useMemo(() => risk === "all" ? allRows : allRows.filter((row) => row.risk?.toUpperCase() === risk), [allRows, risk]);
  // The pending-approvals API has no search/sort/page parameters; keep its contract and apply the shared client adapter.
  const visible = useMemo(() => applyClientListQuery(
    filteredByRisk,
    table.query,
    [(row) => row.approval_id, (row) => row.user_id, (row) => row.agent_id, (row) => row.action, (row) => row.resource],
    { requested: (row) => row.created_at ?? "", expires: (row) => row.expires_at, risk: (row) => row.risk, requester: (row) => row.user_id },
  ), [filteredByRisk, table.query]);

  function decide(row: ApprovalRequestItem, type: ApprovalDecisionType) {
    if (type === "APPROVED_GROUP_ALWAYS" && !groupId.trim()) {
      toast({ title: t("approvals.needGroupId"), variant: "error" });
      return;
    }
    decision.mutate({ id: row.approval_id, decision: type });
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div><h1 className="flex items-center gap-2 text-2xl font-semibold"><ClipboardCheck aria-hidden="true" className="h-6 w-6" />{t("approvals.title")}</h1><p className="text-sm text-muted-foreground">{t("approvals.subtitle")}</p></div>
        <Button variant="outline" size="sm" disabled={approvals.isFetching} onClick={() => approvals.refetch()}><RefreshCw aria-hidden="true" className="h-4 w-4" />{t("common.refresh")}</Button>
      </div>
      <div className="grid gap-3 sm:grid-cols-2">
        <FormField id="approval-risk" label={t("admin.lists.riskFilter")}>
          <select value={risk} onChange={(event) => setRisk(event.target.value)} className="flex h-9 w-full rounded-md border bg-background px-3 text-sm">
            <option value="all">{t("llmUsage.filterAll")}</option><option value="HIGH">HIGH</option><option value="MEDIUM">MEDIUM</option><option value="LOW">LOW</option>
          </select>
        </FormField>
        <FormField id="approval-group" label={t("approvals.groupId")}><Input value={groupId} onChange={(event) => setGroupId(event.target.value)} /></FormField>
      </div>
      <DataTable
        rows={visible.rows}
        columns={[
          { id: "requester", header: t("approvals.colRequester"), accessor: (row) => row.user_id, sortable: true },
          { id: "agent", header: t("approvals.colAgent"), accessor: (row) => row.agent_id },
          { id: "action", header: t("approvals.colAction"), accessor: (row) => row.action },
          { id: "resource", header: t("approvals.colResource"), accessor: (row) => row.resource },
          { id: "risk", header: t("approvals.colRisk"), accessor: (row) => row.risk, sortable: true },
          { id: "requested", header: t("approvals.requestedAt"), cell: (row) => formatTime(row.created_at), sortable: true },
          { id: "expires", header: t("approvals.expires"), cell: (row) => formatTime(row.expires_at), sortable: true },
        ]}
        rowKey={(row) => row.approval_id}
        loading={approvals.isLoading}
        error={approvals.error ? normalizeAdminError(approvals.error) : undefined}
        onRetry={() => approvals.refetch()}
        query={table.query}
        onQueryChange={table.onQueryChange}
        totalRows={visible.totalRows}
        searchable
        rowActions={(row) => [
          { id: "approve-once", label: t("approvals.decideOnce"), disabled: decision.isPending, onSelect: () => decide(row, "APPROVED_ONCE") },
          { id: "approve-user", label: t("approvals.decideUserAlways"), disabled: decision.isPending, onSelect: () => decide(row, "APPROVED_USER_ALWAYS") },
          { id: "approve-group", label: t("approvals.decideGroupAlways"), disabled: decision.isPending, onSelect: () => decide(row, "APPROVED_GROUP_ALWAYS") },
          { id: "deny", label: t("approvals.decideDeny"), tone: "danger", disabled: decision.isPending, onSelect: () => decide(row, "DENIED") },
        ]}
        empty={{ title: t("approvals.emptyTitle"), description: t("approvals.emptyDesc"), filtered: Boolean(table.query.search || risk !== "all") }}
        ariaLabel={t("approvals.ariaList")}
      />
    </div>
  );
}


export function AuditFeature() {
  const router = useRouter();
  const { t } = useI18n();
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const table = useTableQuery();
  const [from, setFrom] = useUrlFilter("from");
  const [to, setTo] = useUrlFilter("to");

  useEffect(() => { if (!getToken()) router.replace("/login"); }, [router]);

  const audit = useQuery({
    queryKey: adminKeys.list("audit", table.query),
    queryFn: async () => {
      const [events, checkpoint, verification] = await Promise.all([
        getAuditEvents(),
        getAuditCheckpoint().catch(() => null),
        verifyAuditChain().catch(() => null),
      ]);
      return { events, checkpoint, verification };
    },
  });
  const sourceRows = useMemo(() => audit.data?.events.events ?? [], [audit.data]);
  useBackgroundQueryToast(audit.error, sourceRows.length > 0, t("admin.lists.backgroundError"));

  const verify = useMutation({
    mutationFn: verifyAuditChain,
    onSuccess: async (result) => {
      toast({ title: result.chain_valid ? t("admin.audit.integrityValid") : t("admin.audit.integrityInvalid"), variant: result.chain_valid ? "success" : "error" });
      await queryClient.invalidateQueries({ queryKey: adminKeys.lists("audit") });
    },
    onError: (error) => toast({ title: t("common.verifyFailed"), description: error instanceof Error ? error.message : undefined, variant: "error" }),
  });

  const datedRows = useMemo(() => sourceRows.filter((row) => {
    const timestamp = new Date(row.timestamp).getTime();
    if (from && timestamp < new Date(from + "T00:00:00").getTime()) return false;
    if (to && timestamp > new Date(to + "T23:59:59.999").getTime()) return false;
    return true;
  }), [from, sourceRows, to]);
  // Audit events currently arrive as one compatible response; client filtering/paging is used until additive server parameters exist.
  const visible = useMemo(() => applyClientListQuery(
    datedRows,
    table.query.sort ? table.query : { ...table.query, sort: { id: "timestamp", direction: "desc" } },
    [(row) => row.event_type, (row) => row.user_id, (row) => row.agent_id, (row) => row.action, (row) => row.resource],
    { timestamp: (row) => row.timestamp, actor: (row) => row.user_id ?? row.agent_id ?? "", event: (row) => row.event_type },
  ), [datedRows, table.query]);
  const integrityFailed = audit.data?.verification && !audit.data.verification.chain_valid;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div><h1 className="flex items-center gap-2 text-2xl font-semibold"><ScrollText aria-hidden="true" className="h-6 w-6" />{t("audit.title")}</h1><p className="text-sm text-muted-foreground">{t("audit.subtitle")}</p></div>
        <div className="flex gap-2">
          <Button variant="outline" size="sm" disabled={audit.isFetching} onClick={() => audit.refetch()}><RefreshCw aria-hidden="true" className="h-4 w-4" />{t("common.refresh")}</Button>
          <Button size="sm" disabled={verify.isPending} onClick={() => verify.mutate()}><ShieldCheck aria-hidden="true" className="h-4 w-4" />{t("common.verify")}</Button>
        </div>
      </div>
      {integrityFailed ? <ErrorState title={t("admin.audit.integrityInvalid")} description={t("admin.audit.integrityInvalidDescription")} code="AUDIT_INTEGRITY_FAILED" retry={() => verify.mutate()} /> : null}
      <div className="grid gap-3 sm:grid-cols-2">
        <FormField id="audit-from" label={t("admin.lists.fromDate")}><input type="date" value={from} onChange={(event) => setFrom(event.target.value)} className="flex h-9 w-full rounded-md border bg-background px-3 text-sm" /></FormField>
        <FormField id="audit-to" label={t("admin.lists.toDate")}><input type="date" value={to} onChange={(event) => setTo(event.target.value)} className="flex h-9 w-full rounded-md border bg-background px-3 text-sm" /></FormField>
      </div>
      <div className="grid gap-3 sm:grid-cols-3">
        <Card><CardHeader className="pb-2"><CardTitle className="text-sm">{t("audit.eventCount")}</CardTitle></CardHeader><CardContent className="text-2xl font-semibold">{audit.data?.events.count ?? sourceRows.length}</CardContent></Card>
        <Card><CardHeader className="pb-2"><CardTitle className="text-sm">{t("audit.chainIntegrity")}</CardTitle></CardHeader><CardContent><Badge variant={audit.data?.verification?.chain_valid ? "success" : "secondary"}>{audit.data?.verification?.chain_valid ? "valid" : "unknown"}</Badge></CardContent></Card>
        <Card><CardHeader className="pb-2"><CardTitle className="text-sm">Checkpoint</CardTitle></CardHeader><CardContent className="truncate font-mono text-xs">{audit.data?.checkpoint?.chain_head_hash ?? "—"}</CardContent></Card>
      </div>
      <DataTable
        rows={visible.rows}
        columns={[
          { id: "timestamp", header: t("llmUsage.colTime"), cell: (row) => formatTime(row.timestamp), sortable: true },
          { id: "event", header: t("audit.colEvent"), accessor: (row) => row.event_type, sortable: true },
          { id: "actor", header: t("audit.colActor"), accessor: (row) => row.user_id ?? row.agent_id ?? "—", sortable: true },
          { id: "action", header: t("audit.colAction"), accessor: (row) => row.action },
          { id: "resource", header: t("audit.colResource"), accessor: (row) => row.resource },
          { id: "decision", header: t("audit.colDecision"), accessor: (row) => row.decision },
        ]}
        rowKey={(row) => row.event_id}
        loading={audit.isLoading}
        error={audit.error ? normalizeAdminError(audit.error) : undefined}
        onRetry={() => audit.refetch()}
        query={table.query}
        onQueryChange={table.onQueryChange}
        totalRows={visible.totalRows}
        searchable
        empty={{ title: t("audit.noEvents"), description: t("audit.noEventsDesc"), filtered: Boolean(table.query.search || from || to) }}
        ariaLabel={t("audit.timelineTitle")}
      />
    </div>
  );
}
