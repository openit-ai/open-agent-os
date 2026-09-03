"use client";
import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { apiFetch, getToken, getSetupEffective, type SetupEffective } from "@/lib/api";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { RefreshCw, Trash2, Pencil, Plus, Server } from "lucide-react";
import { useI18n } from "@/lib/i18n";
import { SetupTab } from "./setup-tab";
import { McpPanel } from "./mcp-panel";
import { MmPanel } from "./mm-panel";

interface InfraItem {
  id: string; service: string; host: string; port: number; health_path: string;
  status: "healthy" | "unhealthy" | "unknown"; latency_ms: number | null; last_check: string | null; last_error?: string | null;
}

const SERVICE_OPTIONS = ["control-plane", "execution-gateway", "security", "hermes", "mattermost", "outline", "postgres", "redis"];

function statusVariant(s: string) {
  if (s === "healthy") return "success" as const;
  if (s === "unhealthy") return "danger" as const;
  return "warning" as const;
}

export default function InfraPage() {
  const { t } = useI18n();
  return (
    <div className="space-y-6 p-6">
      <div>
        <h1 className="flex items-center gap-2 text-2xl font-semibold"><Server className="h-6 w-6" /> {t("infra.title")}</h1>
      </div>
      <Tabs defaultValue="services">
        <TabsList>
          <TabsTrigger value="services">{t("infra.tabServices")}</TabsTrigger>
          <TabsTrigger value="setup">{t("infra.tabSetup")}</TabsTrigger>
          <TabsTrigger value="mcp">{t("infra.tabMcp")}</TabsTrigger>
          <TabsTrigger value="mm">{t("infra.tabMm")}</TabsTrigger>
        </TabsList>
        <TabsContent value="services"><InfraServices /></TabsContent>
        <TabsContent value="setup"><SetupTab /></TabsContent>
        <TabsContent value="mcp"><McpPanel /></TabsContent>
        <TabsContent value="mm"><MmPanel /></TabsContent>
      </Tabs>
    </div>
  );
}

function InfraServices() {
  const router = useRouter();
  const { t } = useI18n();
  const [items, setItems] = useState<InfraItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [probing, setProbing] = useState<string | null>(null);

  // form state
  const [service, setService] = useState(SERVICE_OPTIONS[0]);
  const [host, setHost] = useState("");
  const [port, setPort] = useState("");
  const [healthPath, setHealthPath] = useState("/health");
  const [editingId, setEditingId] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [formLoading, setFormLoading] = useState(false);

  const fetchList = useCallback(async () => {
    try {
      const res = await apiFetch<{ items: InfraItem[] } | InfraItem[]>("/v1/infra");
      const list: InfraItem[] = Array.isArray(res) ? res : (res as { items: InfraItem[] }).items ?? [];
      setItems(list);
      setError(null);
    } catch (e) { setError(e instanceof Error ? e.message : t("common.error")); }
    finally { setLoading(false); }
  }, [t]);

  useEffect(() => {
    if (!getToken()) { router.replace("/login"); return; }
    fetchList();
    ensureConfigServices();
    const id = setInterval(fetchList, 15000);
    return () => clearInterval(id);
  }, [fetchList, router]);

  // Register DB/Redis/ACP from server-side effective config into the
  // infra list (idempotent by service name) so setup results are
  // monitored/edited in the services table instead of a separate card.
  async function ensureConfigServices() {
    let eff: SetupEffective | null = null;
    try { eff = await getSetupEffective(); } catch { return; }
    if (!eff) return;
    const desired: { service: string; host: string; port: number; health_path: string }[] = [];
    if (eff.db.configured && eff.db.host) {
      desired.push({ service: "postgres", host: eff.db.host, port: eff.db.port ?? 5432, health_path: "" });
    }
    if (eff.redis.configured && eff.redis.host) {
      desired.push({ service: "redis", host: eff.redis.host, port: eff.redis.port ?? 6379, health_path: "" });
    }
    if (eff.hermes.base_url) {
      try {
        const u = new URL(eff.hermes.base_url);
        if (u.hostname) {
          desired.push({ service: "hermes", host: u.hostname, port: u.port ? Number(u.port) : 80, health_path: "/health" });
        }
      } catch { /* ignore unparseable base_url */ }
    }
    if (desired.length === 0) return;
    let changed = false;
    try {
      const res = await apiFetch<{ items: InfraItem[] } | InfraItem[]>("/v1/infra");
      const list: InfraItem[] = Array.isArray(res) ? res : (res as { items: InfraItem[] }).items ?? [];
      for (const d of desired) {
        if (!list.some((it) => it.service === d.service)) {
          await apiFetch("/v1/infra", { method: "POST", body: JSON.stringify(d) });
          changed = true;
        }
      }
    } catch { return; }
    if (changed) fetchList();
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setFormError(null);
    if (!host || !port) { setFormError(t("infra.validationHostPort")); return; }
    const portNum = Number(port);
    if (!Number.isInteger(portNum) || portNum < 1 || portNum > 65535) { setFormError(t("infra.validationPort")); return; }
    setFormLoading(true);
    try {
      const payload = { service, host, port: portNum, health_path: healthPath || "/health" };
      if (editingId) {
        await apiFetch(`/v1/infra/${editingId}`, { method: "PATCH", body: JSON.stringify(payload) });
      } else {
        await apiFetch("/v1/infra", { method: "POST", body: JSON.stringify(payload) });
      }
      setHost(""); setPort(""); setHealthPath("/health"); setEditingId(null);
      await fetchList();
    } catch (e) { setFormError(e instanceof Error ? e.message : t("infra.saveFailed")); }
    finally { setFormLoading(false); }
  }

  function startEdit(item: InfraItem) {
    setEditingId(item.id);
    setService(item.service);
    setHost(item.host);
    setPort(String(item.port));
    setHealthPath(item.health_path);
  }

  async function handleDelete(id: string) {
    if (!confirm(t("infra.deleteConfirm"))) return;
    try { await apiFetch(`/v1/infra/${id}`, { method: "DELETE" }); await fetchList(); }
    catch (e) { alert(e instanceof Error ? e.message : t("common.error")); }
  }

  async function handleProbe(id: string) {
    setProbing(id);
    try { await apiFetch(`/v1/infra/${id}/probe`, { method: "POST" }); await fetchList(); }
    catch (e) { alert(e instanceof Error ? e.message : t("infra.probeFailed")); }
    finally { setProbing(null); }
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">{t("infra.title")}</h1>
        <Button variant="outline" size="sm" onClick={fetchList}><RefreshCw className="mr-1 h-4 w-4" />{t("common.refresh")}</Button>
      </div>

      {error && <p className="text-sm text-[#DC2626]" role="alert">{error}</p>}

      {/* 등록/수정 폼 */}
      <Card>
        <CardHeader><CardTitle className="text-base">{editingId ? t("infra.formTitleEdit") : t("infra.formTitleCreate")}</CardTitle></CardHeader>
        <CardContent>
          <form onSubmit={handleSubmit} className="grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
            <div className="space-y-1">
              <Label htmlFor="service">{t("infra.service")}</Label>
              <select id="service" value={service} onChange={(e) => setService(e.target.value)} className="flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm">
                {SERVICE_OPTIONS.map((o) => <option key={o} value={o}>{o}</option>)}
              </select>
            </div>
            <div className="space-y-1">
              <Label htmlFor="host">{t("infra.host")}</Label>
              <Input id="host" placeholder={t("infra.hostPlaceholder")} value={host} onChange={(e) => setHost(e.target.value)} required />
            </div>
            <div className="space-y-1">
              <Label htmlFor="port">{t("infra.port")}</Label>
              <Input id="port" type="number" placeholder={t("infra.portPlaceholder")} value={port} onChange={(e) => setPort(e.target.value)} required />
            </div>
            <div className="space-y-1">
              <Label htmlFor="health_path">{t("infra.healthPath")}</Label>
              <Input id="health_path" placeholder={t("infra.healthPathPlaceholder")} value={healthPath} onChange={(e) => setHealthPath(e.target.value)} />
            </div>
            <div className="flex items-end gap-2">
              <Button type="submit" disabled={formLoading} className="flex-1"><Plus className="mr-1 h-4 w-4" />{editingId ? t("infra.update") : t("infra.register")}</Button>
              {editingId && <Button type="button" variant="outline" onClick={() => { setEditingId(null); setHost(""); setPort(""); setHealthPath("/health"); }}>{t("infra.cancel")}</Button>}
            </div>
          </form>
          {formError && <p className="mt-2 text-sm text-[#DC2626]" role="alert">{formError}</p>}
        </CardContent>
      </Card>

      {/* 목록 테이블 */}
      <Card>
        <CardContent className="p-0">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>{t("infra.service")}</TableHead>
                <TableHead>host:port</TableHead>
                <TableHead>{t("infra.healthPath")}</TableHead>
                <TableHead>{t("infra.status")}</TableHead>
                <TableHead>{t("infra.latency")}</TableHead>
                <TableHead>{t("infra.lastCheck")}</TableHead>
                <TableHead className="text-right">{t("common.actions")}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {loading ? (
                <TableRow><TableCell colSpan={7} className="text-center text-muted-foreground">{t("common.loading")}</TableCell></TableRow>
              ) : items.length === 0 ? (
                <TableRow><TableCell colSpan={7} className="text-center text-muted-foreground">{t("infra.noData")}</TableCell></TableRow>
              ) : items.map((it) => (
                <TableRow key={it.id}>
                  <TableCell className="font-medium">{it.service}</TableCell>
                  <TableCell className="font-mono text-xs">{it.host}:{it.port}</TableCell>
                  <TableCell className="font-mono text-xs">{it.health_path}</TableCell>
                  <TableCell><Badge variant={statusVariant(it.status)}>{it.status}</Badge></TableCell>
                  <TableCell>{it.latency_ms != null ? `${it.latency_ms}ms` : "-"}</TableCell>
                  <TableCell className="text-xs">{it.last_check ? new Date(it.last_check).toLocaleString(langLocale()) : "-"}</TableCell>
                  <TableCell>
                    <div className="flex justify-end gap-1">
                      <Button variant="outline" size="sm" disabled={probing === it.id} onClick={() => handleProbe(it.id)}>{probing === it.id ? "..." : t("infra.probe")}</Button>
                      <Button variant="ghost" size="icon" onClick={() => startEdit(it)} aria-label={t("common.edit")}><Pencil className="h-4 w-4" /></Button>
                      <Button variant="ghost" size="icon" onClick={() => handleDelete(it.id)} aria-label={t("common.delete")}><Trash2 className="h-4 w-4" /></Button>
                    </div>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
      <p className="text-xs text-muted-foreground">{t("infra.autoRefreshNote")}</p>
    </div>
  );
}

function langLocale(): string {
  try {
    const v = typeof window !== "undefined" ? localStorage.getItem("oaos_lang") : null;
    return v === "ko" ? "ko-KR" : "en-US";
  } catch { return "en-US"; }
}
