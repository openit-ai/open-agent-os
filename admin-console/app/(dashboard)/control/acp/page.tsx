"use client";

import { AcpSection } from "@/components/admin/connections/acp-feature";
import { ConnectionFlow } from "@/components/admin/connections/connection-flow";
import { useI18n } from "@/lib/i18n";

export default function ControlAcpPage() {
  const { t } = useI18n();
  return <ConnectionFlow kind="acp" title={t("admin.connections.kind.acp.title")} description={t("admin.connections.kind.acp.description")} serviceLabel="ACP / Hermes"><AcpSection /></ConnectionFlow>;
}
