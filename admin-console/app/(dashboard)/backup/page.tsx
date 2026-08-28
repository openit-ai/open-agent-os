"use client";
import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { getToken, getBackupStatus, triggerBackup, getUpgradeStatus, type BackupStatusResponse, type UpgradeStatusResponse } from "@/lib/api";
import { DatabaseBackup, RefreshCw, Upload } from "lucide-react";

export default function BackupPage() {
  const router = useRouter();
  const [backup, setBackup] = useState<BackupStatusResponse | null>(null);
  const [upgrade, setUpgrade] = useState<UpgradeStatusResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [triggering, setTriggering] = useState(false);

  const fetchAll = useCallback(async () => {
    setError(null);
    try {
      const [b, u] = await Promise.all([getBackupStatus(), getUpgradeStatus()]);
      setBackup(b);
      setUpgrade(u);
    } catch (e) {
      setError(e instanceof Error ? e.message : "조회 실패");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!getToken()) { router.replace("/login"); return; }
    fetchAll();
  }, [fetchAll, router]);

  async function handleTrigger() {
    setTriggering(true);
    setMsg(null);
    try {
      const res = await triggerBackup();
      setMsg(`Backup triggered: ${res.backup.id} (${res.backup.size_mb} MB)`);
      await fetchAll();
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "백업 실패 — L5 권한 필요");
    } finally {
      setTriggering(false);
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-semibold"><DatabaseBackup className="h-6 w-6" /> Backup / Upgrade</h1>
          <p className="text-sm text-muted-foreground">백업 이력 · 보관 30일 (§16A.3.1) · 업그레이드 상태</p>
        </div>
        <Button variant="outline" size="sm" onClick={() => { setLoading(true); fetchAll(); }} disabled={loading}><RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} /> 새로고침</Button>
      </div>

      {error && <div className="rounded-md border border-destructive/50 bg-destructive/10 px-3 py-2 text-sm text-destructive" role="alert">{error}</div>}
      {msg && <div className="rounded-md border bg-card px-3 py-2 text-sm" role="status">{msg}</div>}

      {/* Upgrade status */}
      <Card>
        <CardHeader className="pb-3"><CardTitle className="text-base">Upgrade Status</CardTitle><CardDescription>현재 vs 가용 버전 · Viewer 조회 가능</CardDescription></CardHeader>
        <CardContent>
          {loading ? <p className="text-sm text-muted-foreground">로딩 중...</p> : upgrade ? (
            <div className="grid gap-3 sm:grid-cols-3 text-sm">
              <div><span className="text-muted-foreground">현재 버전</span><div className="font-mono font-medium">{upgrade.current_version}</div></div>
              <div><span className="text-muted-foreground">가용 버전</span><div className="font-mono font-medium">{upgrade.available_version}</div></div>
              <div><span className="text-muted-foreground">상태</span><div><Badge variant={upgrade.status === "idle" ? "secondary" : upgrade.status === "in_progress" ? "warning" : "success"}>{upgrade.status}</Badge></div></div>
              <div className="sm:col-span-3"><span className="text-muted-foreground">Changelog</span><div className="text-xs">{upgrade.changelog}</div></div>
              <div><span className="text-muted-foreground">Last check</span><div className="text-xs">{upgrade.last_check ? new Date(upgrade.last_check).toLocaleString("ko-KR") : "-"}</div></div>
              <div><span className="text-muted-foreground">Last upgrade</span><div className="text-xs">{upgrade.last_upgrade_at ? new Date(upgrade.last_upgrade_at).toLocaleString("ko-KR") : "-"}</div></div>
            </div>
          ) : <p className="text-sm text-muted-foreground">데이터 없음</p>}
        </CardContent>
      </Card>

      {/* Backup trigger + history */}
      <Card>
        <CardHeader className="pb-3">
          <div className="flex items-center justify-between">
            <div>
              <CardTitle className="text-base">Backup</CardTitle>
              <CardDescription>보관 정책: {backup?.retention_policy ?? "§16A.3.1 — 30일"} · 총 {backup?.total ?? 0}건</CardDescription>
            </div>
            <Button size="sm" onClick={handleTrigger} disabled={triggering}><Upload className="h-4 w-4" /> {triggering ? "트리거 중..." : "Backup Trigger (L5)"}</Button>
          </div>
        </CardHeader>
        <CardContent className="p-0">
          {loading ? <div className="px-6 py-12 text-center text-sm text-muted-foreground">로딩 중...</div> : !backup || backup.backups.length === 0 ? (
            <div className="px-6 py-12 text-center text-sm text-muted-foreground">백업 이력이 없습니다. Trigger로 첫 백업을 생성하세요.</div>
          ) : (
            <div className="relative w-full overflow-auto">
              <Table>
                <TableHeader><TableRow><TableHead>ID</TableHead><TableHead>상태</TableHead><TableHead>생성시각</TableHead><TableHead>만료</TableHead><TableHead>Size</TableHead><TableHead>Retention</TableHead><TableHead>Triggered By</TableHead></TableRow></TableHeader>
                <TableBody>
                  {backup.backups.map((b) => (
                    <TableRow key={b.id}>
                      <TableCell className="font-mono text-xs">{b.id}</TableCell>
                      <TableCell><Badge variant={b.expired ? "danger" : b.status === "completed" ? "success" : "warning"}>{b.expired ? "expired" : b.status}</Badge></TableCell>
                      <TableCell className="text-xs">{new Date(b.created_at).toLocaleString("ko-KR")}</TableCell>
                      <TableCell className="text-xs">{new Date(b.expires_at).toLocaleString("ko-KR")}</TableCell>
                      <TableCell className="text-xs">{b.size_mb} MB</TableCell>
                      <TableCell className="text-xs">{b.retention_days}일</TableCell>
                      <TableCell className="text-xs">{b.triggered_by ?? "-"}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
        </CardContent>
      </Card>
      <p className="text-xs text-muted-foreground">§16A.3.1 Workspace 격리 백업은 30일 retention 후 자동 만료됩니다. Trigger는 L5 Admin 전용.</p>
    </div>
  );
}
