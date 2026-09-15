"use client";

import * as React from "react";
import { Label } from "@/components/ui/label";

export interface FormFieldProps {
  id: string;
  label: string;
  required?: boolean;
  hint?: React.ReactNode;
  error?: string;
  children: React.ReactElement;
}

export function FormField({ id, label, required, hint, error, children }: FormFieldProps) {
  const hintId = `${id}-hint`;
  const errorId = `${id}-error`;
  const child = children as React.ReactElement<Record<string, unknown>>;
  const describedBy = [child.props["aria-describedby"], hint ? hintId : null, error ? errorId : null]
    .filter(Boolean)
    .join(" ");

  return (
    <div className="space-y-2">
      <Label htmlFor={id}>
        {label}
        {required ? <span className="ml-1 text-status-danger-text" aria-hidden="true">*</span> : null}
        {required ? <span className="sr-only"> required</span> : null}
      </Label>
      {hint ? <div id={hintId} className="text-sm text-muted-foreground">{hint}</div> : null}
      {React.cloneElement(child, {
        id,
        required: required || undefined,
        "aria-invalid": error ? true : child.props["aria-invalid"],
        "aria-describedby": describedBy || undefined,
      })}
      {error ? <p id={errorId} className="text-sm text-status-danger-text">{error}</p> : null}
    </div>
  );
}
