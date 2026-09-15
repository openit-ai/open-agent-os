"use client";

import * as React from "react";
import { createPortal } from "react-dom";
import { X } from "lucide-react";
import { cn } from "@/lib/utils";

const FOCUSABLE = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "[tabindex]:not([tabindex='-1'])",
].join(",");

export interface DialogProps {
  open: boolean;
  onOpenChange(open: boolean): void;
  title: string;
  description?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
  initialFocusRef?: React.RefObject<HTMLElement | null>;
  triggerRef?: React.RefObject<HTMLElement | null>;
  closeLabel?: string;
}

export function Dialog({
  open,
  onOpenChange,
  title,
  description,
  children,
  className,
  initialFocusRef,
  triggerRef,
  closeLabel = "Close dialog",
}: DialogProps) {
  const titleId = React.useId();
  const descriptionId = React.useId();
  const panelRef = React.useRef<HTMLDivElement>(null);
  const returnFocusRef = React.useRef<HTMLElement | null>(null);
  const wasOpenRef = React.useRef(false);

  React.useLayoutEffect(() => {
    if (open && !wasOpenRef.current) {
      returnFocusRef.current = triggerRef?.current ?? (document.activeElement as HTMLElement | null);
      const first = initialFocusRef?.current ?? panelRef.current?.querySelector<HTMLElement>(FOCUSABLE);
      (first ?? panelRef.current)?.focus();
    }
    if (!open && wasOpenRef.current) {
      (triggerRef?.current ?? returnFocusRef.current)?.focus();
    }
    wasOpenRef.current = open;
  }, [initialFocusRef, open, triggerRef]);

  React.useEffect(() => () => {
    if (wasOpenRef.current) (triggerRef?.current ?? returnFocusRef.current)?.focus();
  }, [triggerRef]);

  function handleKeyDown(event: React.KeyboardEvent<HTMLDivElement>) {
    if (event.key === "Escape") {
      event.preventDefault();
      onOpenChange(false);
      return;
    }
    if (event.key !== "Tab") return;

    const focusable = Array.from(panelRef.current?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? []);
    if (focusable.length === 0) {
      event.preventDefault();
      panelRef.current?.focus();
      return;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  if (!open || typeof document === "undefined") return null;

  return createPortal(
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <button
        type="button"
        className="absolute inset-0 cursor-default bg-slate-950/50"
        aria-label={closeLabel}
        onClick={() => onOpenChange(false)}
      />
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={description ? descriptionId : undefined}
        tabIndex={-1}
        className={cn("relative z-10 max-h-[90vh] w-full max-w-lg overflow-y-auto rounded-lg border bg-background p-6 shadow-xl", className)}
        onKeyDown={handleKeyDown}
      >
        <div className="pr-10">
          <h2 id={titleId} className="text-lg font-semibold">{title}</h2>
          {description ? <div id={descriptionId} className="mt-2 text-sm text-muted-foreground">{description}</div> : null}
        </div>
        <button
          type="button"
          className="absolute right-4 top-4 rounded-sm p-1 text-muted-foreground hover:text-foreground"
          aria-label={closeLabel}
          onClick={() => onOpenChange(false)}
        >
          <X aria-hidden="true" className="h-4 w-4" />
        </button>
        <div className="mt-5">{children}</div>
      </div>
    </div>,
    document.body,
  );
}
