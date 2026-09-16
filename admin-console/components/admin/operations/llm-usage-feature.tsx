"use client";

import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import LLMUsageFeature from "@/components/admin/operations/llm-usage-dashboard-feature";
import QuotaFeature from "@/components/admin/operations/quota-feature";
import { useI18n } from "@/lib/i18n";

export function LLMUsageAndQuotaFeature() {
  const { t } = useI18n();
  return (
    <Tabs defaultValue="usage">
      <TabsList aria-label={t("admin.nav.item.usage")}>
        <TabsTrigger value="usage">{t("admin.nav.item.usage")}</TabsTrigger>
        <TabsTrigger value="quota">{t("admin.nav.item.quota")}</TabsTrigger>
      </TabsList>
      <TabsContent value="usage"><LLMUsageFeature /></TabsContent>
      <TabsContent value="quota"><QuotaFeature /></TabsContent>
    </Tabs>
  );
}
