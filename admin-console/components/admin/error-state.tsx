import * as React from "react";
import { CircleAlert, ExternalLink, RotateCcw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

export interface ErrorStateProps {
  title: string;
  description: string;
  code?: string;
  correlationId?: string;
  retry?: () => void;
  helpHref?: string;
  compact?: boolean;
}

export function ErrorState({ title, description, code, correlationId, retry, helpHref, compact }: ErrorStateProps) {
  return (
    <div role="alert" className={cn("rounded-lg border border-status-danger bg-status-danger-surface text-status-danger-text", compact ? "p-4" : "p-6")}>
      <div className="flex gap-3">
        <CircleAlert aria-hidden="true" className="mt-0.5 h-5 w-5 shrink-0" />
        <div className="min-w-0">
          <h3 className="font-semibold">{title}</h3>
          <p className="mt-1 text-sm">{description}</p>
          {code ? <p className="mt-2 font-mono text-xs">{code}</p> : null}
          {correlationId ? <p className="mt-1 break-all font-mono text-xs">Correlation ID: {correlationId}</p> : null}
          {retry || helpHref ? (
            <div className="mt-4 flex flex-wrap gap-2">
              {retry ? <Button type="button" variant="outline" size="sm" onClick={retry}><RotateCcw aria-hidden="true" className="h-4 w-4" />Retry</Button> : null}
              {helpHref ? <a href={helpHref} className="inline-flex h-8 items-center gap-2 rounded-md px-3 text-xs font-medium underline">Help<ExternalLink aria-hidden="true" className="h-4 w-4" /></a> : null}
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}
