"use client";

import * as React from "react";
import { TriangleAlert } from "lucide-react";
import { Dialog } from "@/components/admin/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useI18n } from "@/lib/i18n";

export interface ConfirmDialogProps {
  open: boolean;
  title: string;
  description: React.ReactNode;
  targetLabel?: string;
  consequence?: string;
  confirmLabel: string;
  cancelLabel?: string;
  tone?: "default" | "danger";
  requireText?: string;
  pending?: boolean;
  error?: string;
  onConfirm(): Promise<void> | void;
  onOpenChange(open: boolean): void;
}

export function ConfirmDialog({
  open,
  title,
  description,
  targetLabel,
  consequence,
  confirmLabel,
  cancelLabel,
  tone = "default",
  requireText,
  pending = false,
  error,
  onConfirm,
  onOpenChange,
}: ConfirmDialogProps) {
  const { t } = useI18n();
  const [confirmation, setConfirmation] = React.useState("");
  const [submitting, setSubmitting] = React.useState(false);
  const [submitError, setSubmitError] = React.useState<string>();
  const cancelRef = React.useRef<HTMLButtonElement>(null);
  const busy = pending || submitting;
  const blocked = Boolean(requireText && confirmation !== requireText);
  const inputId = React.useId();

  React.useEffect(() => {
    if (!open) {
      setConfirmation("");
      setSubmitError(undefined);
      setSubmitting(false);
    }
  }, [open]);

  async function handleConfirm() {
    if (blocked || busy) return;
    setSubmitError(undefined);
    setSubmitting(true);
    try {
      await onConfirm();
      onOpenChange(false);
    } catch {
      setSubmitError(t("admin.confirm.error"));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={(next) => !busy && onOpenChange(next)} title={title} description={description} initialFocusRef={cancelRef}>
      {tone === "danger" ? (
        <div className="mb-4 flex gap-2 rounded-md border border-status-danger bg-status-danger-surface p-3 text-sm text-status-danger-text">
          <TriangleAlert aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0" />
          <span>{consequence ?? description}</span>
        </div>
      ) : consequence ? <p className="mb-4 text-sm text-muted-foreground">{consequence}</p> : null}
      {targetLabel ? <p className="mb-4 break-all rounded-md bg-muted px-3 py-2 text-sm font-medium">{targetLabel}</p> : null}
      {requireText ? (
        <div className="mb-4 space-y-2">
          <Label htmlFor={inputId}>{t("admin.confirm.requiredText", { text: requireText })}</Label>
          <Input id={inputId} value={confirmation} onChange={(event) => setConfirmation(event.target.value)} autoComplete="off" disabled={busy} />
        </div>
      ) : null}
      {error || submitError ? <p role="alert" className="mb-4 text-sm text-status-danger-text">{error ?? submitError}</p> : null}
      <div className="flex justify-end gap-2">
        <Button ref={cancelRef} type="button" variant="outline" disabled={busy} onClick={() => onOpenChange(false)}>
          {cancelLabel ?? t("admin.common.action.cancel")}
        </Button>
        <Button type="button" variant={tone === "danger" ? "destructive" : "default"} disabled={blocked || busy} aria-busy={busy} onClick={handleConfirm}>
          {confirmLabel}
        </Button>
      </div>
    </Dialog>
  );
}
