"use client";
import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { getToken, getLicenseStatus, verifyLicense, type LicenseStatusResponse } from "@/lib/api";
import { BadgeCheck, RefreshCw } from "lucide-react";

export default function LicensePage() {
  const router = useRouter();
  const [status, setStatus] = useState<LicenseStatusResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [verifyKey, setVerifyKey] = useState("");
  const [verifying, setVerifying] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  const fetchStatus = useCallback(async () => {
    setError(null);
    try {
      const res = await getLicenseStatus();
      setStatus(res);
    } catch (e) {
      setError(e instanceof Error ? e.message : "조회 실패");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!getToken()) { router.replace("/login"); return; }
    fetchStatus();
  }, [fetchStatus, router]);

  async function handleVerify(e: React.FormEvent) {
    e.preventDefault();
    setMsg(null);
    if (!verifyKey.trim()) { setMsg("라이선스 키를 입력하세요."); return; }
    setVerifying(true);
    try {
      const res = await verifyLicense(verifyKey.trim());
      setStatus(res);
      setMsg(res.message);
    } catch (err) {
      const m = err instanceof Error ? err.message : "검증 실패";
      setMsg(m);
      await fetchStatus();
    } finally {
      setVerifying(false);
    }
  }

  const variant = status?.status === "valid" ? "success" as const : status?.status === "invalid" ? "danger" as const : "secondary" as const;

  return (
    <div className="space-y-4 max-w-[900px]">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-semibold"><BadgeCheck className="h-6 w-6" /> License</h1>
          <p className="text-sm text-muted-foreground">Business Edition — BSL 1.1 Production License (§41)</p>
        </div>
        <Button variant="outline" size="sm" onClick={() => { setLoading(true); fetchStatus(); }} disabled={loading}><RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} /> 새로고침</Button>
      </div>

      {error && <div className="rounded-md border border-destructive/50 bg-destructive/10 px-3 py-2 text-sm text-destructive" role="alert">{error}</div>}
      {msg && <div className="rounded-md border bg-card px-3 py-2 text-sm" role="status">{msg}</div>}

      <Card>
        <CardHeader className="pb-3"><CardTitle className="text-base">현재 라이선스 상태</CardTitle><CardDescription>Viewer도 조회 가능 · 검증은 L5 Admin만 가능 (§22 RBAC)</CardDescription></CardHeader>
        <CardContent>
          {loading ? <p className="text-sm text-muted-foreground">로딩 중...</p> : status ? (
            <div className="space-y-3 text-sm">
              <div className="flex items-center gap-2"><span className="text-muted-foreground">상태</span><Badge variant={variant}>{status.status}</Badge><span className="text-xs text-muted-foreground">{status.message}</span></div>
              <div className="grid gap-2 sm:grid-cols-2">
                <div><span className="text-muted-foreground">Edition</span><div className="font-medium">{status.edition} (BSL {status.bsl_version})</div></div>
                <div><span className="text-muted-foreground">Holder</span><div className="font-medium">{status.holder ?? "-"}</div></div>
                <div className="sm:col-span-2"><span className="text-muted-foreground">License Key</span><div className="font-mono text-xs break-all">{status.license_key ?? "-"}</div></div>
                <div><span className="text-muted-foreground">Verified At</span><div className="text-xs">{status.verified_at ? new Date(status.verified_at).toLocaleString("ko-KR") : "-"}</div></div>
                <div><span className="text-muted-foreground">Expires At</span><div className="text-xs">{status.expires_at ? new Date(status.expires_at).toLocaleString("ko-KR") : "-"}</div></div>
              </div>
            </div>
          ) : <p className="text-sm text-muted-foreground">데이터 없음</p>}
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-3"><CardTitle className="text-base">라이선스 검증</CardTitle><CardDescription>형식: BSL-1.1-BUSINESS-XXXX 또는 OPENIT-BUSINESS-XXXX · L5 전용</CardDescription></CardHeader>
        <CardContent>
          <form onSubmit={handleVerify} className="flex flex-col gap-3 sm:flex-row sm:items-end">
            <div className="flex-1 space-y-1">
              <Label htmlFor="license_key">License Key</Label>
              <Input id="license_key" placeholder="OPENIT-BUSINESS-XXXX-XXXX" value={verifyKey} onChange={(e) => setVerifyKey(e.target.value)} />
            </div>
            <Button type="submit" disabled={verifying}>{verifying ? "검증 중..." : "Verify"}</Button>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
