"use client";

import { ConnectionFlow } from "@/components/admin/connections/connection-flow";
import { MmPanel } from "../../infra/mm-panel";
import { useI18n } from "@/lib/i18n";

export default function MattermostConnectionPage() {
  const { t } = useI18n();
  return <ConnectionFlow kind="mattermost" title={t("admin.connections.kind.mattermost.title")} description={t("admin.connections.kind.mattermost.description")} serviceLabel="Mattermost"><MmPanel /></ConnectionFlow>;
}
