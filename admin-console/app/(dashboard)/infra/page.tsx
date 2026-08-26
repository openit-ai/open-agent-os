"use client";
import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { apiFetch, getToken } from "@/lib/api";
import { RefreshCw, Trash2, Pencil, Plus } from "lucide-react";

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
  const router = useRouter();
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
    } catch (e) { setError(e instanceof Error ? e.message : "조회 실패"); }
    finally { setLoading(false); }
  }, []);

  useEffect(() => {
    if (!getToken()) { router.replace("/login"); return; }
    fetchList();
    const id = setInterval(fetchList, 15000);
    return () => clearInterval(id);
  }, [fetchList, router]);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setFormError(null);
    if (!host || !port) { setFormError("host, port는 필수입니다."); return; }
    const portNum = Number(port);
    if (!Number.isInteger(portNum) || portNum < 1 || portNum > 65535) { setFormError("port는 1~65535 정수여야 합니다."); return; }
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
    } catch (e) { setFormError(e instanceof Error ? e.message : "저장 실패"); }
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
    if (!confirm("삭제하시겠습니까?")) return;
    try { await apiFetch(`/v1/infra/${id}`, { method: "DELETE" }); await fetchList(); }
    catch (e) { alert(e instanceof Error ? e.message : "삭제 실패"); }
  }

  async function handleProbe(id: string) {
    setProbing(id);
    try { await apiFetch(`/v1/infra/${id}/probe`, { method: "POST" }); await fetchList(); }
    catch (e) { alert(e instanceof Error ? e.message : "probe 실패"); }
    finally { setProbing(null); }
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Infra 관리</h1>
        <Button variant="outline" size="sm" onClick={fetchList}><RefreshCw className="mr-1 h-4 w-4" />새로고침</Button>
      </div>

      {error && <p className="text-sm text-[#DC2626]" role="alert">{error}</p>}

      {/* 등록/수정 폼 */}
      <Card>
        <CardHeader><CardTitle className="text-base">{editingId ? "인프라 수정" : "인프라 등록"}</CardTitle></CardHeader>
        <CardContent>
          <form onSubmit={handleSubmit} className="grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
            <div className="space-y-1">
              <Label htmlFor="service">서비스</Label>
              <select id="service" value={service} onChange={(e) => setService(e.target.value)} className="flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm">
                {SERVICE_OPTIONS.map((o) => <option key={o} value={o}>{o}</option>)}
              </select>
            </div>
            <div className="space-y-1">
              <Label htmlFor="host">host</Label>
              <Input id="host" placeholder="127.0.0.1" value={host} onChange={(e) => setHost(e.target.value)} required />
            </div>
            <div className="space-y-1">
              <Label htmlFor="port">port</Label>
              <Input id="port" type="number" placeholder="8000" value={port} onChange={(e) => setPort(e.target.value)} required />
            </div>
            <div className="space-y-1">
              <Label htmlFor="health_path">health_path</Label>
              <Input id="health_path" placeholder="/health" value={healthPath} onChange={(e) => setHealthPath(e.target.value)} />
            </div>
            <div className="flex items-end gap-2">
              <Button type="submit" disabled={formLoading} className="flex-1"><Plus className="mr-1 h-4 w-4" />{editingId ? "수정" : "등록"}</Button>
              {editingId && <Button type="button" variant="outline" onClick={() => { setEditingId(null); setHost(""); setPort(""); setHealthPath("/health"); }}>취소</Button>}
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
                <TableHead>서비스</TableHead>
                <TableHead>host:port</TableHead>
                <TableHead>health_path</TableHead>
                <TableHead>상태</TableHead>
                <TableHead>latency</TableHead>
                <TableHead>last_check</TableHead>
                <TableHead className="text-right">작업</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {loading ? (
                <TableRow><TableCell colSpan={7} className="text-center text-muted-foreground">로딩 중...</TableCell></TableRow>
              ) : items.length === 0 ? (
                <TableRow><TableCell colSpan={7} className="text-center text-muted-foreground">등록된 인프라가 없습니다.</TableCell></TableRow>
              ) : items.map((it) => (
                <TableRow key={it.id}>
                  <TableCell className="font-medium">{it.service}</TableCell>
                  <TableCell className="font-mono text-xs">{it.host}:{it.port}</TableCell>
                  <TableCell className="font-mono text-xs">{it.health_path}</TableCell>
                  <TableCell><Badge variant={statusVariant(it.status)}>{it.status}</Badge></TableCell>
                  <TableCell>{it.latency_ms != null ? `${it.latency_ms}ms` : "-"}</TableCell>
                  <TableCell className="text-xs">{it.last_check ? new Date(it.last_check).toLocaleString("ko-KR") : "-"}</TableCell>
                  <TableCell>
                    <div className="flex justify-end gap-1">
                      <Button variant="outline" size="sm" disabled={probing === it.id} onClick={() => handleProbe(it.id)}>{probing === it.id ? "..." : "probe"}</Button>
                      <Button variant="ghost" size="icon" onClick={() => startEdit(it)} aria-label="수정"><Pencil className="h-4 w-4" /></Button>
                      <Button variant="ghost" size="icon" onClick={() => handleDelete(it.id)} aria-label="삭제"><Trash2 className="h-4 w-4" /></Button>
                    </div>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
      <p className="text-xs text-muted-foreground">상태는 15초마다 자동 갱신됩니다. probe 버튼으로 수동 점검 가능.</p>
    </div>
  );
}
