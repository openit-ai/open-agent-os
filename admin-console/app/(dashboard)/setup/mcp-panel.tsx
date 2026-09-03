"use client";
import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { listMcpServers, createMcpServer, updateMcpServer, deleteMcpServer, testMcpServer, type McpServer, type McpServerTestResult } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { Boxes, Loader2, Plus, Trash2 } from "lucide-react";

const TRANSPORTS = ["stdio", "sse", "streamable-http"] as const;

export function McpPanel() {
  const { t } = useI18n();
  const [servers, setServers] = useState<McpServer[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [formOpen, setFormOpen] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [transport, setTransport] = useState<string>("streamable-http");
  const [url, setUrl] = useState("");
  const [command, setCommand] = useState("");
  const [args, setArgs] = useState("");
  const [headers, setHeaders] = useState("");
  const [saving, setSaving] = useState(false);
  const [testRes, setTestRes] = useState<Record<string, McpServerTestResult>>({});
  const [testing, setTesting] = useState<string | null>(null);

  const fetchServers = useCallback(async () => {
    setError(null);
    try {
      setServers(await listMcpServers());
    } catch (e) {
      setError(e instanceof Error ? e.message : t("mcp.loadFailed"));
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => { fetchServers(); }, [fetchServers]);

  const openAdd = () => {
    setEditing(null); setName(""); setTransport("streamable-http");
    setUrl(""); setCommand(""); setArgs(""); setHeaders("");
    setFormOpen(true);
  };

  const openEdit = (s: McpServer) => {
    setEditing(s.name); setName(s.name); setTransport(s.transport);
    setUrl(s.url ?? ""); setCommand(s.command ?? ""); setArgs((s.args ?? []).join(", "));
    setHeaders(""); setFormOpen(true);
  };

  const parseHeaders = (): Record<string, string> | undefined => {
    const out: Record<string, string> = {};
    for (const line of headers.split("\n")) {
      const l = line.trim();
      if (!l) continue;
      const i = l.indexOf("=");
      if (i < 0) throw new Error(`bad header line: ${l}`);
      out[l.slice(0, i).trim()] = l.slice(i + 1).trim();
    }
    return Object.keys(out).length ? out : undefined;
  };

  const save = async () => {
    setSaving(true);
    setError(null);
    setMsg(null);
    try {
      const payload = {
        name: name.trim().toLowerCase(),
        transport,
        ...(url.trim() ? { url: url.trim() } : {}),
        ...(command.trim() ? { command: command.trim() } : {}),
        ...(args.trim() ? { args: args.split(",").map((a) => a.trim()).filter(Boolean) } : {}),
        ...(() => { const h = parseHeaders(); return h ? { headers: h } : {}; })(),
      };
      if (editing) await updateMcpServer(editing, payload);
      else await createMcpServer(payload);
      setFormOpen(false);
      setMsg(t("mcp.saved"));
      await fetchServers();
    } catch (e) {
      setError(e instanceof Error ? e.message : "save failed");
    } finally {
      setSaving(false);
    }
  };

  const remove = async (n: string) => {
    if (!confirm(t("mcp.deleteConfirm"))) return;
    try {
      await deleteMcpServer(n);
      await fetchServers();
    } catch (e) {
      setError(e instanceof Error ? e.message : "delete failed");
    }
  };

  const test = async (n: string) => {
    setTesting(n);
    try {
      const res = await testMcpServer(n);
      setTestRes((p) => ({ ...p, [n]: res }));
    } catch (e) {
      setError(e instanceof Error ? e.message : "test failed");
    } finally {
      setTesting(null);
    }
  };

  if (loading) return <div className="p-6 text-sm text-muted-foreground">{t("common.loading")}</div>;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-semibold"><Boxes className="h-6 w-6" /> {t("mcp.title")}</h1>
          <p className="text-sm text-muted-foreground">{t("mcp.subtitle")}</p>
        </div>
        <Button onClick={openAdd}><Plus className="mr-2 h-4 w-4" />{t("mcp.add")}</Button>
      </div>
      {error && <Card className="border-red-500"><CardContent className="pt-4 text-sm text-red-600">{error}</CardContent></Card>}
      {msg && <Card className="border-green-500"><CardContent className="pt-4 text-sm text-green-700">{msg}</CardContent></Card>}

      {formOpen && (
        <Card>
          <CardHeader><CardTitle>{editing ?? t("mcp.add")}</CardTitle></CardHeader>
          <CardContent className="grid gap-3 md:grid-cols-2">
            <div>
              <Label>{t("mcp.name")}</Label>
              <Input value={name} disabled={!!editing} onChange={(e) => setName(e.target.value)} placeholder="outline" />
            </div>
            <div>
              <Label>{t("mcp.transport")}</Label>
              <select className="w-full rounded border p-2 text-sm" value={transport} onChange={(e) => setTransport(e.target.value)}>
                {TRANSPORTS.map((tr) => <option key={tr} value={tr}>{tr}</option>)}
              </select>
            </div>
            <div>
              <Label>{t("mcp.url")}</Label>
              <Input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://…" />
            </div>
            <div>
              <Label>{t("mcp.command")}</Label>
              <Input value={command} onChange={(e) => setCommand(e.target.value)} placeholder="outline-mcp" />
            </div>
            <div>
              <Label>{t("mcp.args")}</Label>
              <Input value={args} onChange={(e) => setArgs(e.target.value)} />
            </div>
            <div>
              <Label>{t("mcp.headers")}</Label>
              <Input value={headers} onChange={(e) => setHeaders(e.target.value)} placeholder="Authorization=Bearer …" />
            </div>
            <div className="flex gap-2 md:col-span-2">
              <Button onClick={save} disabled={saving}>{saving && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}{t("mcp.save")}</Button>
              <Button variant="outline" onClick={() => setFormOpen(false)}>{t("mcp.cancel")}</Button>
            </div>
          </CardContent>
        </Card>
      )}

      {servers.length === 0 && !formOpen && (
        <Card><CardContent className="pt-4 text-sm text-muted-foreground">{t("mcp.empty")}</CardContent></Card>
      )}

      <div className="grid gap-3 md:grid-cols-2">
        {servers.map((s) => (
          <Card key={s.name}>
            <CardHeader>
              <CardTitle className="flex items-center gap-2 text-base">
                {s.name}
                <Badge variant="secondary">{s.transport}</Badge>
              </CardTitle>
              <CardDescription className="break-all text-xs">
                {[s.url, s.command, (s.args ?? []).join(" ")].filter(Boolean).join(" ")}
                {s.headers_set && s.headers_set.length > 0 && <div>headers: {s.headers_set.join(", ")} (masked)</div>}
              </CardDescription>
            </CardHeader>
            <CardContent className="flex flex-wrap items-center gap-2">
              <Button size="sm" variant="outline" onClick={() => openEdit(s)}>Edit</Button>
              <Button size="sm" variant="outline" onClick={() => test(s.name)} disabled={testing === s.name}>
                {testing === s.name && <Loader2 className="mr-2 h-3 w-3 animate-spin" />}{t("mcp.test")}
              </Button>
              <Button size="sm" variant="destructive" onClick={() => remove(s.name)}><Trash2 className="mr-1 h-3 w-3" />{t("mcp.delete")}</Button>
              {testRes[s.name] && (
                <div className="w-full text-xs">
                  {testRes[s.name].ok === null && <span className="text-muted-foreground">{testRes[s.name].note}</span>}
                  {testRes[s.name].ok === true && <span className="text-green-700">OK · {testRes[s.name].tool_count} {t("mcp.tools")} · {testRes[s.name].latency_ms} ms{(testRes[s.name].tools ?? []).slice(0, 8).join(", ")}</span>}
                  {testRes[s.name].ok === false && <span className="text-red-600">FAIL · {testRes[s.name].error ?? testRes[s.name].status_code}</span>}
                </div>
              )}
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  );
}
