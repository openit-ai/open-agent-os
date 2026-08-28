"use client";
import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { getToken, getSecurityUpdates, type SecurityUpdatesResponse } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { ShieldAlert, RefreshCw } from "lucide-react";

function severityVariant(s: string) {
  const u = s.toUpperCase();
  if (u === "HIGH" || u === "CRITICAL") return "danger" as const;
  if (u === "MEDIUM") return "warning" as const;
  return "secondary" as const;
}

export default function SecurityUpdatesPage() {
  const { t } = useI18n();
  const router = useRouter();
  const [data, setData] = useState<SecurityUpdatesResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);

  const fetchData = useCallback(async () => {
    setError(null);
    try {
      const res = await getSecurityUpdates();
      setData(res);
    } catch (e) {
      setError(e instanceof Error ? e.message : t("common.fetchFailed"));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!getToken()) { router.replace("/login"); return; }
    fetchData();
  }, [fetchData, router]);

  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-semibold"><ShieldAlert className="h-6 w-6" /> Security Updates</h1>
          <p className="text-sm text-muted-foreground">{t("securityUpdates.subtitle")}</p>
        </div>
        <Button variant="outline" size="sm" onClick={() => { setLoading(true); fetchData(); }} disabled={loading}><RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} /> {t("common.refresh")}</Button>
      </div>

      {error && <div className="rounded-md border border-destructive/50 bg-destructive/10 px-3 py-2 text-sm text-destructive" role="alert">{error}</div>}
      {msg && <div className="rounded-md border bg-card px-3 py-2 text-sm" role="status">{msg}</div>}

      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-base">{t("securityUpdates.listTitle")}</CardTitle>
          <CardDescription>{t("securityUpdates.listDesc", { version: data?.current_version ?? (loading ? "..." : "-"), count: String(data?.count ?? 0) })}</CardDescription>
        </CardHeader>
        <CardContent className="p-0">
          {loading ? <div className="px-6 py-12 text-center text-sm text-muted-foreground">{t("common.loading")}</div> : !data || data.updates.length === 0 ? (
            <div className="px-6 py-12 text-center text-sm text-muted-foreground">{t("securityUpdates.noUpdates")}</div>
          ) : (
            <div className="space-y-4 p-4">
              {data.updates.map((u) => (
                <Card key={u.version} className="border-l-4" style={{ borderLeftColor: u.severity === "high" ? "#DC2626" : u.severity === "medium" ? "#F59E0B" : "#6B7280" }}>
                  <CardHeader className="pb-2">
                    <div className="flex items-center gap-2">
                      <CardTitle className="text-base">v{u.version}</CardTitle>
                      <Badge variant={severityVariant(u.severity)}>{u.severity}</Badge>
                      <Badge variant={u.available ? "success" : "secondary"}>{u.available ? "available" : "installed"}</Badge>
                      <span className="text-xs text-muted-foreground">{u.release_date}</span>
                    </div>
                    <CardDescription>{u.changelog}</CardDescription>
                  </CardHeader>
                  <CardContent className="pt-0">
                    <div className="text-xs font-medium mb-1">{t("securityUpdates.cves", { count: String(u.cves.length) })}</div>
                    <Table>
                      <TableHeader><TableRow><TableHead>{t("securityUpdates.cve")}</TableHead><TableHead>{t("securityUpdates.severity")}</TableHead><TableHead>{t("securityUpdates.summary")}</TableHead></TableRow></TableHeader>
                      <TableBody>
                        {u.cves.map((c) => (
                          <TableRow key={c.id}><TableCell className="font-mono text-xs">{c.id}</TableCell><TableCell><Badge variant={severityVariant(c.severity)}>{c.severity}</Badge></TableCell><TableCell className="text-xs">{c.summary}</TableCell></TableRow>
                        ))}
                      </TableBody>
                    </Table>
                    <div className="mt-3 flex gap-2">
                      <Button size="sm" variant="outline" onClick={() => setMsg(t("securityUpdates.applyMsg", { version: u.version }))}>{t("securityUpdates.apply")}</Button>
                    </div>
                  </CardContent>
                </Card>
              ))}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
