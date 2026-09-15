"use client";

import { ConnectionFlow } from "@/components/admin/connections/connection-flow";
import { OlPanel } from "../../infra/ol-panel";
import { useI18n } from "@/lib/i18n";

export default function OutlineConnectionPage() {
  const { t } = useI18n();
  return <ConnectionFlow kind="outline" title={t("admin.connections.kind.outline.title")} description={t("admin.connections.kind.outline.description")} serviceLabel="Outline"><OlPanel /></ConnectionFlow>;
}
