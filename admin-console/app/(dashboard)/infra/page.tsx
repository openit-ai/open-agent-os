"use client";
import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { apiFetch, getToken, getSetupEffective, getMmConfig, getOlConfig, getLiveInfra, getInfraRegistry, seedUnifiedInfra, type SetupEffective, type LiveInfraItem, type UnifiedInfraRow } from "@/lib/api";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { RefreshCw, Trash2, Pencil, Plus, Server, Activity, Database } from "lucide-react";
import { useI18n } from "@/lib/i18n";
import { SetupTab } from "./setup-tab";
import { McpPanel } from "./mcp-panel";
import { MmPanel } from "./mm-panel";
import { OlPanel } from "./ol-panel";
import { NotionPanel } from "./notion-panel";
import { SlackPanel } from "./slack-panel";
import { OAuthPanel } from "./oauth-panel";
import { SmtpPanel } from "./smtp-panel";

type ProbeType = "http" | "tcp" | string;
interface UnifiedRow {
  id: string;
  name: string;
  service: string;
  display_name?: string;
  host: string;
  port: number;
  health_path: string;
  expected_status?: number;
  status: "healthy" | "unhealthy" | "unknown";
  latency_ms: number | null;
  last_check: string | null;
  last_error?: string | null;
  probe_type?: ProbeType;
  category?: string | null;
  source?: "db" | "live" | "both";
  url?: string;
  db_exists?: boolean;
  live_exists?: boolean;
  // compat alias
  db?: string | null;
  live?: boolean;
}
interface UnifiedResponse {
  probed_at: string;
  count: number;
  items: UnifiedRow[];
  registry?: UnifiedRow[];
}

const SERVICE_OPTIONS = ["control-plane", "memory", "admin-api", "admin-console", "nginx", "mattermost", "hermes", "outline", "postgres", "redis", "execution-gateway", "security"];

function statusVariant(s: string) {
  if (s === "healthy") return "success" as const;
  if (s === "unhealthy") return "danger" as const;
  return "warning" as const;
}
function sourceVariant(src?: string) {
  if (src === "both") return "default" as const;
  if (src === "db") return "secondary" as const;
  if (src === "live") return "outline" as const;
  return "outline" as const;
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
          <TabsTrigger value="live">{t("infra.tabLive")}</TabsTrigger>
          <TabsTrigger value="unified">{t("infra.tabUnified")}</TabsTrigger>
          <TabsTrigger value="setup">{t("infra.tabSetup")}</TabsTrigger>
          <TabsTrigger value="mcp">{t("infra.tabMcp")}</TabsTrigger>
          <TabsTrigger value="mm">{t("infra.tabMm")}</TabsTrigger>
          <TabsTrigger value="ol">{t("infra.tabOl")}</TabsTrigger>
          <TabsTrigger value="notion">{t("infra.tabNotion")}</TabsTrigger>
          <TabsTrigger value="slack">{t("infra.tabSlack")}</TabsTrigger>
          <TabsTrigger value="oauth">{t("infra.tabOAuth")}</TabsTrigger>
          <TabsTrigger value="smtp">{t("infra.tabSmtp")}</TabsTrigger>
        </TabsList>
        <TabsContent value="services"><InfraServices /></TabsContent>
        <TabsContent value="live"><InfraLive /></TabsContent>
        <TabsContent value="unified"><InfraUnified /></TabsContent>
        <TabsContent value="setup"><SetupTab /></TabsContent>
        <TabsContent value="mcp"><McpPanel /></TabsContent>
        <TabsContent value="mm"><MmPanel /></TabsContent>
        <TabsContent value="ol"><OlPanel /></TabsContent>
        <TabsContent value="notion"><NotionPanel /></TabsContent>
        <TabsContent value="slack"><SlackPanel /></TabsContent>
        <TabsContent value="oauth"><OAuthPanel /></TabsContent>
        <TabsContent value="smtp"><SmtpPanel /></TabsContent>
      </Tabs>
    </div>
  );
}

function InfraServices() {
  const router = useRouter();
  const { t } = useI18n();
  const [rows, setRows] = useState<UnifiedRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [probedAt, setProbedAt] = useState<string | null>(null);
  const [probing, setProbing] = useState<string | null>(null);
  const [seeding, setSeeding] = useState(false);
  const [seedMsg, setSeedMsg] = useState<string | null>(null);

  // form state
  const [service, setService] = useState(SERVICE_OPTIONS[0]);
  const [host, setHost] = useState("");
  const [port, setPort] = useState("");
  const [healthPath, setHealthPath] = useState("/health");
  const [editingId, setEditingId] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [formLoading, setFormLoading] = useState(false);

  const fetchUnified = useCallback(async () => {
    try {
      let res: UnifiedResponse;
      try {
        res = await apiFetch<UnifiedResponse>("/v1/infra/unified");
      } catch {
        res = await apiFetch<UnifiedResponse>("/v1/infra/registry");
      }
      const list = res.items ?? res.registry ?? [];
      setRows(list);
      setProbedAt(res.probed_at ?? null);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : t("common.error"));
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    if (!getToken()) { router.replace("/login"); return; }
    fetchUnified();
    ensureConfigServices();
    const id = setInterval(fetchUnified, 15000);
    return () => clearInterval(id);
  }, [fetchUnified, router]);

  function isLiveOnlyRow(id: string | null): boolean {
    if (!id) return false;
    if (id.startsWith("live_")) return true;
    const r = rows.find((x) => x.id === id);
    if (!r) return id.startsWith("live_");
    if (r.db_exists === false) return true;
    if (r.source === "live") return true;
    return false;
  }

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
    // Mattermost / Outline: register from their config tabs so saved
    // connections show up in the services table for monitoring/editing.
    try {
      const mm = await getMmConfig();
      if (mm?.mattermost_url) {
        try {
          const u = new URL(mm.mattermost_url);
          if (u.hostname) {
            desired.push({
              service: "mattermost", host: u.hostname,
              port: u.port ? Number(u.port) : (u.protocol === "https:" ? 443 : 80),
              health_path: "/api/v4/system/ping",
            });
          }
        } catch { /* ignore */ }
      }
    } catch { /* ignore */ }
    try {
      const ol = await getOlConfig();
      if (ol?.outline_url) {
        try {
          const u = new URL(ol.outline_url);
          if (u.hostname) {
            desired.push({
              service: "outline", host: u.hostname,
              port: u.port ? Number(u.port) : (u.protocol === "https:" ? 443 : 80),
              health_path: "/",
            });
          }
        } catch { /* ignore */ }
      }
    } catch { /* ignore */ }
    if (desired.length === 0) return;
    let changed = false;
    try {
      const res = await apiFetch<{ items: { service: string }[] } | { service: string }[]>("/v1/infra");
      const list = Array.isArray(res) ? res : res.items ?? [];
      for (const d of desired) {
        if (!list.some((it) => it.service === d.service)) {
          await apiFetch("/v1/infra", { method: "POST", body: JSON.stringify(d) });
          changed = true;
        }
      }
    } catch { return; }
    if (changed) fetchUnified();
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setFormError(null);
    if (!host || !port) {
      setFormError(t("infra.validationHostPort"));
      return;
    }
    const portNum = Number(port);
    if (!Number.isInteger(portNum) || portNum < 1 || portNum > 65535) {
      setFormError(t("infra.validationPort"));
      return;
    }
    setFormLoading(true);
    try {
      // Non-secret fields only — never send credentials/DSN/password
      const payload = { service, host, port: portNum, health_path: healthPath || "/health" };
      if (editingId) {
        // Live-only rows (e.g. live_outline) have no canonical DB row — 404 on PATCH.
        // Minimal safe flow per task: if live-only, POST/create canonical DB service instead, then refresh.
        // If DB row exists, PATCH as before. Keep L5 auth, tenant isolation server-side.
        if (isLiveOnlyRow(editingId)) {
          // Explicit user action: POST canonical DB service (host/port/health_path only, no secrets)
          await apiFetch("/v1/infra", { method: "POST", body: JSON.stringify(payload) });
        } else {
          // Fast path: DB row exists — PATCH
          try {
            await apiFetch(`/v1/infra/${editingId}`, { method: "PATCH", body: JSON.stringify(payload) });
          } catch (err) {
            const msg = err instanceof Error ? err.message : String(err);
            // Fallback if backend reports 404 but row was thought to be DB-backed (race)
            if (/404|not found/i.test(msg)) {
              try {
                await apiFetch("/v1/infra/upsert", { method: "POST", body: JSON.stringify(payload) });
              } catch {
                await apiFetch("/v1/infra", { method: "POST", body: JSON.stringify(payload) });
              }
            } else {
              throw err;
            }
          }
        }
      } else {
        await apiFetch("/v1/infra", { method: "POST", body: JSON.stringify(payload) });
      }
      setHost("");
      setPort("");
      setHealthPath("/health");
      setEditingId(null);
      await fetchUnified();
    } catch (e) {
      setFormError(e instanceof Error ? e.message : t("infra.saveFailed"));
    } finally {
      setFormLoading(false);
    }
  }

  function startEdit(row: UnifiedRow) {
    // Single registry: allow editing any row (db or live). Live-only edits will POST/create on Update.
    setEditingId(row.id);
    setService(row.service || row.name);
    setHost(row.host);
    setPort(String(row.port));
    setHealthPath(row.health_path);
  }

  async function handleDelete(id: string) {
    if (!confirm(t("infra.deleteConfirm"))) return;
    try {
      await apiFetch(`/v1/infra/${id}`, { method: "DELETE" });
      await fetchUnified();
    } catch (e) {
      alert(e instanceof Error ? e.message : t("common.error"));
    }
  }

  async function handleProbe(id: string) {
    setProbing(id);
    try {
      await apiFetch(`/v1/infra/${id}/probe`, { method: "POST" });
      await fetchUnified();
    } catch (e) {
      alert(e instanceof Error ? e.message : t("infra.probeFailed"));
    } finally {
      setProbing(null);
    }
  }

  async function handleSeed() {
    if (!confirm(t("infra.seedConfirm"))) return;
    setSeeding(true);
    setSeedMsg(null);
    try {
      const res = await apiFetch<{ created_count: number; skipped_count: number; created: unknown[]; skipped: string[] }>(
        "/v1/infra/seed",
        { method: "POST" }
      );
      setSeedMsg(t("infra.seedResult").replace("{created}", String(res.created_count)).replace("{skipped}", String(res.skipped_count)));
      await fetchUnified();
    } catch (e) {
      setSeedMsg(e instanceof Error ? e.message : t("infra.seedFailed"));
    } finally {
      setSeeding(false);
    }
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <h1 className="text-2xl font-semibold">{t("infra.title")}</h1>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" onClick={() => fetchUnified()}>
            <RefreshCw className="mr-1 h-4 w-4" />
            {t("common.refresh")}
          </Button>
          <Button variant="secondary" size="sm" onClick={handleSeed} disabled={seeding} title={t("infra.seedHint")}>
            <Database className="mr-1 h-4 w-4" />
            {seeding ? t("infra.seeding") : t("infra.seedButton")}
          </Button>
        </div>
      </div>

      {seedMsg && <p className="text-sm text-muted-foreground" role="status">{seedMsg}</p>}
      {error && <p className="text-sm text-[#DC2626]" role="alert">{error}</p>}

      {/* Single unified registry table — DB desired + live probe merged */}
      <Card className="border-slate-200 dark:border-slate-800">
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            <Activity className="h-4 w-4 text-emerald-600" />
            {t("infra.unifiedTitle")}
            <Badge variant="outline" className="ml-2 font-normal">
              {t("infra.unifiedCount").replace("{count}", String(rows.length))}
            </Badge>
            {probedAt && (
              <span className="ml-auto text-xs font-normal text-muted-foreground">
                {t("infra.liveProbedAt")}: {new Date(probedAt).toLocaleString(langLocale())}
              </span>
            )}
          </CardTitle>
          <CardDescription className="text-xs">
            {t("infra.unifiedDesc")} · {t("infra.liveNote")} · Probe: <span className="font-mono">http</span>=GET health_path, <span className="font-mono">tcp</span>=TCP connect (PostgreSQL/Redis — no credentials stored)
          </CardDescription>
        </CardHeader>
        <CardContent className="p-0">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>{t("infra.service")}</TableHead>
                <TableHead>host:port</TableHead>
                <TableHead>{t("infra.healthPath")}</TableHead>
                <TableHead>Probe</TableHead>
                <TableHead>{t("infra.source")}</TableHead>
                <TableHead>{t("infra.status")}</TableHead>
                <TableHead>{t("infra.latency")}</TableHead>
                <TableHead>{t("infra.lastCheck")}</TableHead>
                <TableHead className="text-right">{t("common.actions")}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {loading ? (
                <TableRow>
                  <TableCell colSpan={9} className="text-center text-muted-foreground">
                    {t("common.loading")}
                  </TableCell>
                </TableRow>
              ) : rows.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={9} className="text-center text-muted-foreground">
                    {t("infra.noData")}
                  </TableCell>
                </TableRow>
              ) : (
                rows.map((it) => (
                  <TableRow key={it.id} className={it.source === "live" ? "bg-amber-50/20 dark:bg-amber-950/10" : it.source === "both" ? "bg-emerald-50/15 dark:bg-emerald-950/10" : ""}>
                    <TableCell className="font-medium">
                      <span className="font-mono text-xs">{it.display_name || it.service || it.name}</span>
                      {(() => {
                        const dn = (it.display_name || "").trim().toLowerCase();
                        const sn = (it.service || it.name || "").trim().toLowerCase();
                        if (!dn || dn === sn) return null;
                        return <span className="ml-2 text-[10px] text-muted-foreground">{it.service || it.name}</span>;
                      })()}
                      {it.category && (
                        <Badge variant="outline" className="ml-1 text-[10px] font-normal">
                          {it.category}
                        </Badge>
                      )}
                    </TableCell>
                    <TableCell className="font-mono text-xs">
                      {it.host}:{it.port}
                    </TableCell>
                    <TableCell className="font-mono text-xs">{it.health_path}</TableCell>
                    <TableCell>
                      <Badge variant="outline" className={it.probe_type === "tcp" ? "border-amber-300 text-amber-700" : "border-sky-300 text-sky-700"}>
                        {it.probe_type || "http"}
                      </Badge>
                    </TableCell>
                    <TableCell>
                      <Badge variant={sourceVariant(it.source)} className="text-[11px]">
                        {it.source || (it.db_exists ? (it.live_exists ? "both" : "db") : "live")}
                      </Badge>
                    </TableCell>
                    <TableCell>
                      <Badge variant={statusVariant(it.status)}>{it.status}</Badge>
                    </TableCell>
                    <TableCell>{it.latency_ms != null ? `${it.latency_ms}ms` : "-"}</TableCell>
                    <TableCell className="text-xs">{it.last_check ? new Date(it.last_check).toLocaleString(langLocale()) : "-"}</TableCell>
                    <TableCell>
                      <div className="flex justify-end gap-1">
                        <Button variant="outline" size="sm" disabled={probing === it.id} onClick={() => handleProbe(it.id)}>
                          {probing === it.id ? "..." : t("infra.probe")}
                        </Button>
                        <Button variant="ghost" size="icon" onClick={() => startEdit(it)} aria-label={t("common.edit")}>
                          <Pencil className="h-4 w-4" />
                        </Button>
                        {it.db_exists !== false && !it.id.startsWith("live_") && (
                          <Button variant="ghost" size="icon" onClick={() => handleDelete(it.id)} aria-label={t("common.delete")}>
                            <Trash2 className="h-4 w-4" />
                          </Button>
                        )}
                      </div>
                    </TableCell>
                  </TableRow>
                ))
              )}
            </TableBody>
          </Table>
          <p className="px-4 py-2 text-[11px] text-muted-foreground">
            {t("infra.unifiedHint")} · Source: <span className="font-mono">both</span>=DB+live, <span className="font-mono">db</span>=DB only, <span className="font-mono">live</span>=yet-unregistered (use 등록/수정 to persist).
          </p>
        </CardContent>
      </Card>

      {/* 등록/수정 폼 — single registry writer (DB) */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">
            {!editingId ? t("infra.formTitleCreate") : isLiveOnlyRow(editingId) ? `${t("infra.formTitleEdit")} · ${t("infra.register")}` : t("infra.formTitleEdit")}
            {editingId && isLiveOnlyRow(editingId) && (
              <Badge variant="outline" className="ml-2 border-amber-300 text-amber-700 text-[11px]">live → {t("infra.register")}</Badge>
            )}
          </CardTitle>
          <CardDescription className="text-xs">
            {editingId && isLiveOnlyRow(editingId) ? `${t("infra.formDescUnified")} — ${t("infra.liveNote")} · live_* rows are not in DB yet; clicking ${t("infra.register")}/${t("infra.update")} will POST /v1/infra (no secrets) to persist.` : t("infra.formDescUnified")}
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleSubmit} className="grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
            <div className="space-y-1">
              <Label htmlFor="service">{t("infra.service")}</Label>
              <select id="service" value={service} onChange={(e) => setService(e.target.value)} className="flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm">
                {SERVICE_OPTIONS.map((o) => (
                  <option key={o} value={o}>{o}</option>
                ))}
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
              <Button type="submit" disabled={formLoading} className="flex-1" title={editingId && isLiveOnlyRow(editingId) ? "Register live row to DB — explicit click required" : undefined}>
                <Plus className="mr-1 h-4 w-4" />
                {!editingId ? t("infra.register") : isLiveOnlyRow(editingId) ? t("infra.register") : t("infra.update")}
              </Button>
              {editingId && (
                <Button type="button" variant="outline" onClick={() => { setEditingId(null); setHost(""); setPort(""); setHealthPath("/health"); }}>
                  {t("infra.cancel")}
                </Button>
              )}
            </div>
          </form>
          {formError && <p className="mt-2 text-sm text-[#DC2626]" role="alert">{formError}</p>}
        </CardContent>
      </Card>

      <p className="text-xs text-muted-foreground">{t("infra.autoRefreshNote")} · {t("infra.liveNote")}</p>
    </div>
  );
}

function langLocale(): string {
  try {
    const v = typeof window !== "undefined" ? localStorage.getItem("oaos_lang") : null;
    return v === "ko" ? "ko-KR" : "en-US";
  } catch {
    return "en-US";
  }
}

/* ── Runtime Config plane merge (P0, additive): read-only live inventory ── */
function InfraLive() {
  const router = useRouter();
  const { t } = useI18n();
  const [items, setItems] = useState<LiveInfraItem[]>([]);
  const [probedAt, setProbedAt] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchLive = useCallback(async () => {
    try {
      const res = await getLiveInfra();
      setItems(res.items ?? []);
      setProbedAt(res.probed_at ?? null);
      setError(null);
    } catch (e) { setError(e instanceof Error ? e.message : t("common.error")); }
    finally { setLoading(false); }
  }, [t]);

  useEffect(() => {
    if (!getToken()) { router.replace("/login"); return; }
    fetchLive();
    const id = setInterval(fetchLive, 15000);
    return () => clearInterval(id);
  }, [fetchLive, router]);

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-lg font-semibold">{t("infra.liveTitle")}</h2>
          <p className="text-xs text-muted-foreground">{t("infra.liveDesc")} · {t("infra.liveCount").replace("{count}", String(items.length))}{probedAt ? ` · ${t("infra.liveProbedAt")}: ${new Date(probedAt).toLocaleString(langLocale())}` : ""}</p>
        </div>
        <Button variant="outline" size="sm" onClick={fetchLive}><RefreshCw className="mr-1 h-4 w-4" />{t("common.refresh")}</Button>
      </div>
      {error && <p className="text-sm text-[#DC2626]" role="alert">{error}</p>}
      <Card>
        <CardContent className="p-0">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>{t("infra.service")}</TableHead>
                <TableHead>host:port</TableHead>
                <TableHead>{t("infra.status")}</TableHead>
                <TableHead>{t("infra.latency")}</TableHead>
                <TableHead>{t("infra.lastCheck")}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {loading ? (
                <TableRow><TableCell colSpan={5} className="text-center text-muted-foreground">{t("common.loading")}</TableCell></TableRow>
              ) : items.length === 0 ? (
                <TableRow><TableCell colSpan={5} className="text-center text-muted-foreground">{t("infra.liveEmpty")}</TableCell></TableRow>
              ) : items.map((it) => (
                <TableRow key={it.id}>
                  <TableCell className="font-medium">{it.display_name || it.service || it.name}</TableCell>
                  <TableCell className="font-mono text-xs">{it.host}:{it.port}</TableCell>
                  <TableCell><Badge variant={statusVariant(String(it.status))}>{String(it.status)}</Badge></TableCell>
                  <TableCell>{it.latency_ms != null ? `${it.latency_ms}ms` : "-"}</TableCell>
                  <TableCell className="text-xs">{it.last_check ? new Date(it.last_check).toLocaleString(langLocale()) : "-"}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
      <p className="text-xs text-muted-foreground">{t("infra.liveNote")}</p>
    </div>
  );
}

/* ── Runtime Config plane merge (P0, additive): unified registry + seed ── */
function InfraUnified() {
  const router = useRouter();
  const { t } = useI18n();
  const [rows, setRows] = useState<UnifiedInfraRow[]>([]);
  const [probedAt, setProbedAt] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [seeding, setSeeding] = useState(false);
  const [seedMsg, setSeedMsg] = useState<string | null>(null);

  const fetchRegistry = useCallback(async () => {
    try {
      const res = await getInfraRegistry();
      setRows(res.items ?? []);
      setProbedAt(res.probed_at ?? null);
      setError(null);
    } catch (e) { setError(e instanceof Error ? e.message : t("common.error")); }
    finally { setLoading(false); }
  }, [t]);

  useEffect(() => {
    if (!getToken()) { router.replace("/login"); return; }
    fetchRegistry();
    const id = setInterval(fetchRegistry, 15000);
    return () => clearInterval(id);
  }, [fetchRegistry, router]);

  async function handleSeed() {
    if (!confirm(t("infra.seedConfirm"))) return;
    setSeeding(true);
    setSeedMsg(null);
    try {
      const res = await seedUnifiedInfra();
      setSeedMsg(t("infra.seedResult").replace("{created}", String(res.created_count)).replace("{skipped}", String(res.skipped_count)));
      await fetchRegistry();
    } catch (e) { setSeedMsg(e instanceof Error ? e.message : t("infra.seedFailed")); }
    finally { setSeeding(false); }
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-lg font-semibold">{t("infra.unifiedTitle")}</h2>
          <p className="text-xs text-muted-foreground">{t("infra.unifiedDesc")} · {t("infra.unifiedCount").replace("{count}", String(rows.length))}{probedAt ? ` · ${t("infra.liveProbedAt")}: ${new Date(probedAt).toLocaleString(langLocale())}` : ""}</p>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" size="sm" onClick={fetchRegistry}><RefreshCw className="mr-1 h-4 w-4" />{t("common.refresh")}</Button>
          <Button variant="secondary" size="sm" onClick={handleSeed} disabled={seeding} title={t("infra.seedHint")}>
            <Plus className="mr-1 h-4 w-4" />{seeding ? t("infra.seeding") : t("infra.seedButton")}
          </Button>
        </div>
      </div>
      {error && <p className="text-sm text-[#DC2626]" role="alert">{error}</p>}
      {seedMsg && <p className="text-sm text-muted-foreground" role="status">{seedMsg}</p>}
      <Card>
        <CardContent className="p-0">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>{t("infra.service")}</TableHead>
                <TableHead>host:port</TableHead>
                <TableHead>{t("infra.source")}</TableHead>
                <TableHead>{t("infra.status")}</TableHead>
                <TableHead>{t("infra.latency")}</TableHead>
                <TableHead>{t("infra.lastCheck")}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {loading ? (
                <TableRow><TableCell colSpan={6} className="text-center text-muted-foreground">{t("common.loading")}</TableCell></TableRow>
              ) : rows.length === 0 ? (
                <TableRow><TableCell colSpan={6} className="text-center text-muted-foreground">{t("infra.noData")}</TableCell></TableRow>
              ) : rows.map((it) => (
                <TableRow key={it.id}>
                  <TableCell className="font-medium">{it.display_name || it.service || it.name}</TableCell>
                  <TableCell className="font-mono text-xs">{it.host}:{it.port}</TableCell>
                  <TableCell className="font-mono text-xs">{it.source ?? "-"}</TableCell>
                  <TableCell><Badge variant={statusVariant(String(it.status))}>{String(it.status)}</Badge></TableCell>
                  <TableCell>{it.latency_ms != null ? `${it.latency_ms}ms` : "-"}</TableCell>
                  <TableCell className="text-xs">{it.last_check ? new Date(it.last_check).toLocaleString(langLocale()) : "-"}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
      <p className="text-xs text-muted-foreground">{t("infra.unifiedHint")}</p>
    </div>
  );
}
