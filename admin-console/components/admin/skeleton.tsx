import * as React from "react";
import { cn } from "@/lib/utils";

export interface SkeletonProps {
  variant: "text" | "card" | "table" | "form";
  rows?: number;
  ariaLabel?: string;
}

export function Skeleton({ variant, rows = variant === "table" ? 5 : 3, ariaLabel = "Loading content" }: SkeletonProps) {
  return (
    <div role="status" aria-label={ariaLabel} className="animate-pulse space-y-3 motion-reduce:animate-none">
      <span className="sr-only">{ariaLabel}</span>
      {variant === "card" ? <div className="h-32 rounded-lg bg-muted" /> : null}
      {variant === "form" ? Array.from({ length: rows }, (_, index) => (
        <div key={index} className="space-y-2">
          <div className="h-4 w-1/4 rounded bg-muted" />
          <div className="h-9 w-full rounded bg-muted" />
        </div>
      )) : null}
      {variant === "text" ? Array.from({ length: rows }, (_, index) => (
        <div key={index} className={cn("h-4 rounded bg-muted", index === rows - 1 ? "w-2/3" : "w-full")} />
      )) : null}
      {variant === "table" ? (
        <div className="overflow-hidden rounded-lg border">
          <div className="h-10 bg-muted" />
          {Array.from({ length: rows }, (_, index) => <div key={index} className="h-11 border-t bg-muted/50" />)}
        </div>
      ) : null}
    </div>
  );
}
