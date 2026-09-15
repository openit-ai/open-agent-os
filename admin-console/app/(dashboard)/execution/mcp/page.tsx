"use client";

import { ConnectionFlow } from "@/components/admin/connections/connection-flow";
import { McpPanel } from "@/components/admin/connections/mcp-feature";
import { useI18n } from "@/lib/i18n";

export default function ExecutionMcpPage() {
  const { t } = useI18n();
  return <ConnectionFlow kind="mcp" title={t("admin.connections.kind.mcp.title")} description={t("admin.connections.kind.mcp.description")} serviceLabel="MCP"><McpPanel /></ConnectionFlow>;
}
