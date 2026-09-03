"use client";
import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { getToken, getEmbeddingConfig, updateEmbeddingConfig, type EmbeddingConfig } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { Database, Loader2 } from "lucide-react";

const PROVIDERS = ["ollama", "openai-compatible", "fake"] as const;

export default function EmbeddingPage() {
  const router = useRouter();
  const { t } = useI18n();
  const [cfg, setCfg] = useState<EmbeddingConfig | null>(null);
  const [provider, setProvider] = useState<string>("ollama");
  const [model, setModel] = useState("");
  const [dim, setDim] = useState("1024");
  const [apiUrl, setApiUrl] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);

  const fetchCfg = useCallback(async () => {
    setError(null);
    try {
      const res = await getEmbeddingConfig();
      setCfg(res);
      setProvider(res.provider);
      setModel(res.model);
      setDim(String(res.dim));
      setApiUrl(res.api_url);
    } catch (e) {
      setError(e instanceof Error ? e.message : t("embedding.loadFailed"));
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    if (!getToken()) { router.replace("/login"); return; }
    fetchCfg();
  }, [fetchCfg, router]);

  const save = async () => {
    setSaving(true);
    setError(null);
    setMsg(null);
    try {
      const res = await updateEmbeddingConfig({
        provider,
        model: model.trim(),
        dim: Number(dim),
        api_url: apiUrl.trim(),
      });
      setCfg(res);
      setMsg(t("embedding.saved"));
    } catch (e) {
      setError(e instanceof Error ? e.message : t("embedding.saveFailed"));
    } finally {
      setSaving(false);
    }
  };

  if (loading) return <div className="p-6 text-sm text-muted-foreground">{t("common.loading")}</div>;

  return (
    <div className="space-y-6 p-6">
      <div>
        <h1 className="flex items-center gap-2 text-2xl font-semibold"><Database className="h-6 w-6" /> {t("embedding.title")}</h1>
        <p className="text-sm text-muted-foreground">{t("embedding.subtitle")}</p>
      </div>
      {error && <Card className="border-red-500"><CardContent className="pt-4 text-sm text-red-600">{error}</CardContent></Card>}
      {msg && <Card className="border-green-500"><CardContent className="pt-4 text-sm text-green-700">{msg}</CardContent></Card>}

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            {t("embedding.title")}
            {cfg && <Badge variant="secondary">source: {cfg.source}</Badge>}
            {cfg && (cfg.applied
              ? <Badge className="bg-green-600 text-white">{t("embedding.appliedYes")}</Badge>
              : <Badge className="bg-amber-500 text-white">{t("embedding.appliedNo")}</Badge>)}
          </CardTitle>
          {cfg?.note && <CardDescription>{cfg.note}</CardDescription>}
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-4 md:grid-cols-2">
            <div className="space-y-1">
              <Label htmlFor="emb-provider">{t("embedding.provider")}</Label>
              <select
                id="emb-provider"
                value={provider}
                onChange={(e) => setProvider(e.target.value)}
                className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
              >
                {PROVIDERS.map((p) => <option key={p} value={p}>{p}</option>)}
              </select>
            </div>
            <div className="space-y-1">
              <Label htmlFor="emb-model">{t("embedding.model")}</Label>
              <Input id="emb-model" value={model} onChange={(e) => setModel(e.target.value)} />
            </div>
            <div className="space-y-1">
              <Label htmlFor="emb-dim">{t("embedding.dim")}</Label>
              <Input id="emb-dim" inputMode="numeric" value={dim} onChange={(e) => setDim(e.target.value)} />
            </div>
            <div className="space-y-1">
              <Label htmlFor="emb-url">{t("embedding.apiUrl")}</Label>
              <Input id="emb-url" value={apiUrl} onChange={(e) => setApiUrl(e.target.value)} placeholder="http://127.0.0.1:11434" />
            </div>
          </div>
          <div className="flex gap-2">
            <Button onClick={save} disabled={saving}>
              {saving && <Loader2 className="mr-1 h-4 w-4 animate-spin" />}{t("embedding.save")}
            </Button>
            <Button variant="outline" onClick={fetchCfg}>{t("common.refresh")}</Button>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
