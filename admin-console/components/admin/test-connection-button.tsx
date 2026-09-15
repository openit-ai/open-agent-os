"use client";

import * as React from "react";
import Link from "next/link";
import { CheckCircle2, LoaderCircle, TriangleAlert, XCircle } from "lucide-react";
import { Button } from "@/components/ui/button";
import { testConnection, AdminApiError, normalizeAdminError } from "@/lib/admin-api/client";
import type { AdminError, TestConnectionResult } from "@/lib/admin-api/types";
import { useI18n } from "@/lib/i18n";

export interface TestConnectionButtonProps {
  connectionId: string;
  candidateId?: string;
  configRevision?: string;
  disabled?: boolean;
  onResult?(result: TestConnectionResult): void;
}

export function TestConnectionButton({ connectionId, candidateId, configRevision, disabled, onResult }: TestConnectionButtonProps) {
  const { t } = useI18n();
  const [pending, setPending] = React.useState(false);
  const [result, setResult] = React.useState<TestConnectionResult>();
  const [error, setError] = React.useState<AdminError>();
  const inFlightRef = React.useRef(false);
  const abortRef = React.useRef<AbortController | undefined>(undefined);

  React.useEffect(() => () => abortRef.current?.abort(), []);

  async function handleTest() {
    if (inFlightRef.current || disabled) return;
    inFlightRef.current = true;
    setPending(true);
    setResult(undefined);
    setError(undefined);
    const controller = new AbortController();
    abortRef.current = controller;
    try {
      const next = await testConnection(connectionId, {
        candidate_id: candidateId,
        config_revision: configRevision,
        mode: "safe",
      }, controller.signal);
      setResult(next);
      onResult?.(next);
    } catch (cause) {
      setError(cause instanceof AdminApiError ? cause.error : normalizeAdminError(cause));
    } finally {
      inFlightRef.current = false;
      setPending(false);
    }
  }

  const resultText = result ? (() => {
    const translated = t(result.message_key);
    return translated === result.message_key ? result.summary : translated;
  })() : null;
  const errorText = error ? t(error.message_key) : null;
  const isFullyHealthy = Boolean(result?.ok && result.applied && result.status === "healthy");
  const isWarning = Boolean(result && !isFullyHealthy && (result.status === "warning" || !result.applied));

  return (
    <div className="inline-flex flex-col items-start gap-2">
      <Button type="button" variant="outline" disabled={disabled || pending} aria-busy={pending} onClick={handleTest}>
        {pending ? <LoaderCircle aria-hidden="true" className="h-4 w-4 animate-spin motion-reduce:animate-none" /> : null}
        {pending ? t("admin.common.connectionTest.checking") : t("admin.common.connectionTest.start")}
      </Button>
      {result ? (
        <div role="status" className={isFullyHealthy ? "text-sm text-status-ok-text" : isWarning ? "text-sm text-status-warn-text" : "text-sm text-status-danger-text"}>
          <p className="flex items-center gap-1.5">
            {isFullyHealthy ? <CheckCircle2 aria-hidden="true" className="h-4 w-4" /> : isWarning ? <TriangleAlert aria-hidden="true" className="h-4 w-4" /> : <XCircle aria-hidden="true" className="h-4 w-4" />}
            <span><span className="font-medium">{result.code}</span> · {resultText}</span>
          </p>
          {!result.applied ? <p className="mt-1">{t("admin.connections.apply.savedNotApplied")}</p> : null}
          {result.next_action ? <Link className="mt-1 inline-flex font-medium underline" href={result.next_action.href}>{t(result.next_action.label_key)}</Link> : null}
        </div>
      ) : null}
      {error ? <p role="alert" className="flex items-center gap-1.5 text-sm text-status-danger-text"><XCircle aria-hidden="true" className="h-4 w-4" />{errorText}</p> : null}
    </div>
  );
}
