"use client";

import { ConnectionFlow } from "@/components/admin/connections/connection-flow";
import { SmtpPanel } from "../../infra/smtp-panel";
import { useI18n } from "@/lib/i18n";

export default function SmtpConnectionPage() {
  const { t } = useI18n();
  return <ConnectionFlow kind="smtp" title={t("admin.connections.kind.smtp.title")} description={t("admin.connections.kind.smtp.description")} serviceLabel="SMTP"><SmtpPanel /></ConnectionFlow>;
}
