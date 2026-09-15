"use client";

import * as React from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { Skeleton } from "@/components/admin";
import { resolveInfraDestination } from "@/lib/infra-compat";
import { useI18n } from "@/lib/i18n";

export default function InfraCompatibilityPage() {
  const { t } = useI18n();
  return <React.Suspense fallback={<Skeleton variant="card" ariaLabel={t("admin.infraResolver.resolving")} />}><InfraCompatibilityResolver /></React.Suspense>;
}

function InfraCompatibilityResolver() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const { t } = useI18n();

  React.useEffect(() => {
    const replace = () => router.replace(resolveInfraDestination(searchParams.toString(), window.location.hash));
    replace();
    window.addEventListener("hashchange", replace);
    return () => window.removeEventListener("hashchange", replace);
  }, [router, searchParams]);

  return <div aria-live="polite"><Skeleton variant="card" ariaLabel={t("admin.infraResolver.resolving")} /></div>;
}
