"use client";

import Link from "next/link";
import { TriangleAlert } from "lucide-react";
import { useI18n } from "@/lib/i18n";

export interface ApplyStateBannerProps {
  applied: boolean;
  affectedServices: string[];
  operationHref?: string;
}

export function ApplyStateBanner({ applied, affectedServices, operationHref }: ApplyStateBannerProps) {
  const { t } = useI18n();
  if (applied) return null;

  return (
    <div role="status" className="rounded-lg border border-status-warn bg-status-warn-surface p-4 text-status-warn-text">
      <div className="flex gap-3">
        <TriangleAlert aria-hidden="true" className="mt-0.5 h-5 w-5 shrink-0" />
        <div>
          <p className="font-semibold">{t("admin.connections.apply.savedNotApplied")}</p>
          <p className="mt-1 text-sm">{t("admin.connections.apply.affectedServices", { services: affectedServices.join(", ") })}</p>
          <p className="mt-1 text-sm">{t("admin.connections.apply.operatorAction")}</p>
          {operationHref ? <Link href={operationHref} className="mt-2 inline-flex text-sm font-medium underline">{t("admin.connections.apply.openProcedure")}</Link> : null}
        </div>
      </div>
    </div>
  );
}
