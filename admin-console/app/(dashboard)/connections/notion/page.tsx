"use client";

import { ConnectionFlow } from "@/components/admin/connections/connection-flow";
import { NotionPanel } from "../../infra/notion-panel";
import { useI18n } from "@/lib/i18n";

export default function NotionConnectionPage() {
  const { t } = useI18n();
  return <ConnectionFlow kind="notion" title={t("admin.connections.kind.notion.title")} description={t("admin.connections.kind.notion.description")} serviceLabel="Notion"><NotionPanel /></ConnectionFlow>;
}
