"use client";
import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { getToken, getSecretsStatus, getRotationGuide, type SecretsStatus, type RotationGuide } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { Lock } from "lucide-react";

export default function SecretsPage() {
  const router = useRouter();
  const { t } = useI18n();
  const [status, setStatus] = useState<SecretsStatus | null>(null);
  const [guide, setGuide] = useState<RotationGuide | null>(null);
  const [checked, setChecked] = useState<Record<string, boolean>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchAll = useCallback(async () => {
    setError(null);
    try {
      const [s, g] = await Promise.all([getSecretsStatus(), getRotationGuide()]);
      setStatus(s);
      setGuide(g);
    } catch (e) {
      setError(e instanceof Error ? e.message : t("secrets.loadFailed"));
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    if (!getToken()) { router.replace("/login"); return; }
    fetchAll();
  }, [fetchAll, router]);

  const toggle = (id: string) => setChecked((prev) => ({ ...prev, [id]: !prev[id] }));

  if (loading) return <div className="p-6 text-sm text-muted-foreground">{t("common.loading")}</div>;

  return (
    <div className="space-y-6 p-6">
      <div>
        <h1 className="flex items-center gap-2 text-2xl font-semibold"><Lock className="h-6 w-6" /> {t("secrets.title")}</h1>
        <p className="text-sm text-muted-foreground">{t("secrets.subtitle")}</p>
      </div>
      {error && <Card className="border-red-500"><CardContent className="pt-4 text-sm text-red-600">{error}</CardContent></Card>}

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            {t("secrets.title")}
            {status && <Badge variant="secondary">{status.rotation_needed_count} / {status.count} {t("secrets.rotationNeeded")}</Badge>}
          </CardTitle>
          {status?.note && <CardDescription>{status.note}</CardDescription>}
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Key</TableHead>
                <TableHead>{t("common.status")}</TableHead>
                <TableHead>{t("secrets.length")}</TableHead>
                <TableHead>source_env</TableHead>
                <TableHead>reason</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {(status?.items ?? []).map((item) => (
                <TableRow key={item.name}>
                  <TableCell className="font-mono text-xs">{item.name}</TableCell>
                  <TableCell>
                    {item.rotation_needed
                      ? <Badge className="bg-amber-500 text-white">{t("secrets.rotationNeeded")}</Badge>
                      : <Badge className="bg-green-600 text-white">{t("secrets.healthy")}</Badge>}
                    {!item.configured && <Badge variant="secondary" className="ml-1">{t("secrets.notConfigured")}</Badge>}
                  </TableCell>
                  <TableCell>{item.length}</TableCell>
                  <TableCell className="font-mono text-xs">{item.source_env ?? "—"}</TableCell>
                  <TableCell className="text-xs text-muted-foreground">{item.reason}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          <p className="mt-3 text-xs text-muted-foreground">{t("secrets.noExecution")}</p>
        </CardContent>
      </Card>

      {guide && (
        <Card>
          <CardHeader>
            <CardTitle>{t("secrets.guide")}</CardTitle>
            <CardDescription>{guide.overview}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <ol className="list-decimal space-y-2 pl-5 text-sm">
              {guide.steps.map((s) => (
                <li key={s.order}><span className="font-medium">{s.title}</span><p className="text-muted-foreground">{s.detail}</p></li>
              ))}
            </ol>
            <div>
              <p className="mb-2 text-sm font-medium">{t("secrets.checklist")} ({Object.values(checked).filter(Boolean).length}/{guide.checklist.length})</p>
              <div className="space-y-1">
                {guide.checklist.map((c) => (
                  <label key={c.id} className="flex cursor-pointer items-center gap-2 rounded-sm px-2 py-1.5 text-sm hover:bg-accent">
                    <input type="checkbox" checked={!!checked[c.id]} onChange={() => toggle(c.id)} className="h-4 w-4 accent-primary" />
                    <span>{c.label}</span>
                  </label>
                ))}
              </div>
            </div>
            <Button variant="outline" onClick={fetchAll}>{t("common.refresh")}</Button>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
