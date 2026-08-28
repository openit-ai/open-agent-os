"use client";
import { useCallback, useEffect, useState, useMemo } from "react";
import { useRouter } from "next/navigation";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import {
  getToken,
  getPendingApprovals,
  decideApproval,
  listMappings,
  deriveAgentId,
  deriveEmployeePrincipal,
  type ApprovalRequestItem,
  type ApprovalDecisionType,
  type UserMapping,
} from "@/lib/api";
import { ClipboardCheck, RefreshCw, ShieldAlert, Link2, Users } from "lucide-react";

function riskVariant(risk: string) {
  const r = risk?.toUpperCase();
  if (r === "HIGH") return "danger" as const;
  if (r === "MEDIUM") return "warning" as const;
  return "success" as const;
}

function formatTime(iso?: string | null) {
  if (!iso) return "-";
  try {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    return d.toLocaleString("ko-KR", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
  } catch {
    return iso;
  }
}

function buildMappingIndex(mappings: UserMapping[]) {
  const byPrincipal = new Map<string, UserMapping>();
  const byMmId = new Map<string, UserMapping>();
  const byUsername = new Map<string, UserMapping>();
  for (const m of mappings) {
    if (m.employee_principal) byPrincipal.set(m.employee_principal, m);
    if (m.mm_user_id) byMmId.set(m.mm_user_id, m);
    const uname = m.mm_username ?? m.username ?? "";
    if (uname) byUsername.set(uname, m);
  }
  return { byPrincipal, byMmId, byUsername };
}

function resolveLinkedAgent(userId: string, idx: ReturnType<typeof buildMappingIndex>): { mapping: UserMapping | null; agentId: string | null; hint: string } {
  // direct principal match
  if (idx.byPrincipal.has(userId)) {
    const m = idx.byPrincipal.get(userId)!;
    return { mapping: m, agentId: m.agent_id, hint: "principal 일치" };
  }
  if (idx.byMmId.has(userId)) {
    const m = idx.byMmId.get(userId)!;
    return { mapping: m, agentId: m.agent_id, hint: "MM ID 일치" };
  }
  if (idx.byUsername.has(userId)) {
    const m = idx.byUsername.get(userId)!;
    return { mapping: m, agentId: m.agent_id, hint: "username 일치" };
  }
  // not mapped — derive hint if looks like employee: or username
  if (userId.startsWith("employee:")) {
    try {
      return { mapping: null, agentId: deriveAgentId(userId), hint: "미매핑 · 자동 유도" };
    } catch {
      return { mapping: null, agentId: null, hint: "미매핑" };
    }
  }
  if (userId) {
    try {
      const principal = deriveEmployeePrincipal(userId, userId);
      return { mapping: null, agentId: deriveAgentId(principal), hint: "미매핑 · 자동 유도" };
    } catch {
      return { mapping: null, agentId: null, hint: "미매핑" };
    }
  }
  return { mapping: null, agentId: null, hint: "" };
}

export default function ApprovalsPage() {
  const router = useRouter();
  const [items, setItems] = useState<ApprovalRequestItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [deciding, setDeciding] = useState<string | null>(null);
  const [groupId, setGroupId] = useState("default-group");
  const [actionMsg, setActionMsg] = useState<string | null>(null);
  const [mappings, setMappings] = useState<UserMapping[]>([]);

  const mappingIndex = useMemo(() => buildMappingIndex(mappings), [mappings]);

  const fetchList = useCallback(async () => {
    setError(null);
    try {
      const res = await getPendingApprovals();
      setItems(res.pending ?? []);
    } catch (e) {
      setError(e instanceof Error ? e.message : "조회 실패");
    } finally {
      setLoading(false);
    }
  }, []);

  const fetchMappings = useCallback(async () => {
    try {
      const res = await listMappings();
      const arr: UserMapping[] = Array.isArray(res)
        ? (res as UserMapping[])
        : ((res as { mappings?: UserMapping[] }).mappings ??
          (res as { items?: UserMapping[] }).items ??
          []);
      setMappings(arr);
    } catch {
      // silent — mapping context is optional
    }
  }, []);

  useEffect(() => {
    if (!getToken()) { router.replace("/login"); return; }
    fetchList();
    fetchMappings();
  }, [fetchList, fetchMappings, router]);

  async function handleDecide(id: string, decision: ApprovalDecisionType) {
    setDeciding(id + decision);
    setActionMsg(null);
    try {
      if (decision === "APPROVED_GROUP_ALWAYS" && !groupId.trim()) {
        setActionMsg("그룹 ID를 입력하세요.");
        return;
      }
      await decideApproval({
        approval_id: id,
        decision,
        group_id: decision === "APPROVED_GROUP_ALWAYS" ? groupId.trim() : undefined,
      });
      setActionMsg(`${decision} 처리 완료`);
      await fetchList();
    } catch (e) {
      setActionMsg(e instanceof Error ? e.message : "결정 실패");
    } finally {
      setDeciding(null);
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-semibold">
            <ClipboardCheck className="h-6 w-6" /> Approvals
          </h1>
          <p className="text-sm text-muted-foreground">승인 대기 — 위험도 기반 JIT 승인 (Once / Always 사용자·그룹 / Deny) · 요청자 매핑 컨텍스트 표시</p>
        </div>
        <Button variant="outline" size="sm" onClick={() => { setLoading(true); fetchList(); fetchMappings(); }} disabled={loading}>
          <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} /> 새로고침
        </Button>
      </div>

      {actionMsg && (
        <div className="rounded-md border bg-card px-3 py-2 text-sm" role="status">
          {actionMsg}
        </div>
      )}
      {error && (
        <div className="rounded-md border border-destructive/50 bg-destructive/10 px-3 py-2 text-sm text-destructive" role="alert">
          {error}
        </div>
      )}

      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="flex items-center gap-2 text-base">
            <Users className="h-4 w-4" />
            승인 대기 목록
          </CardTitle>
          <CardDescription>
            {loading ? "로딩 중..." : `${items.length}건 대기 중`}
            <span className="ml-3 inline-flex items-center gap-1">
              <span className="text-xs">그룹 ID:</span>
              <Input value={groupId} onChange={(e) => setGroupId(e.target.value)} placeholder="group id" className="h-7 w-36 text-xs" />
            </span>
            {mappings.length > 0 && (
              <span className="ml-3 inline-flex items-center gap-1 text-xs text-muted-foreground">
                <Link2 className="h-3 w-3" />
                매핑 {mappings.length}건 로드됨
              </span>
            )}
          </CardDescription>
        </CardHeader>
        <CardContent className="p-0">
          {loading ? (
            <div className="px-6 py-12 text-center text-sm text-muted-foreground">로딩 중...</div>
          ) : items.length === 0 ? (
            <div className="flex flex-col items-center justify-center px-6 py-16 text-center">
              <div className="mb-3 flex h-12 w-12 items-center justify-center rounded-full bg-muted">
                <ShieldAlert className="h-6 w-6 text-muted-foreground" />
              </div>
              <p className="text-sm font-medium">대기 중인 승인이 없습니다</p>
              <p className="mt-1 max-w-sm text-xs text-muted-foreground">
                새로운 고위험 작업이 요청되면 이 목록에 표시됩니다. 정책에 따라 APPROVAL_REQUIRED가 발생한 요청이 여기에 쌓입니다.
              </p>
              <Button variant="outline" size="sm" className="mt-4" onClick={fetchList}>새로고침</Button>
            </div>
          ) : (
            <div className="relative w-full overflow-auto">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="min-w-[130px]">Approval ID</TableHead>
                    <TableHead className="min-w-[140px]">Requester</TableHead>
                    <TableHead className="min-w-[160px]">
                      <span className="inline-flex items-center gap-1">
                        <Link2 className="h-3.5 w-3.5" />
                        Linked Agent
                      </span>
                    </TableHead>
                    <TableHead>Agent</TableHead>
                    <TableHead>Action</TableHead>
                    <TableHead className="min-w-[160px]">Resource</TableHead>
                    <TableHead>Risk</TableHead>
                    <TableHead className="min-w-[130px]">요청시각</TableHead>
                    <TableHead className="min-w-[130px]">만료</TableHead>
                    <TableHead className="min-w-[360px] text-right">결정</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {items.map((it) => {
                    const aid = (it.approval_id as string) || (it as unknown as { id: string }).id || "-";
                    const reqTime = (it as unknown as { created_at?: string }).created_at || undefined;
                    const { mapping, agentId, hint } = resolveLinkedAgent(it.user_id, mappingIndex);
                    const isMapped = !!mapping;
                    return (
                      <TableRow key={aid}>
                        <TableCell className="font-mono text-xs">
                          <span className="truncate" title={aid}>{aid}</span>
                        </TableCell>
                        <TableCell>
                          <div className="flex flex-col gap-0.5">
                            <span className="font-mono text-xs" title={it.user_id}>{it.user_id}</span>
                            {mapping && (
                              <span className="inline-flex items-center gap-1 text-[11px] text-muted-foreground" title={mapping.employee_principal}>
                                <Link2 className="h-3 w-3" />
                                {mapping.mm_username ?? mapping.mm_user_id}
                              </span>
                            )}
                          </div>
                        </TableCell>
                        <TableCell>
                          {agentId ? (
                            <span
                              className={`inline-flex max-w-[170px] items-center gap-1 truncate font-mono text-xs ${isMapped ? "text-foreground" : "text-muted-foreground"}`}
                              title={`${hint}: ${agentId}${mapping ? ` (principal: ${mapping.employee_principal})` : ""}`}
                            >
                              <Badge variant={isMapped ? "success" : "secondary"} className="shrink-0 gap-1">
                                <Link2 className="h-3 w-3" />
                                {isMapped ? "매핑" : "유도"}
                              </Badge>
                              <span className="truncate">{agentId}</span>
                            </span>
                          ) : (
                            <span className="text-xs text-muted-foreground" title={hint}>-</span>
                          )}
                        </TableCell>
                        <TableCell className="font-mono text-xs text-muted-foreground" title={it.agent_id}>{it.agent_id}</TableCell>
                        <TableCell className="text-xs font-medium">{it.action}</TableCell>
                        <TableCell className="max-w-[200px] truncate text-xs" title={it.resource}>{it.resource}</TableCell>
                        <TableCell>
                          <Badge variant={riskVariant(it.risk)}>{it.risk}</Badge>
                        </TableCell>
                        <TableCell className="text-xs text-muted-foreground">{formatTime(reqTime)}</TableCell>
                        <TableCell className="text-xs text-muted-foreground">{formatTime(it.expires_at)}</TableCell>
                        <TableCell>
                          <div className="flex flex-wrap justify-end gap-1">
                            <Button size="sm" variant="default" disabled={!!deciding} onClick={() => handleDecide(aid, "APPROVED_ONCE")} className="h-7 px-2 text-xs">
                              Approve Once
                            </Button>
                            <Button size="sm" variant="secondary" disabled={!!deciding} onClick={() => handleDecide(aid, "APPROVED_USER_ALWAYS")} className="h-7 px-2 text-xs">
                              Always(사용자)
                            </Button>
                            <Button size="sm" variant="outline" disabled={!!deciding} onClick={() => handleDecide(aid, "APPROVED_GROUP_ALWAYS")} className="h-7 px-2 text-xs">
                              Always(그룹)
                            </Button>
                            <Button size="sm" variant="destructive" disabled={!!deciding} onClick={() => handleDecide(aid, "DENIED")} className="h-7 px-2 text-xs">
                              Deny
                            </Button>
                          </div>
                        </TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            </div>
          )}
        </CardContent>
      </Card>

      <p className="text-xs text-muted-foreground">
        * Approve Once: 이번 요청만 승인 · Always(사용자): 동일 사용자·동일 action/resource 영구 승인 · Always(그룹): 그룹 전체 영구 승인 · Deny: 거부. Requester의 Mattermost 매핑이 있으면 Linked Agent에 <span className="inline-flex items-center gap-1"><Link2 className="h-3 w-3" />매핑</span> 배지, 없으면 자동 유도값(<span className="font-mono">agent:assistant:&lt;suffix&gt;</span>)을 표시합니다.
      </p>
    </div>
  );
}
