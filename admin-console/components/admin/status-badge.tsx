"use client";

import * as React from "react";
import { CheckCircle2, CircleHelp, TriangleAlert, XCircle } from "lucide-react";
import { cn } from "@/lib/utils";
import { useI18n } from "@/lib/i18n";

export interface StatusBadgeProps {
  status: "healthy" | "warning" | "failed" | "unknown";
  label?: string;
  showIcon?: boolean;
  size?: "sm" | "md";
}

const presentation = {
  healthy: { Icon: CheckCircle2, className: "border-status-ok bg-status-ok-surface text-status-ok-text" },
  warning: { Icon: TriangleAlert, className: "border-status-warn bg-status-warn-surface text-status-warn-text" },
  failed: { Icon: XCircle, className: "border-status-danger bg-status-danger-surface text-status-danger-text" },
  unknown: { Icon: CircleHelp, className: "border-status-neutral-border bg-status-neutral-surface text-status-neutral-text" },
};

export function StatusBadge({ status, label, showIcon = true, size = "sm" }: StatusBadgeProps) {
  const { t } = useI18n();
  const { Icon, className } = presentation[status];
  const text = label ?? t(`admin.common.status.${status}`);
  return (
    <span className={cn("inline-flex items-center rounded-md border font-medium", size === "sm" ? "gap-1 px-2 py-0.5 text-xs" : "gap-1.5 px-2.5 py-1 text-sm", className)}>
      {showIcon ? <Icon aria-hidden="true" className={size === "sm" ? "h-3.5 w-3.5" : "h-4 w-4"} /> : null}
      <span>{text}</span>
    </span>
  );
}
