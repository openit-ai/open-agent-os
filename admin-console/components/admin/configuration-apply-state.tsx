"use client";

import { ApplyStateBanner } from "@/components/admin/apply-state-banner";
import { useI18n } from "@/lib/i18n";

export interface ConfigurationApplyStateValue {
  persisted?: boolean;
  applied?: boolean;
  config_revision?: string;
  effective_revision?: string;
  requires_restart?: boolean;
  apply_strategy?: "immediate" | "reload" | "restart_required" | "external_action";
}

export function ConfigurationApplyState({ value, service }: { value: ConfigurationApplyStateValue; service: string }) {
  const { t } = useI18n();
  if (value.applied == null) return null;

  return (
    <div className="space-y-3">
      <ApplyStateBanner applied={value.applied} affectedServices={[service]} operationHref="/operations/services" />
      <dl className="grid gap-2 rounded-md border bg-muted/20 p-3 text-xs sm:grid-cols-2">
        <div><dt className="text-muted-foreground">{t("admin.connections.apply.persisted")}</dt><dd className="font-medium">{String(Boolean(value.persisted))}</dd></div>
        <div><dt className="text-muted-foreground">{t("admin.connections.apply.applied")}</dt><dd className="font-medium">{String(value.applied)}</dd></div>
        <div><dt className="text-muted-foreground">{t("admin.connections.apply.configRevision")}</dt><dd className="break-all font-mono">{value.config_revision ?? "-"}</dd></div>
        <div><dt className="text-muted-foreground">{t("admin.connections.apply.effectiveRevision")}</dt><dd className="break-all font-mono">{value.effective_revision ?? "-"}</dd></div>
        <div><dt className="text-muted-foreground">{t("admin.connections.apply.strategy")}</dt><dd className="font-medium">{value.apply_strategy ?? "-"}</dd></div>
        <div><dt className="text-muted-foreground">{t("admin.connections.apply.restartRequired")}</dt><dd className="font-medium">{String(Boolean(value.requires_restart))}</dd></div>
      </dl>
    </div>
  );
}
