"use client";

import { ConnectionFlow } from "@/components/admin/connections/connection-flow";
import { OAuthPanel } from "../../infra/oauth-panel";
import { useI18n } from "@/lib/i18n";

export default function OAuthConnectionPage() {
  const { t } = useI18n();
  return <ConnectionFlow kind="oauth" title={t("admin.connections.kind.oauth.title")} description={t("admin.connections.kind.oauth.description")} serviceLabel="OAuth"><OAuthPanel /></ConnectionFlow>;
}
