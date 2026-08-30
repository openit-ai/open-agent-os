"use client";
import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/lib/i18n";
import { getToken, getPolicyBundles, type PolicyBundle, type PolicyRule } from "@/lib/api";
import { RefreshCw, Shield, AlertTriangle } from "lucide-react";

const EVALUATION_ORDER_FALLBACK = [
  "explicit_deny",
  "security_boundary_deny",
  "personal_delegation",
  "persistent_user_grant",
  "group_grant",
  "default_bundle",
  "jit_approval",
  "default_deny",
];

const SOURCE_LABEL: Record<string, string> = {
  explicit_deny: "Explicit Deny",
  security_boundary_deny: "Security Boundary",
  personal_delegation: "Personal Delegation",
  persistent_user_grant: "Persistent Grant",
  group_grant: "Group Grant",
  default_bundle: "Default Bundle",
  jit_approval: "JIT Approval",
  default_deny: "Default Deny",
};

function decisionVariant(d: string) {
  if (d === "DENY") return "danger" as const;
  if (d === "ALLOW") return "success" as const;
  if (d === "APPROVAL_REQUIRED") return "warning" as const;
  return "secondary" as const;
}

function orderIndex(source: string, order: string[]) {
  const idx = order.indexOf(source);
  return idx >= 0 ? idx + 1 : 99;
}

export default function PolicyPage() {
  const router = useRouter();
  const { t } = useI18n();
  const [bundles, setBundles] = useState<PolicyBundle[]>([]);
  const [evalOrder, setEvalOrder] = useState<string[]>(EVALUATION_ORDER_FALLBACK);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchBundles = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await getPolicyBundles();
      setBundles(res.bundles ?? []);
      if (res.evaluation_order?.length) setEvalOrder(res.evaluation_order);
    } catch (e) {
      setError(e instanceof Error ? e.message : t("common.fetchFailed"));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!getToken()) {
      router.replace("/login");
      return;
    }
    fetchBundles();
  }, [fetchBundles, router]);

  return (
    <div className="mx-auto w-full max-w-[1200px] space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="flex items-center gap-2 text-2xl font-semibold">
          <Shield className="h-6 w-6" />
          Policy Bundles
        </h1>
        <Button variant="outline" size="sm" onClick={fetchBundles} disabled={loading}>
          <RefreshCw className="mr-1 h-4 w-4" />
          {t("common.refresh")}
        </Button>
      </div>

      {error && (
        <p className="rounded-md bg-[#DC2626]/10 p-3 text-sm text-[#DC2626]" role="alert">
          {error}
        </p>
      )}

      {/* Section 25 fixed order */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">{t("policy.section25Title")}</CardTitle>
          <CardDescription>{t("policy.section25Desc")}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="flex flex-wrap gap-2">
            {evalOrder.map((src, idx) => {
              const isExplicitDeny = src === "explicit_deny";
              const isPersonal = src === "personal_delegation";
              return (
                <div
                  key={src}
                  className={`flex items-center gap-1.5 rounded-md border px-2.5 py-1 text-xs font-medium ${isExplicitDeny ? "border-[#DC2626] bg-[#DC2626] text-white" : isPersonal ? "border-[#22C55E] bg-[#22C55E]/10 text-[#16A34A]" : "bg-muted"}`}
                >
                  <span className="flex h-5 w-5 items-center justify-center rounded-full bg-background text-[11px] font-bold text-foreground">{idx + 1}</span>
                  {SOURCE_LABEL[src] ?? src}
                </div>
              );
            })}
          </div>
          <div className="flex flex-wrap gap-2 pt-1">
            <Badge variant="danger" className="gap-1">
              <AlertTriangle className="h-3 w-3" />
              {t("policy.explicitDenyOverride")}
            </Badge>
            <Badge variant="success" className="gap-1">
              {t("policy.personalDelegationNote")}
            </Badge>
          </div>
          <p className="text-xs text-muted-foreground">
            {t("policy.sortingNote")}
          </p>
        </CardContent>
      </Card>

      {/* Bundles */}
      {loading ? (
        <Card>
          <CardContent className="pt-6 text-center text-sm text-muted-foreground">{t("policy.loading")}</CardContent>
        </Card>
      ) : bundles.length === 0 ? (
        <Card>
          <CardContent className="pt-6 text-center text-sm text-muted-foreground">{t("policy.noBundles")}</CardContent>
        </Card>
      ) : (
        bundles.map((bundle) => (
          <Card key={bundle.id} className="overflow-hidden">
            <CardHeader className="pb-3">
              <CardTitle className="flex flex-wrap items-center gap-2 text-base">
                {bundle.name}
                <Badge variant="outline">{bundle.id}</Badge>
                <Badge variant="secondary">v{bundle.version}</Badge>
              </CardTitle>
              <CardDescription className="flex flex-wrap gap-2">
                <span>
                  {t("policy.tenant")} <span className="font-mono font-medium text-foreground">{bundle.tenant_id}</span>
                </span>
                <span>·</span>
                <span>{t("policy.bundleRules", { count: String(bundle.rules?.length ?? 0) })}</span>
              </CardDescription>
            </CardHeader>
            <CardContent className="p-0">
              <div className="w-full overflow-auto">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead className="whitespace-nowrap"># (Section 25)</TableHead>
                      <TableHead>source</TableHead>
                      <TableHead>action (glob)</TableHead>
                      <TableHead>resource (glob)</TableHead>
                      <TableHead>decision</TableHead>
                      <TableHead>priority</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {[...(bundle.rules ?? [])]
                      .sort((a, b) => {
                        const ao = orderIndex(a.source, evalOrder);
                        const bo = orderIndex(b.source, evalOrder);
                        if (ao !== bo) return ao - bo;
                        if (a.priority !== b.priority) return a.priority - b.priority;
                        return a.id.localeCompare(b.id);
                      })
                      .map((rule: PolicyRule) => {
                        const isExplicitDeny = rule.source === "explicit_deny";
                        const isPersonal = rule.source === "personal_delegation";
                        return (
                          <TableRow
                            key={rule.id}
                            className={isExplicitDeny ? "bg-[#DC2626]/10 hover:bg-[#DC2626]/15" : isPersonal ? "bg-[#22C55E]/5" : ""}
                          >
                            <TableCell className="whitespace-nowrap text-xs font-medium">
                              {orderIndex(rule.source, evalOrder)}
                              <span className="ml-1 text-muted-foreground">· {rule.id}</span>
                            </TableCell>
                            <TableCell>
                              <Badge
                                variant={isExplicitDeny ? "danger" : isPersonal ? "success" : "secondary"}
                                className="whitespace-nowrap"
                              >
                                {SOURCE_LABEL[rule.source] ?? rule.source}
                              </Badge>
                              {isPersonal && (
                                <div className="mt-1">
                                  <Badge variant="outline" className="text-[10px] leading-none">
                                    {t("policy.explicitDenyBadge")}
                                  </Badge>
                                </div>
                              )}
                            </TableCell>
                            <TableCell className="font-mono text-xs">{rule.action}</TableCell>
                            <TableCell className="font-mono text-xs">{rule.resource_pattern}</TableCell>
                            <TableCell>
                              <Badge variant={decisionVariant(rule.effect)}>{rule.effect}</Badge>
                            </TableCell>
                            <TableCell className="text-xs">{rule.priority ?? 0}</TableCell>
                          </TableRow>
                        );
                      })}
                  </TableBody>
                </Table>
              </div>
              {/* Rule description footnote */}
              <div className="border-t bg-muted/20 p-3">
                <p className="text-xs text-muted-foreground">
                  {t("policy.explicitDenyHint")}
                </p>
              </div>
            </CardContent>
          </Card>
        ))
      )}

      <p className="text-xs text-muted-foreground">
        {t("policy.dataNote")}
      </p>
    </div>
  );
}
