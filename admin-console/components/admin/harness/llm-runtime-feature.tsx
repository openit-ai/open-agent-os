"use client";

import { Cpu } from "lucide-react";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import ProvidersFeature from "@/components/admin/harness/providers-feature";
import FallbackFeature from "@/components/admin/harness/fallback-feature";
import { useI18n } from "@/lib/i18n";

export function LLMRuntimeFeature() {
  const { t } = useI18n();
  return (
    <div className="space-y-6">
      <header>
        <h1 className="flex items-center gap-2 text-2xl font-semibold"><Cpu aria-hidden="true" className="h-6 w-6" />{t("admin.harness.llmRuntime.title")}</h1>
        <p className="mt-1 text-sm text-muted-foreground">{t("admin.harness.llmRuntime.description")}</p>
      </header>
      <Tabs defaultValue="providers">
        <TabsList aria-label={t("admin.harness.llmRuntime.title")}>
          <TabsTrigger value="providers">{t("admin.harness.llmRuntime.providersSection")}</TabsTrigger>
          <TabsTrigger value="fallback">{t("admin.harness.llmRuntime.fallbackSection")}</TabsTrigger>
        </TabsList>
        <TabsContent value="providers"><ProvidersFeature /></TabsContent>
        <TabsContent value="fallback"><FallbackFeature /></TabsContent>
      </Tabs>
    </div>
  );
}
