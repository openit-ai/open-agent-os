"use client";

import { ConnectionFlow } from "@/components/admin/connections/connection-flow";
import { SlackPanel } from "../../infra/slack-panel";
import { useI18n } from "@/lib/i18n";

export default function SlackConnectionPage() {
  const { t } = useI18n();
  return <ConnectionFlow kind="slack" title={t("admin.connections.kind.slack.title")} description={t("admin.connections.kind.slack.description")} serviceLabel="Slack"><SlackPanel /></ConnectionFlow>;
}
