import * as React from "react";
import { SearchX } from "lucide-react";
import { Button } from "@/components/ui/button";
import type { ActionProps } from "@/lib/admin-api/types";

export interface EmptyStateProps {
  icon?: React.ComponentType;
  title: string;
  description: string;
  primaryAction?: ActionProps;
  secondaryAction?: ActionProps;
  filtered?: boolean;
}

function Action({ action, secondary = false }: { action: ActionProps; secondary?: boolean }) {
  if (action.href) {
    return <a href={action.href} aria-disabled={action.disabled || undefined} className="inline-flex h-9 items-center rounded-md border px-4 text-sm font-medium">{action.label}</a>;
  }
  return <Button type="button" variant={secondary ? "outline" : "default"} disabled={action.disabled} onClick={action.onClick}>{action.label}</Button>;
}

export function EmptyState({ icon: Icon, title, description, primaryAction, secondaryAction, filtered }: EmptyStateProps) {
  const StateIcon = Icon ?? (filtered ? SearchX : undefined);
  return (
    <div className="flex min-h-48 flex-col items-center justify-center rounded-lg border border-dashed bg-status-neutral-surface p-8 text-center">
      {StateIcon ? <StateIcon aria-hidden="true" /> : null}
      <h3 className="mt-3 font-semibold text-status-neutral-text">{title}</h3>
      <p className="mt-1 max-w-md text-sm text-muted-foreground">{description}</p>
      {primaryAction || secondaryAction ? (
        <div className="mt-4 flex flex-wrap justify-center gap-2">
          {primaryAction ? <Action action={primaryAction} /> : null}
          {secondaryAction ? <Action action={secondaryAction} secondary /> : null}
        </div>
      ) : null}
    </div>
  );
}
