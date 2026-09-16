"use client";

import { KeyRound } from "lucide-react";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { CredentialsFeature, SecretsFeature } from "@/components/admin/management/management-features";
import { useI18n } from "@/lib/i18n";

export function SecurityKeysFeature() {
  const { t } = useI18n();
  return (
    <div className="space-y-6">
      <header>
        <h1 className="flex items-center gap-2 text-2xl font-semibold"><KeyRound aria-hidden="true" className="h-6 w-6" />{t("admin.nav.item.securityKeys")}</h1>
      </header>
      <Tabs defaultValue="credentials">
        <TabsList aria-label={t("admin.nav.item.securityKeys")}>
          <TabsTrigger value="credentials">{t("admin.nav.item.credentials")}</TabsTrigger>
          <TabsTrigger value="secrets">{t("admin.nav.item.secrets")}</TabsTrigger>
        </TabsList>
        <TabsContent value="credentials"><CredentialsFeature /></TabsContent>
        <TabsContent value="secrets"><SecretsFeature /></TabsContent>
      </Tabs>
    </div>
  );
}
