"use client";
import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { getToken, listLLMProviders, createLLMProvider, updateLLMProvider, deleteLLMProvider, testLLMProvider, toggleLLMProvider, getRuntimeMode, setRuntimeMode, type LLMProvider, type LLMProviderType, type RuntimeMode } from "@/lib/api";
import { RefreshCw, Trash2, Pencil, Plus, Cpu, Plug2, Ban, CheckCircle2, Info } from "lucide-react";
import { useI18n } from "@/lib/i18n";

const PROVIDER_TYPES: LLMProviderType[] = ["claude", "codex", "gemini", "opencode-go", "openrouter", "ollama"];
const APIKEY_TYPES: LLMProviderType[] = ["claude", "codex", "gemini", "openrouter"];

function providerBadge(p: string) {
  const map: Record<string, string> = {
    claude: "bg-purple-600 text-white",
    codex: "bg-black text-white",
    gemini: "bg-blue-600 text-white",
    "opencode-go": "bg-zinc-700 text-white",
    "opencode": "bg-zinc-700 text-white",
    openrouter: "bg-pink-600 text-white",
    ollama: "bg-orange-500 text-white",
  };
  return map[p] ?? "bg-secondary";
}

export default function ProvidersPage() {
  const router = useRouter();
  const { t } = useI18n();

  const [runtimeMode, setRuntimeModeState] = useState<RuntimeMode>("hermes");
  const [runtimeLoading, setRuntimeLoading] = useState(true);
  const [runtimeSaving, setRuntimeSaving] = useState(false);
  const [runtimeError, setRuntimeError] = useState<string | null>(null);

  const [items, setItems] = useState<LLMProvider[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // form state
  const [provider, setProvider] = useState<LLMProviderType>("claude");
  const [name, setName] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [model, setModel] = useState("");
  const [path, setPath] = useState("");
  const [url, setUrl] = useState("");
  const [enabled, setEnabled] = useState(true);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [formLoading, setFormLoading] = useState(false);
  const [testingId, setTestingId] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<Record<string, { status: string; latency_ms?: number; detail?: string }>>({});

  const fetchRuntime = useCallback(async () => {
    setRuntimeError(null);
    try {
      const res = await getRuntimeMode();
      setRuntimeMode(res.mode);
    } catch (e) {
      setRuntimeError(e instanceof Error ? e.message : t("common.error"));
    } finally {
      setRuntimeLoading(false);
    }
  }, [t]);

  const fetchList = useCallback(async () => {
    try {
      const res = await listLLMProviders();
      const list: LLMProvider[] = Array.isArray(res) ? res : ((res as { providers: LLMProvider[] }).providers ?? (res as { items: LLMProvider[] }).items ?? []);
      setItems(list);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : t("common.error"));
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    if (!getToken()) { router.replace("/login"); return; }
    fetchRuntime();
    fetchList();
  }, [fetchRuntime, fetchList, router]);

  async function handleRuntimeChange(mode: RuntimeMode) {
    setRuntimeSaving(true);
    setRuntimeError(null);
    try {
      const res = await setRuntimeMode(mode);
      setRuntimeModeState(res.mode);
      if (res.mode === "llm") await fetchList();
    } catch (e) {
      setRuntimeError(e instanceof Error ? e.message : t("common.error"));
    } finally {
      setRuntimeSaving(false);
    }
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setFormError(null);
    // validation per provider type
    if (APIKEY_TYPES.includes(provider) && !apiKey && !editingId) {
      setFormError(t("providers.validationApiKey", { provider }));
      return;
    }
    if (APIKEY_TYPES.includes(provider) && !apiKey && editingId) {
      // allow empty apiKey on edit (keep existing) but if we cleared, it's okay
    }
    if (provider === "opencode-go" && !path) {
      setFormError(t("providers.validationPath"));
      return;
    }
    if (provider === "ollama" && !url) {
      setFormError(t("providers.validationUrl"));
      return;
    }
    setFormLoading(true);
    try {
      const payload: Record<string, unknown> = { provider, name, model: model || undefined, baseUrl: baseUrl || undefined, enabled };
      if (APIKEY_TYPES.includes(provider)) {
        if (apiKey) payload.apiKey = apiKey;
      }
      if (provider === "opencode-go") payload.path = path;
      if (provider === "ollama") payload.url = url;
      // for other types include optional baseUrl/model already
      if (provider !== "opencode-go" && provider !== "ollama") {
        // keep path/url undefined
      }
      // clean empty strings
      Object.keys(payload).forEach((k) => { if (payload[k] === "") delete payload[k]; });

      if (editingId) {
        await updateLLMProvider(editingId, payload as never);
      } else {
        await createLLMProvider(payload as never);
      }
      setName(""); setApiKey(""); setBaseUrl(""); setModel(""); setPath(""); setUrl(""); setEnabled(true); setEditingId(null);
      await fetchList();
    } catch (e) {
      setFormError(e instanceof Error ? e.message : t("providers.saveFailed"));
    } finally {
      setFormLoading(false);
    }
  }

  function startEdit(item: LLMProvider) {
    setEditingId(item.id);
    setProvider(item.provider);
    setName(item.name ?? "");
    setApiKey(""); // don't prefill masked key; user re-enters if needed
    setBaseUrl((item.base_url ?? item.baseUrl ?? "") as string);
    setModel(item.model ?? "");
    setPath(item.path ?? "");
    setUrl(item.url ?? "");
    setEnabled(item.enabled);
  }

  async function handleDelete(id: string) {
    if (!confirm(t("providers.deleteConfirm"))) return;
    try { await deleteLLMProvider(id); await fetchList(); }
    catch (e) { alert(e instanceof Error ? e.message : t("providers.deleteFailed")); }
  }

  async function handleTest(id: string) {
    setTestingId(id);
    try {
      const res = await testLLMProvider(id);
      setTestResult((prev) => ({ ...prev, [id]: { status: res.status, latency_ms: res.latency_ms, detail: res.detail } }));
      await fetchList();
    } catch (e) {
      setTestResult((prev) => ({ ...prev, [id]: { status: "failed", detail: e instanceof Error ? e.message : t("providers.testFailed") } }));
    } finally { setTestingId(null); }
  }

  async function handleToggle(id: string) {
    try { await toggleLLMProvider(id); await fetchList(); }
    catch (e) { alert(e instanceof Error ? e.message : t("common.error")); }
  }

  const isHermes = runtimeMode === "hermes";

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-semibold"><Cpu className="h-6 w-6" /> {t("providers.title")}</h1>
          <p className="text-sm text-muted-foreground">{t("providers.subtitle")}</p>
        </div>
        <Button variant="outline" size="sm" onClick={() => { setLoading(true); fetchList(); fetchRuntime(); }}><RefreshCw className="mr-1 h-4 w-4" />{t("common.refresh")}</Button>
      </div>

      {error && <p className="text-sm text-[#DC2626]" role="alert">{error}</p>}
      {runtimeError && <p className="text-sm text-[#DC2626]" role="alert">{runtimeError}</p>}

      {/* Runtime Mode Selector */}
      <Card>
        <CardHeader><CardTitle className="text-base">{t("providers.runtimeTitle")}</CardTitle><CardDescription>{t("providers.runtimeDesc")}</CardDescription></CardHeader>
        <CardContent>
          {runtimeLoading ? (
            <p className="text-sm text-muted-foreground">{t("common.loading")}</p>
          ) : (
            <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
              <label className={`flex cursor-pointer items-center gap-3 rounded-lg border p-3 flex-1 ${runtimeMode === "hermes" ? "border-primary bg-primary/5" : "hover:bg-accent"}`}>
                <input type="radio" name="runtime_mode" value="hermes" checked={runtimeMode === "hermes"} onChange={() => handleRuntimeChange("hermes")} disabled={runtimeSaving} className="h-4 w-4 accent-primary" />
                <div>
                  <p className="text-sm font-medium">{t("providers.runtimeHermes")}</p>
                  <p className="text-xs text-muted-foreground">{t("providers.runtimeHermesDesc")}</p>
                </div>
                {runtimeMode === "hermes" && <Badge variant="success" className="ml-auto">Active</Badge>}
              </label>
              <label className={`flex cursor-pointer items-center gap-3 rounded-lg border p-3 flex-1 ${runtimeMode === "llm" ? "border-primary bg-primary/5" : "hover:bg-accent"}`}>
                <input type="radio" name="runtime_mode" value="llm" checked={runtimeMode === "llm"} onChange={() => handleRuntimeChange("llm")} disabled={runtimeSaving} className="h-4 w-4 accent-primary" />
                <div>
                  <p className="text-sm font-medium">{t("providers.runtimeLLM")}</p>
                  <p className="text-xs text-muted-foreground">{t("providers.runtimeLLMDesc")}</p>
                </div>
                {runtimeMode === "llm" && <Badge variant="success" className="ml-auto">Active</Badge>}
              </label>
              {runtimeSaving && <span className="text-xs text-muted-foreground">{t("common.saving")}</span>}
            </div>
          )}
        </CardContent>
      </Card>

      {/* Hermes banner */}
      {isHermes ? (
        <Card className="border-blue-200 bg-blue-50 dark:bg-blue-950/20">
          <CardContent className="flex items-start gap-3 pt-6">
            <Info className="h-5 w-5 text-blue-600 mt-0.5" />
            <div>
              <p className="text-sm font-medium text-blue-900 dark:text-blue-100">{t("providers.runtimeBannerHermes")}</p>
              <p className="text-xs text-blue-700 dark:text-blue-300 mt-1">{t("providers.runtimeBannerHermesDesc")}</p>
            </div>
          </CardContent>
        </Card>
      ) : (
        <>
          {/* Add/Edit Form */}
          <Card>
            <CardHeader><CardTitle className="text-base">{editingId ? t("providers.formTitleEdit") : t("providers.formTitleCreate")}</CardTitle></CardHeader>
            <CardContent>
              <form onSubmit={handleSubmit} className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
                <div className="space-y-1">
                  <Label htmlFor="provider">{t("providers.provider")}</Label>
                  <select id="provider" value={provider} onChange={(e) => setProvider(e.target.value as LLMProviderType)} className="flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm">
                    {PROVIDER_TYPES.map((o) => <option key={o} value={o}>{o}</option>)}
                  </select>
                </div>
                <div className="space-y-1">
                  <Label htmlFor="name">{t("providers.name")}</Label>
                  <Input id="name" placeholder={t("providers.namePlaceholder")} value={name} onChange={(e) => setName(e.target.value)} />
                </div>
                <div className="space-y-1">
                  <Label htmlFor="model">{t("providers.model")}</Label>
                  <Input id="model" placeholder={t("providers.modelPlaceholder")} value={model} onChange={(e) => setModel(e.target.value)} />
                </div>

                {APIKEY_TYPES.includes(provider) && (
                  <div className="space-y-1">
                    <Label htmlFor="apiKey">{t("providers.apiKey")} {editingId && <span className="text-xs text-muted-foreground">(leave blank to keep)</span>}</Label>
                    <Input id="apiKey" type="password" placeholder={t("providers.apiKeyPlaceholder")} value={apiKey} onChange={(e) => setApiKey(e.target.value)} />
                  </div>
                )}
                {provider === "opencode-go" && (
                  <div className="space-y-1">
                    <Label htmlFor="path">{t("providers.path")}</Label>
                    <Input id="path" placeholder={t("providers.pathPlaceholder")} value={path} onChange={(e) => setPath(e.target.value)} required />
                  </div>
                )}
                {provider === "ollama" && (
                  <div className="space-y-1">
                    <Label htmlFor="url">{t("providers.url")}</Label>
                    <Input id="url" placeholder={t("providers.urlPlaceholder")} value={url} onChange={(e) => setUrl(e.target.value)} required />
                  </div>
                )}
                <div className="space-y-1">
                  <Label htmlFor="baseUrl">{t("providers.baseUrl")}</Label>
                  <Input id="baseUrl" placeholder={t("providers.baseUrlPlaceholder")} value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} />
                </div>
                <div className="flex items-end gap-2">
                  <label className="flex items-center gap-2 text-sm">
                    <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} className="h-4 w-4 accent-primary" />
                    {t("providers.enabled")}
                  </label>
                </div>
                <div className="flex items-end gap-2 sm:col-span-2 lg:col-span-3">
                  <Button type="submit" disabled={formLoading} className="flex-1 sm:flex-none"><Plus className="mr-1 h-4 w-4" />{editingId ? t("providers.update") : t("providers.add")}</Button>
                  {editingId && <Button type="button" variant="outline" onClick={() => { setEditingId(null); setName(""); setApiKey(""); setBaseUrl(""); setModel(""); setPath(""); setUrl(""); setEnabled(true); }}>{t("providers.cancel")}</Button>}
                </div>
              </form>
              {formError && <p className="mt-2 text-sm text-[#DC2626]" role="alert">{formError}</p>}
              <p className="mt-2 text-xs text-muted-foreground">{t("providers.testConnectionNote")}</p>
            </CardContent>
          </Card>

          {/* List Table */}
          <Card>
            <CardContent className="p-0">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>{t("providers.provider")}</TableHead>
                    <TableHead>Name</TableHead>
                    <TableHead>Model</TableHead>
                    <TableHead>Cred / Target</TableHead>
                    <TableHead>{t("common.status")}</TableHead>
                    <TableHead>Last Test</TableHead>
                    <TableHead className="text-right">{t("common.actions")}</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {loading ? (
                    <TableRow><TableCell colSpan={7} className="text-center text-muted-foreground">{t("common.loading")}</TableCell></TableRow>
                  ) : items.length === 0 ? (
                    <TableRow><TableCell colSpan={7} className="text-center text-muted-foreground">{t("providers.noData")}</TableCell></TableRow>
                  ) : items.map((it) => {
                    const cred = it.provider === "opencode-go" || (it.provider as string) === "opencode" ? it.path : it.provider === "ollama" ? (it.url ?? it.base_url ?? it.baseUrl) : (it.api_key_masked ?? it.apiKey ?? "***");
                    const isEnabled = it.enabled;
                    const tr = testResult[it.id];
                    return (
                      <TableRow key={it.id} className={!isEnabled ? "opacity-60" : ""}>
                        <TableCell><span className={`inline-flex rounded px-2 py-0.5 text-xs font-medium ${providerBadge(it.provider)}`}>{it.provider}</span></TableCell>
                        <TableCell className="text-sm">{it.name || "-"}</TableCell>
                        <TableCell className="text-xs font-mono">{it.model || "-"}</TableCell>
                        <TableCell className="max-w-[180px] truncate text-xs font-mono" title={String(cred ?? "")}>{cred ?? "-"}</TableCell>
                        <TableCell>{isEnabled ? <Badge variant="success">enabled</Badge> : <Badge variant="secondary">disabled</Badge>}</TableCell>
                        <TableCell className="text-xs">
                          {it.last_test_status ? (
                            <span className={it.last_test_status === "ok" ? "text-[#22C55E]" : "text-[#DC2626]"}>
                              {it.last_test_status} {it.last_test_latency_ms ? `(${it.last_test_latency_ms}ms)` : ""}
                            </span>
                          ) : tr ? (
                            <span className={tr.status === "ok" ? "text-[#22C55E]" : "text-[#DC2626]"}>{tr.status === "ok" ? t("providers.testOk", { ms: String(tr.latency_ms ?? "") }) : tr.detail ?? t("providers.testFailed")}</span>
                          ) : "-"}
                        </TableCell>
                        <TableCell>
                          <div className="flex justify-end gap-1">
                            <Button variant="outline" size="sm" disabled={testingId === it.id} onClick={() => handleTest(it.id)} title={t("providers.test")}>
                              {testingId === it.id ? t("providers.testing") : <><Plug2 className="h-3 w-3 mr-1" />{t("providers.test")}</>}
                            </Button>
                            <Button variant={isEnabled ? "outline" : "default"} size="sm" onClick={() => handleToggle(it.id)} title={isEnabled ? t("providers.toggleDisable") : t("providers.toggleEnable")}>
                              {isEnabled ? <Ban className="h-3 w-3" /> : <CheckCircle2 className="h-3 w-3" />}
                            </Button>
                            <Button variant="ghost" size="icon" onClick={() => startEdit(it)} aria-label={t("common.edit")}><Pencil className="h-4 w-4" /></Button>
                            <Button variant="ghost" size="icon" onClick={() => handleDelete(it.id)} aria-label={t("common.delete")}><Trash2 className="h-4 w-4" /></Button>
                          </div>
                        </TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            </CardContent>
          </Card>
        </>
      )}
    </div>
  );
}
