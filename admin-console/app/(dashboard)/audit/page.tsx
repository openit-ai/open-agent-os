"use client";
import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { getToken, getAuditEvents, verifyAuditChain, getAuditCheckpoint, type AuditEventItem, type AuditVerifyResponse, type AuditCheckpoint } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { ScrollText, ShieldCheck, ShieldAlert, RefreshCw, CheckCircle2, XCircle } from "lucide-react";

function eventBadgeVariant(t: string) {
  const u = t.toUpperCase();
  if (u.includes("APPROVAL")) return "warning" as const;
  if (u.includes("POLICY")) return "secondary" as const;
  if (u.includes("DELEGATION")) return "default" as const;
  if (u.includes("CAPABILITY")) return "success" as const;
  if (u.includes("DATA") || u.includes("TOOL")) return "outline" as const;
  return "secondary" as const;
}

function formatTime(iso: string) {
  try {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    const locale = typeof window !== "undefined" && localStorage.getItem("oaos_lang") === "ko" ? "ko-KR" : "en-US";
    return d.toLocaleString(locale, { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" });
  } catch { return iso; }
}

export default function AuditPage() {
  const router = useRouter();
  const { t } = useI18n();
  const [events, setEvents] = useState<AuditEventItem[]>([]);
  const [head, setHead] = useState<string | null>(null);
  const [count, setCount] = useState(0);
  const [verify, setVerify] = useState<AuditVerifyResponse | null>(null);
  const [checkpoint, setCheckpoint] = useState<AuditCheckpoint | null>(null);
  const [loading, setLoading] = useState(true);
  const [verifying, setVerifying] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);

  const fetchAll = useCallback(async () => {
    setError(null);
    try {
      const [evRes, cpRes] = await Promise.allSettled([getAuditEvents(), getAuditCheckpoint()]);
      if (evRes.status === "fulfilled") {
        setEvents(evRes.value.events ?? []);
        setHead(evRes.value.head ?? null);
        setCount(evRes.value.count ?? evRes.value.events?.length ?? 0);
      }
      if (cpRes.status === "fulfilled") {
        setCheckpoint(cpRes.value);
      }
      // also try verify silently
      try {
        const v = await verifyAuditChain();
        setVerify(v);
        if (v.checkpoint) setCheckpoint(v.checkpoint);
        if (v.head) setHead(v.head);
        if (typeof v.event_count === "number") setCount(v.event_count);
      } catch { /* ignore */ }
    } catch (e) {
      setError(e instanceof Error ? e.message : t("common.fetchFailed"));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!getToken()) { router.replace("/login"); return; }
    fetchAll();
  }, [fetchAll, router]);

  async function handleVerify() {
    setVerifying(true);
    setMsg(null);
    try {
      const v = await verifyAuditChain();
      setVerify(v);
      setMsg(`chain_valid=${String(v.chain_valid)} · checkpoint_valid=${String(v.checkpoint_valid ?? "n/a")}`);
      if (v.checkpoint) setCheckpoint(v.checkpoint);
      if (v.head) setHead(v.head);
    } catch (e) {
      setMsg(e instanceof Error ? e.message : t("common.verifyFailed"));
    } finally {
      setVerifying(false);
    }
  }

  const sorted = [...events].sort((a, b) => new Date(b.timestamp).getTime() - new Date(a.timestamp).getTime());

  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-semibold"><ScrollText className="h-6 w-6" /> Audit</h1>
          <p className="text-sm text-muted-foreground">{t("audit.subtitle")}</p>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" size="sm" onClick={() => { setLoading(true); fetchAll(); }} disabled={loading}>
            <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} /> {t("common.refresh")}
          </Button>
          <Button size="sm" onClick={handleVerify} disabled={verifying}>
            <ShieldCheck className="h-4 w-4" /> {verifying ? t("common.verifying") : t("common.verify")}
          </Button>
        </div>
      </div>

      {error && <div className="rounded-md border border-destructive/50 bg-destructive/10 px-3 py-2 text-sm text-destructive" role="alert">{error}</div>}
      {msg && <div className="rounded-md border bg-card px-3 py-2 text-sm" role="status">{msg}</div>}

      {/* 검증 배지 / 카운트 / checkpoint 요약 */}
      <div className="grid gap-4 sm:grid-cols-3">
        <Card>
          <CardHeader className="pb-2"><CardTitle className="text-sm">{t("audit.eventCount")}</CardTitle></CardHeader>
          <CardContent><div className="text-2xl font-bold">{loading ? "-" : count}</div><CardDescription>{t("audit.chainLengthDesc")}</CardDescription></CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2"><CardTitle className="text-sm">{t("audit.chainIntegrity")}</CardTitle></CardHeader>
          <CardContent className="space-y-2">
            {verify ? (
              <div className="flex items-center gap-2">
                {verify.chain_valid ? <CheckCircle2 className="h-5 w-5 text-[#22C55E]" /> : <XCircle className="h-5 w-5 text-[#DC2626]" />}
                <Badge variant={verify.chain_valid ? "success" : "danger"}>{verify.chain_valid ? "chain_valid" : "tampered"}</Badge>
              </div>
            ) : <span className="text-sm text-muted-foreground">{t("audit.verifyToCheck")}</span>}
            {verify?.checkpoint_valid !== undefined && (
              <div className="flex items-center gap-2">
                {verify.checkpoint_valid ? <CheckCircle2 className="h-4 w-4 text-[#22C55E]" /> : <XCircle className="h-4 w-4 text-[#DC2626]" />}
                <Badge variant={verify.checkpoint_valid ? "success" : "danger"}>checkpoint_{verify.checkpoint_valid ? "valid" : "invalid"}</Badge>
              </div>
            )}
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2"><CardTitle className="text-sm">Checkpoint</CardTitle></CardHeader>
          <CardContent className="space-y-1 text-xs">
            {checkpoint ? (
              <>
                <div className="flex justify-between gap-2"><span className="text-muted-foreground">head hash</span><span className="truncate font-mono" title={checkpoint.chain_head_hash}>{checkpoint.chain_head_hash.slice(0, 16)}...{checkpoint.chain_head_hash.slice(-6)}</span></div>
                <div className="flex justify-between gap-2"><span className="text-muted-foreground">event_count</span><span className="font-mono">{checkpoint.event_count}</span></div>
                <div className="flex justify-between gap-2"><span className="text-muted-foreground">created</span><span className="font-mono text-[11px]">{formatTime(checkpoint.created_at)}</span></div>
                <div className="pt-1"><span className="text-muted-foreground">signature</span><div className="mt-1 break-all rounded bg-muted p-2 font-mono text-[10px] leading-relaxed">{checkpoint.signature}</div></div>
              </>
            ) : (
              <span className="text-muted-foreground">{t("audit.noCheckpoint")}</span>
            )}
            {head && (
              <div className="pt-2 border-t mt-2"><span className="text-muted-foreground">current head</span><div className="mt-1 break-all font-mono text-[10px]">{head}</div></div>
            )}
          </CardContent>
        </Card>
      </div>

      {/* 타임라인 */}
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-base">{t("audit.timelineTitle")}</CardTitle>
          <CardDescription>{t("audit.timelineDesc", { count: String(sorted.length) })}</CardDescription>
        </CardHeader>
        <CardContent>
          {loading ? (
            <div className="py-12 text-center text-sm text-muted-foreground">{t("common.loading")}</div>
          ) : sorted.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-16 text-center">
              <div className="mb-3 flex h-12 w-12 items-center justify-center rounded-full bg-muted"><ScrollText className="h-6 w-6 text-muted-foreground" /></div>
              <p className="text-sm font-medium">{t("audit.noEvents")}</p>
              <p className="mt-1 text-xs text-muted-foreground">{t("audit.noEventsDesc")}</p>
            </div>
          ) : (
            <div className="relative">
              {/* vertical line */}
              <div className="absolute left-2 top-2 bottom-2 w-px bg-border hidden sm:block" />
              <ul className="space-y-3">
                {sorted.map((ev) => (
                  <li key={ev.event_id} className="relative flex gap-3 rounded-lg border p-3 sm:ml-4">
                    <span className="absolute -left-[1.35rem] top-4 hidden h-2.5 w-2.5 rounded-full border-2 border-primary bg-background sm:block" />
                    <div className="flex-1 space-y-2">
                      <div className="flex flex-wrap items-center gap-2">
                        <Badge variant={eventBadgeVariant(ev.event_type)}>{ev.event_type}</Badge>
                        <span className="text-xs text-muted-foreground">{formatTime(ev.timestamp)}</span>
                        <span className="font-mono text-[11px] text-muted-foreground">{ev.event_id}</span>
                        {ev.decision && <Badge variant="outline">{ev.decision}</Badge>}
                      </div>
                      <div className="grid grid-cols-2 gap-2 text-xs sm:grid-cols-4">
                        <div><span className="text-muted-foreground">user</span><div className="truncate font-medium">{ev.user_id ?? "-"}</div></div>
                        <div><span className="text-muted-foreground">agent</span><div className="truncate font-medium">{ev.agent_id ?? "-"}</div></div>
                        <div><span className="text-muted-foreground">resource</span><div className="truncate font-mono text-[11px]" title={ev.resource ?? ""}>{ev.resource ?? "-"}</div></div>
                        <div><span className="text-muted-foreground">action</span><div className="truncate">{ev.action ?? "-"}</div></div>
                      </div>
                      {(ev.event_hash || ev.previous_hash) && (
                        <div className="flex flex-col gap-1 rounded bg-muted/50 p-2 font-mono text-[10px] leading-relaxed">
                          {ev.event_hash && <div className="flex gap-1"><span className="shrink-0 text-muted-foreground">hash</span><span className="break-all">{ev.event_hash}</span></div>}
                          {ev.previous_hash && <div className="flex gap-1"><span className="shrink-0 text-muted-foreground">prev </span><span className="break-all">{ev.previous_hash}</span></div>}
                        </div>
                      )}
                    </div>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
