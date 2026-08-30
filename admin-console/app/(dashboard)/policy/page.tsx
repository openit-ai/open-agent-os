"use client";
import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { useI18n } from "@/lib/i18n";
import {
  getToken,
  getPolicyBundles,
  getPolicyDraft,
  getPolicyHistory,
  validatePolicy,
  simulatePolicy,
  upsertPolicyDraft,
  approvePolicy,
  publishPolicy,
  rollbackPolicy,
  type PolicyBundle,
  type PolicyRule,
  type PolicyDraftBundle,
} from "@/lib/api";
import { RefreshCw, Shield, AlertTriangle, CheckCircle2, FlaskConical, History, FileEdit, Upload, RotateCcw } from "lucide-react";

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

const SOURCE_LABEL: Record<string, string> = {
  explicit_deny: "Explicit Deny",
  security_boundary_deny: "Security Boundary",
  personal_delegation: "Personal Delegation",
  persistent_user_grant: "Persistent Grant",
  group_grant: "Group Grant",
  default_bundle: "Default Bundle",
  jit_approval: "JIT Approval",
  default_deny: "Default Deny",
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

export default function PolicyPage() {
  const router = useRouter();
  const { t } = useI18n();
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
        setValidateResult({ ok: false, errors: [e instanceof Error ? e.message : "Invalid JSON"] });
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
        try { const p = JSON.parse(jsonDraft); if (Array.isArray(p)) rules = p; } catch (e) { throw new Error(e instanceof Error ? e.message : "Invalid JSON"); }
      }
      if (!rules.length) throw new Error("Draft must contain at least one rule");
      const res = await upsertPolicyDraft({ rules, name: editName, bundle_id: editBundleId, allow_remove_mandatory: allowRemoveMandatory });
      setDraft(res.draft);
      setActionMsg(`Draft saved — status: ${res.draft.status} · version: ${res.draft.version}`);
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
    try { const r = await approvePolicy("default"); setActionMsg(`Approved — ${r.status}`); await fetchAll(); }
    catch (e) { setActionMsg(e instanceof Error ? e.message : String(e)); }
  }

  async function handlePublish() {
    setActionMsg(null);
    try { const r = await publishPolicy("default"); setActionMsg(`Published — active ${r.active_version}`); await fetchAll(); }
    catch (e) { setActionMsg(e instanceof Error ? e.message : String(e)); }
  }

  async function handleRollback(v: string) {
    if (!confirm(`Rollback to ${v}? This creates a new published version copying that historical rules.`)) return;
    setActionMsg(null);
    try { const r = await rollbackPolicy(v, "default"); setActionMsg(`Rolled back to ${v} → now ${r.active_version}`); await fetchAll(); }
    catch (e) { setActionMsg(e instanceof Error ? e.message : String(e)); }
  }

  return (
    <div className="mx-auto w-full max-w-[1200px] space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="flex items-center gap-2 text-2xl font-semibold">
          <Shield className="h-6 w-6" />
          Policy Bundles
        </h1>
        <Button variant="outline" size="sm" onClick={fetchAll} disabled={loading}>
          <RefreshCw className={`mr-1 h-4 w-4 ${loading ? "animate-spin" : ""}`} />
          {t("common.refresh")}
        </Button>
      </div>

      {error && <p className="rounded-md bg-[#DC2626]/10 p-3 text-sm text-[#DC2626]" role="alert">{error}</p>}
      {actionMsg && <p className="rounded-md border bg-card p-3 text-sm" role="status">{actionMsg}</p>}
      {activeVersion && <p className="text-xs text-muted-foreground">Active published version: <span className="font-mono font-medium text-foreground">{activeVersion}</span>{draft ? <> · Draft {draft.status} ({draft.version})</> : " · No draft"}</p>}

      <Tabs value={activeTab} onValueChange={setActiveTab}>
        <TabsList>
          <TabsTrigger value="bundles">Published</TabsTrigger>
          <TabsTrigger value="draft"><FileEdit className="mr-1 h-3.5 w-3.5" />Draft</TabsTrigger>
          <TabsTrigger value="simulate"><FlaskConical className="mr-1 h-3.5 w-3.5" />Simulate</TabsTrigger>
          <TabsTrigger value="history"><History className="mr-1 h-3.5 w-3.5" />History / Rollback</TabsTrigger>
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
                      {SOURCE_LABEL[src] ?? src}
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
            <Card><CardContent className="pt-6 text-center text-sm text-muted-foreground">{t("policy.noBundles")}</CardContent></Card>
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
                      <TableHeader><TableRow><TableHead className="whitespace-nowrap"># (Section 25)</TableHead><TableHead>source</TableHead><TableHead>action (glob)</TableHead><TableHead>resource (glob)</TableHead><TableHead>decision</TableHead><TableHead>priority</TableHead></TableRow></TableHeader>
                      <TableBody>
                        {[...(bundle.rules ?? [])].sort((a, b) => { const ao = orderIndex(a.source, evalOrder); const bo = orderIndex(b.source, evalOrder); if (ao !== bo) return ao - bo; if (a.priority !== b.priority) return a.priority - b.priority; return a.id.localeCompare(b.id); }).map((rule: PolicyRule) => {
                          const isExplicitDeny = rule.source === "explicit_deny";
                          const isPersonal = rule.source === "personal_delegation";
                          return (
                            <TableRow key={rule.id} className={isExplicitDeny ? "bg-[#DC2626]/10 hover:bg-[#DC2626]/15" : isPersonal ? "bg-[#22C55E]/5" : ""}>
                              <TableCell className="whitespace-nowrap text-xs font-medium">{orderIndex(rule.source, evalOrder)}<span className="ml-1 text-muted-foreground">· {rule.id}</span></TableCell>
                              <TableCell><Badge variant={isExplicitDeny ? "danger" : isPersonal ? "success" : "secondary"} className="whitespace-nowrap">{SOURCE_LABEL[rule.source] ?? rule.source}</Badge></TableCell>
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
              <CardHeader className="pb-2"><CardTitle className="text-sm flex items-center gap-2"><FileEdit className="h-4 w-4" />Draft — {draft.status} <Badge variant={draft.status === "approved" ? "success" : "warning"}>{draft.status}</Badge><span className="font-mono text-xs text-muted-foreground">{draft.version}</span></CardTitle>
                <CardDescription>Created by {draft.created_by ?? "-"} {draft.created_at ? new Date(draft.created_at).toLocaleString() : ""}{draft.approved_by ? ` · Approved by ${draft.approved_by}` : ""}</CardDescription>
              </CardHeader>
              <CardContent className="flex flex-wrap gap-2">
                <Button size="sm" variant="outline" onClick={handleValidate}><CheckCircle2 className="h-4 w-4" />Validate</Button>
                <Button size="sm" variant="secondary" onClick={handleApprove}>Approve (L5)</Button>
                <Button size="sm" onClick={handlePublish}><Upload className="h-4 w-4" />Publish (L5)</Button>
                <Button size="sm" variant="outline" onClick={() => setActiveTab("draft")}>Edit Draft</Button>
              </CardContent>
            </Card>
          )}
          <p className="mt-3 text-xs text-muted-foreground">{t("policy.dataNote")}</p>
        </TabsContent>

        <TabsContent value="draft">
          <Card>
            <CardHeader><CardTitle className="text-base">Draft — Edit / Validate → Approve → Publish</CardTitle><CardDescription>L5 required for draft/approve/publish. Validation enforces explicit_deny + mandatory deny-external-export. Default deny is implicit.</CardDescription></CardHeader>
            <CardContent className="space-y-4">
              <div className="grid gap-3 sm:grid-cols-3">
                <div className="space-y-1"><Label>Bundle ID</Label><Input value={editBundleId} onChange={(e) => setEditBundleId(e.target.value)} placeholder="default-bundle-v1" /></div>
                <div className="space-y-1"><Label>Name</Label><Input value={editName} onChange={(e) => setEditName(e.target.value)} placeholder="Default Policy Bundle" /></div>
                <div className="flex items-end gap-2">
                  <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={allowRemoveMandatory} onChange={(e) => setAllowRemoveMandatory(e.target.checked)} /> allow_remove_mandatory (L5)</label>
                </div>
              </div>

              <div className="space-y-2">
                <div className="flex items-center justify-between">
                  <Label>Rules (table)</Label>
                  <Button size="sm" variant="outline" onClick={() => syncJson([...editRules, { ...newEmptyRule(), id: `rule-${Date.now()}` }])}>Add rule</Button>
                </div>
                <div className="overflow-auto rounded-md border">
                  <Table>
                    <TableHeader><TableRow><TableHead>id</TableHead><TableHead>source</TableHead><TableHead>action</TableHead><TableHead>resource_pattern</TableHead><TableHead>effect</TableHead><TableHead>priority</TableHead><TableHead></TableHead></TableRow></TableHeader>
                    <TableBody>
                      {editRules.length === 0 ? <TableRow><TableCell colSpan={7} className="text-center text-sm text-muted-foreground">No rules — add one</TableCell></TableRow> : editRules.map((r, idx) => (
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
                <Label>Raw JSON (alternative editor — kept in sync on load)</Label>
                <textarea value={jsonDraft} onChange={(e) => setJsonDraft(e.target.value)} rows={8} className="w-full rounded-md border bg-muted/20 p-3 font-mono text-xs" placeholder='[{"id":"deny-external-export","source":"explicit_deny","action":"*","resource_pattern":"external:*","effect":"DENY","priority":10}]' />
                <div className="flex gap-2">
                  <Button size="sm" variant="outline" onClick={() => { try { const p = JSON.parse(jsonDraft); if (Array.isArray(p)) setEditRules(p as PolicyRule[]); setActionMsg("Loaded from JSON"); } catch (e) { setActionMsg(e instanceof Error ? e.message : String(e)); } }}>Load JSON → table</Button>
                  <Button size="sm" variant="outline" onClick={() => setJsonDraft(JSON.stringify(editRules, null, 2))}>Table → JSON</Button>
                </div>
              </div>

              <div className="flex flex-wrap gap-2">
                <Button variant="outline" onClick={handleValidate}><CheckCircle2 className="h-4 w-4" />Validate</Button>
                <Button onClick={handleSaveDraft} disabled={saving}>{saving ? "Saving..." : "Save Draft (L5)"}</Button>
                <Button variant="secondary" onClick={handleApprove}>Approve (L5)</Button>
                <Button onClick={handlePublish}><Upload className="h-4 w-4" />Publish (L5)</Button>
              </div>

              {validateResult && (
                <div className={`rounded-md border p-3 text-sm ${validateResult.ok ? "border-[#22C55E] bg-[#22C55E]/10" : "border-[#DC2626] bg-[#DC2626]/10"}`}>
                  <p className="font-medium">{validateResult.ok ? "Validation passed" : "Validation failed"}</p>
                  {validateResult.errors.length > 0 && <ul className="mt-1 list-disc pl-5 text-xs">{validateResult.errors.map((er, i) => <li key={i}>{er}</li>)}</ul>}
                </div>
              )}
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="simulate">
          <Card>
            <CardHeader><CardTitle className="text-base flex items-center gap-2"><FlaskConical className="h-4 w-4" />Simulation (dry-run)</CardTitle><CardDescription>Evaluates action+resource against draft (default) or published bundle. Uses same Section 25 order + fnmatch glob.</CardDescription></CardHeader>
            <CardContent className="space-y-4">
              <div className="grid gap-3 sm:grid-cols-3">
                <div className="space-y-1"><Label>action</Label><Input value={simAction} onChange={(e) => setSimAction(e.target.value)} placeholder="read / write / external:export" /></div>
                <div className="space-y-1"><Label>resource</Label><Input value={simResource} onChange={(e) => setSimResource(e.target.value)} placeholder="doc:public/* / external:share" /></div>
                <div className="flex items-end gap-2">
                  <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={simUseDraft} onChange={(e) => setSimUseDraft(e.target.checked)} /> use_draft</label>
                  <Button onClick={handleSimulate} disabled={simLoading}>{simLoading ? "..." : "Simulate"}</Button>
                </div>
              </div>
              {simError && <p className="text-sm text-[#DC2626]" role="alert">{simError}</p>}
              {simResult && (
                <div className="rounded-md border bg-muted/30 p-3 text-sm">
                  <div className="flex flex-wrap items-center gap-2">Decision: <Badge variant={decisionVariant(simResult.decision)}>{simResult.decision}</Badge> source: <Badge variant="secondary">{simResult.source}</Badge></div>
                  <p className="mt-1 text-xs text-muted-foreground">{simResult.reason}</p>
                  {simResult.matched_rule && <pre className="mt-2 overflow-auto rounded bg-background p-2 text-xs">{JSON.stringify(simResult.matched_rule, null, 2)}</pre>}
                </div>
              )}
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="history">
          <Card>
            <CardHeader><CardTitle className="text-base flex items-center gap-2"><History className="h-4 w-4" />Version History — rollback creates new published version</CardTitle><CardDescription>Immutable history; rollback copies target rules into a new published version (incremented). Requires L5.</CardDescription></CardHeader>
            <CardContent className="p-0">
              {history.length === 0 ? <div className="p-6 text-center text-sm text-muted-foreground">No history yet — publish a draft to create a version.</div> : (
                <div className="overflow-auto">
                  <Table>
                    <TableHeader><TableRow><TableHead>version</TableHead><TableHead>status</TableHead><TableHead>bundle</TableHead><TableHead>rules</TableHead><TableHead>created_by</TableHead><TableHead>created_at</TableHead><TableHead className="text-right">Action</TableHead></TableRow></TableHeader>
                    <TableBody>
                      {[...history].sort((a, b) => (a.created_at ?? "").localeCompare(b.created_at ?? "")).map((h) => (
                        <TableRow key={h.id_row ?? h.id + h.version} className={h.version === activeVersion ? "bg-[#22C55E]/5" : ""}>
                          <TableCell className="font-mono text-xs">v{h.version}{h.version === activeVersion && <Badge variant="success" className="ml-2">active</Badge>}</TableCell>
                          <TableCell><Badge variant={h.status === "published" ? "success" : h.status === "approved" ? "warning" : "secondary"}>{h.status}</Badge></TableCell>
                          <TableCell className="text-xs">{h.name}<span className="ml-1 font-mono text-muted-foreground">{h.id}</span></TableCell>
                          <TableCell className="text-xs">{h.rules?.length ?? 0}</TableCell>
                          <TableCell className="text-xs">{h.created_by ?? "-"}</TableCell>
                          <TableCell className="text-xs">{h.created_at ? new Date(h.created_at).toLocaleString() : "-"}</TableCell>
                          <TableCell className="text-right">{h.status === "published" ? <Button size="sm" variant="outline" onClick={() => handleRollback(h.version)}><RotateCcw className="h-3.5 w-3.5" />Rollback</Button> : <span className="text-xs text-muted-foreground">-</span>}</TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </div>
              )}
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>
    </div>
  );
}
