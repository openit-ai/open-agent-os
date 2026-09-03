"use client";
import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { getToken, getFeatureFlags, toggleFeatureFlag, type FeatureFlagsResponse } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { Flag, Loader2 } from "lucide-react";

export default function FeatureFlagsPage() {
  const router = useRouter();
  const { t } = useI18n();
  const [data, setData] = useState<FeatureFlagsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [toggling, setToggling] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);

  const fetchFlags = useCallback(async () => {
    setError(null);
    try {
      setData(await getFeatureFlags());
    } catch (e) {
      setError(e instanceof Error ? e.message : t("featureFlags.loadFailed"));
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    if (!getToken()) { router.replace("/login"); return; }
    fetchFlags();
  }, [fetchFlags, router]);

  const toggle = async (name: string, enabled: boolean) => {
    setToggling(name);
    setError(null);
    setMsg(null);
    try {
      await toggleFeatureFlag(name, !enabled);
      setData(await getFeatureFlags());
      setMsg(t("featureFlags.saved"));
    } catch (e) {
      setError(e instanceof Error ? e.message : t("featureFlags.saveFailed"));
    } finally {
      setToggling(null);
    }
  };

  if (loading) return <div className="p-6 text-sm text-muted-foreground">{t("common.loading")}</div>;

  return (
    <div className="space-y-6 p-6">
      <div>
        <h1 className="flex items-center gap-2 text-2xl font-semibold"><Flag className="h-6 w-6" /> {t("featureFlags.title")}</h1>
        <p className="text-sm text-muted-foreground">{t("featureFlags.subtitle")}</p>
      </div>
      {error && <Card className="border-red-500"><CardContent className="pt-4 text-sm text-red-600">{error}</CardContent></Card>}
      {msg && <Card className="border-green-500"><CardContent className="pt-4 text-sm text-green-700">{msg}</CardContent></Card>}

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            {t("featureFlags.title")}
            {data && <Badge variant="secondary">source: {data.source}</Badge>}
            {data && !data.runtime_wired && <Badge variant="secondary">runtime_wired: false</Badge>}
          </CardTitle>
          {data?.note && <CardDescription>{data.note}</CardDescription>}
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Flag</TableHead>
                <TableHead>{t("common.status")}</TableHead>
                <TableHead>{t("featureFlags.default")}</TableHead>
                <TableHead>{t("common.actions")}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {(data?.flags ?? []).map((f) => (
                <TableRow key={f.name}>
                  <TableCell>
                    <span className="font-mono text-xs">{f.name}</span>
                    {f.overridden && <Badge variant="secondary" className="ml-1">{t("featureFlags.overridden")}</Badge>}
                    {f.custom && <Badge variant="secondary" className="ml-1">{t("featureFlags.custom")}</Badge>}
                    {f.description && <p className="text-xs text-muted-foreground">{f.description}</p>}
                  </TableCell>
                  <TableCell>
                    {f.enabled
                      ? <Badge className="bg-green-600 text-white">{t("featureFlags.enabled")}</Badge>
                      : <Badge variant="secondary">{t("featureFlags.disabled")}</Badge>}
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">{String(f.default)}</TableCell>
                  <TableCell>
                    <Button variant="outline" size="sm" disabled={toggling === f.name} onClick={() => toggle(f.name, f.enabled)}>
                      {toggling === f.name && <Loader2 className="mr-1 h-3 w-3 animate-spin" />}
                      {f.enabled ? t("featureFlags.disabled") : t("featureFlags.enabled")}
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          <div className="mt-3">
            <Button variant="outline" onClick={fetchFlags}>{t("common.refresh")}</Button>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
