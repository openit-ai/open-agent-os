"use client";
import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { getToken, getFallbackConfig, updateFallbackConfig, type FallbackConfig, type FallbackEntry } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { RefreshCw, Plus, Trash2, ArrowUp, ArrowDown, Save, Info, Layers, Cpu, ShieldAlert } from "lucide-react";

const PROVIDER_TYPES = ["claude", "codex", "gemini", "opencode-go", "openrouter", "ollama"] as const;

function providerBadge(p: string) {
  const map: Record<string, string> = {
    claude: "bg-purple-600 text-white",
    codex: "bg-black text-white",
    gemini: "bg-blue-600 text-white",
    "opencode-go": "bg-zinc-700 text-white",
    openrouter: "bg-pink-600 text-white",
    ollama: "bg-orange-500 text-white",
  };
  return map[p] ?? "bg-secondary";
}

export default function FallbackPage() {
  const router = useRouter();
  const { t } = useI18n();
  const [cfg, setCfg] = useState<FallbackConfig | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saveMsg, setSaveMsg] = useState<string | null>(null);

  // add form
  const [addProvider, setAddProvider] = useState<string>("claude");
  const [addModel, setAddModel] = useState("");
  const [addEnabled, setAddEnabled] = useState(true);
  const [fallbackModel, setFallbackModel] = useState("");
  const [enabled, setEnabled] = useState(true);

  const fetchCfg = useCallback(async () => {
    setError(null);
    try {
      const res = await getFallbackConfig();
      setCfg(res);
      setEnabled(res.enabled);
      setFallbackModel(res.fallback_model ?? "");
    } catch (e) {
      setError(e instanceof Error ? e.message : t("fallback.loadFailed"));
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    if (!getToken()) { router.replace("/login"); return; }
    fetchCfg();
  }, [fetchCfg, router]);

  function addEntry() {
    if (!cfg) return;
    const provider = addProvider.trim();
    if (!provider) { setError(t("fallback.validationProvider")); return; }
    const normalized = provider === "opencode" ? "opencode-go" : provider;
    const entry: FallbackEntry = { provider: normalized, model: addModel.trim() || null, enabled: addEnabled };
    // duplicate check
    const dup = cfg.chain.some((e) => e.provider === entry.provider && (e.model ?? "") === (entry.model ?? ""));
    if (dup) { setError(t("fallback.validationDuplicate")); return; }
    setError(null);
    setCfg({ ...cfg, chain: [...cfg.chain, entry] });
    setAddModel("");
  }

  function removeAt(idx: number) {
    if (!cfg) return;
    if (!confirm(t("fallback.removeConfirm"))) return;
    const next = cfg.chain.filter((_, i) => i !== idx);
    setCfg({ ...cfg, chain: next });
  }

  function move(idx: number, dir: -1 | 1) {
    if (!cfg) return;
    const next = [...cfg.chain];
    const target = idx + dir;
    if (target < 0 || target >= next.length) return;
    const tmp = next[idx];
    next[idx] = next[target];
    next[target] = tmp;
    setCfg({ ...cfg, chain: next });
  }

  function toggleEntry(idx: number) {
    if (!cfg) return;
    const next = cfg.chain.map((e, i) => i === idx ? { ...e, enabled: !e.enabled } : e);
    setCfg({ ...cfg, chain: next });
  }

  async function handleSave() {
    if (!cfg) return;
    setSaving(true);
    setSaveMsg(null);
    setError(null);
    try {
      const payload = { enabled, chain: cfg.chain, fallback_model: fallbackModel.trim() || null };
      const saved = await updateFallbackConfig(payload);
      setCfg(saved);
      setEnabled(saved.enabled);
      setFallbackModel(saved.fallback_model ?? "");
      setSaveMsg(t("fallback.saved"));
      setTimeout(() => setSaveMsg(null), 2500);
    } catch (e) {
      setError(e instanceof Error ? e.message : t("fallback.saveFailed"));
    } finally {
      setSaving(false);
    }
  }

  if (loading) return <div className="p-6 text-sm text-muted-foreground">{t("common.loading")}</div>;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-semibold"><Layers className="h-6 w-6" /> {t("fallback.title")}</h1>
          <p className="text-sm text-muted-foreground">{t("fallback.subtitle")}</p>
        </div>
        <div className="flex items-center gap-2">
          <Button asChild variant="outline" size="sm"><Link href="/providers"><Cpu className="mr-1 h-4 w-4" />{t("fallback.viewProviders")}</Link></Button>
          <Button variant="outline" size="sm" onClick={() => { setLoading(true); fetchCfg(); }}><RefreshCw className="mr-1 h-4 w-4" />{t("common.refresh")}</Button>
        </div>
      </div>

      {error && <p className="text-sm text-[#DC2626]" role="alert">{error}</p>}
      {saveMsg && <p className="text-sm text-green-600" role="status">{saveMsg}</p>}

      <Card className="border-blue-200 bg-blue-50 dark:bg-blue-950/20">
        <CardHeader className="pb-2"><CardTitle className="flex items-center gap-2 text-sm"><Info className="h-4 w-4" /> {t("fallback.helpTitle")}</CardTitle></CardHeader>
        <CardContent className="space-y-1 text-xs text-muted-foreground">
          <p>{t("fallback.helpDesc")}</p>
          <p>{t("fallback.helpChain")}</p>
          <p>{t("fallback.helpModel")}</p>
          <p className="flex items-start gap-1"><ShieldAlert className="h-3 w-3 mt-0.5" />{t("fallback.helpNote")}</p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader><CardTitle className="text-base">{t("fallback.enabledLabel")}</CardTitle><CardDescription>{t("fallback.enabledDesc")}</CardDescription></CardHeader>
        <CardContent className="flex items-center gap-3">
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} className="h-4 w-4 accent-primary" />
            {enabled ? <Badge variant="success">enabled</Badge> : <Badge variant="secondary">disabled</Badge>}
          </label>
          {cfg?.updated_by && <span className="text-xs text-muted-foreground">{t("fallback.updatedBy")}: {cfg.updated_by} {cfg.updated_at ? `· ${new Date(cfg.updated_at).toLocaleString()}` : ""}</span>}
        </CardContent>
      </Card>

      <Card>
        <CardHeader><CardTitle className="text-base">{t("fallback.chainTitle")}</CardTitle><CardDescription>{t("fallback.chainDesc")}</CardDescription></CardHeader>
        <CardContent className="space-y-3">
          {cfg && cfg.chain.length === 0 ? (
            <p className="text-sm text-muted-foreground">{t("fallback.emptyChain")}</p>
          ) : (
            <div className="space-y-2">
              {cfg?.chain.map((e, idx) => (
                <div key={`${e.provider}-${e.model ?? ""}-${idx}`} className={`flex items-center gap-2 rounded-md border p-2 ${!e.enabled ? "opacity-60 bg-muted/30" : "bg-card"}`}>
                  <span className="text-xs font-mono text-muted-foreground w-5 text-center">{idx + 1}</span>
                  <span className={`inline-flex rounded px-2 py-0.5 text-xs font-medium ${providerBadge(e.provider)}`}>{e.provider}</span>
                  <span className="text-sm font-mono truncate flex-1">{e.model ?? <span className="text-muted-foreground italic">default model</span>}</span>
                  <label className="flex items-center gap-1 text-xs">
                    <input type="checkbox" checked={e.enabled} onChange={() => toggleEntry(idx)} className="h-3.5 w-3.5 accent-primary" />
                    {e.enabled ? "on" : "off"}
                  </label>
                  <Button variant="ghost" size="icon" className="h-7 w-7" disabled={idx === 0} onClick={() => move(idx, -1)} aria-label={t("fallback.moveUp")}><ArrowUp className="h-3.5 w-3.5" /></Button>
                  <Button variant="ghost" size="icon" className="h-7 w-7" disabled={idx === (cfg.chain.length - 1)} onClick={() => move(idx, 1)} aria-label={t("fallback.moveDown")}><ArrowDown className="h-3.5 w-3.5" /></Button>
                  <Button variant="ghost" size="icon" className="h-7 w-7" onClick={() => removeAt(idx)} aria-label={t("common.delete")}><Trash2 className="h-3.5 w-3.5" /></Button>
                </div>
              ))}
            </div>
          )}

          <div className="rounded-md border p-3 space-y-3 bg-muted/20">
            <p className="text-sm font-medium">{t("fallback.addTitle")}</p>
            <div className="grid gap-3 sm:grid-cols-3">
              <div className="space-y-1">
                <Label>{t("fallback.providerLabel")}</Label>
                <select value={addProvider} onChange={(e) => setAddProvider(e.target.value)} className="flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm">
                  {PROVIDER_TYPES.map((o) => <option key={o} value={o}>{o}</option>)}
                </select>
              </div>
              <div className="space-y-1">
                <Label>{t("fallback.modelLabel")}</Label>
                <Input placeholder={t("fallback.modelPlaceholder")} value={addModel} onChange={(e) => setAddModel(e.target.value)} />
              </div>
              <div className="flex items-end gap-2">
                <label className="flex items-center gap-2 text-sm">
                  <input type="checkbox" checked={addEnabled} onChange={(e) => setAddEnabled(e.target.checked)} className="h-4 w-4 accent-primary" />
                  Enabled
                </label>
                <Button type="button" size="sm" onClick={addEntry} className="ml-auto"><Plus className="mr-1 h-4 w-4" />{t("fallback.addBtn")}</Button>
              </div>
            </div>
          </div>

          <div className="grid gap-2 sm:grid-cols-2">
            <div className="space-y-1">
              <Label>{t("fallback.fallbackModelLabel")}</Label>
              <Input placeholder={t("fallback.fallbackModelPlaceholder")} value={fallbackModel} onChange={(e) => setFallbackModel(e.target.value)} />
              <p className="text-xs text-muted-foreground">{t("fallback.fallbackModelHelp")}</p>
            </div>
          </div>

          <div className="flex gap-2">
            <Button onClick={handleSave} disabled={saving}><Save className="mr-1 h-4 w-4" />{saving ? t("fallback.saving") : t("fallback.saveBtn")}</Button>
            <span className="text-xs text-muted-foreground self-center">{t("fallback.l5Only")}</span>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
