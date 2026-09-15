"use client";

import * as React from "react";
import { CheckCircle2, Circle, CircleDashed, TriangleAlert } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { useI18n } from "@/lib/i18n";
import type { SetupStepStatus } from "@/lib/admin-api/types";

export interface StepWizardProps {
  steps: Array<{ id: string; label: string; required: boolean; status: SetupStepStatus }>;
  currentStep: string;
  progress: { requiredDone: number; requiredTotal: number; optionalDone: number; optionalTotal: number };
  onStepChange(id: string): void;
  onNext(): Promise<void> | void;
  onBack(): void;
  onSkip?(): void;
  canContinue: boolean;
  busy?: boolean;
  children: React.ReactNode;
}

const statusPresentation = {
  complete: { Icon: CheckCircle2, className: "text-status-ok-text" },
  needs_attention: { Icon: TriangleAlert, className: "text-status-danger-text" },
  checking: { Icon: CircleDashed, className: "animate-spin text-status-warn-text motion-reduce:animate-none" },
  skipped: { Icon: Circle, className: "text-status-neutral-text" },
  pending: { Icon: Circle, className: "text-status-neutral-text" },
};

export function StepWizard({
  steps,
  currentStep,
  progress,
  onStepChange,
  onNext,
  onBack,
  onSkip,
  canContinue,
  busy = false,
  children,
}: StepWizardProps) {
  const { t } = useI18n();
  const currentIndex = steps.findIndex((step) => step.id === currentStep);
  const percent = progress.requiredTotal === 0 ? 0 : Math.round((progress.requiredDone / progress.requiredTotal) * 100);

  return (
    <div className="grid gap-6 lg:grid-cols-[18rem_minmax(0,1fr)]">
      <aside aria-label={t("admin.setup.wizard.stepsLabel")} className="rounded-lg border bg-card p-4">
        <p className="text-sm font-semibold">{t("admin.setup.wizard.progressTitle")}</p>
        <p className="mt-1 text-sm text-muted-foreground">
          {t("admin.setup.wizard.requiredProgress", { done: progress.requiredDone, total: progress.requiredTotal, percent })}
        </p>
        <div
          className="mt-3 h-2 overflow-hidden rounded-full bg-muted"
          role="progressbar"
          aria-label={t("admin.setup.wizard.requiredProgressLabel")}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={percent}
        >
          <div className="h-full bg-primary transition-[width] duration-200 motion-reduce:transition-none" style={{ width: `${percent}%` }} />
        </div>
        <p className="mt-2 text-xs text-muted-foreground">
          {t("admin.setup.wizard.optionalProgress", { done: progress.optionalDone, total: progress.optionalTotal })}
        </p>

        <ol className="mt-5 space-y-1">
          {steps.map((step, index) => {
            const { Icon, className } = statusPresentation[step.status];
            const active = step.id === currentStep;
            return (
              <li key={step.id}>
                <button
                  type="button"
                  aria-current={active ? "step" : undefined}
                  onClick={() => onStepChange(step.id)}
                  className={cn(
                    "flex w-full items-start gap-3 rounded-md px-3 py-2 text-left text-sm transition-colors duration-200 motion-reduce:transition-none",
                    active ? "bg-primary text-primary-foreground" : "hover:bg-accent",
                  )}
                >
                  <Icon aria-hidden="true" className={cn("mt-0.5 h-4 w-4 shrink-0", active ? "text-primary-foreground" : className)} />
                  <span className="min-w-0 flex-1">
                    <span className="block font-medium">{index + 1}. {step.label}</span>
                    <span className={cn("block text-xs", active ? "text-primary-foreground/80" : "text-muted-foreground")}>
                      {step.required ? t("admin.setup.wizard.required") : t("admin.setup.wizard.optional")}
                      {" · "}{t(`admin.setup.status.${step.status}`)}
                    </span>
                  </span>
                </button>
              </li>
            );
          })}
        </ol>
      </aside>

      <section aria-labelledby="setup-current-step" className="min-w-0 rounded-lg border bg-card p-5 md:p-6">
        {children}
        <div className="mt-8 flex flex-wrap items-center justify-between gap-3 border-t pt-5">
          <Button type="button" variant="outline" disabled={currentIndex <= 0 || busy} onClick={onBack}>
            {t("admin.setup.wizard.back")}
          </Button>
          <div className="flex flex-wrap gap-2">
            {onSkip ? (
              <Button type="button" variant="ghost" disabled={busy} onClick={onSkip}>
                {t("admin.setup.wizard.later")}
              </Button>
            ) : null}
            <Button type="button" disabled={!canContinue || busy} onClick={onNext}>
              {currentIndex === steps.length - 1 ? t("admin.setup.wizard.finish") : t("admin.setup.wizard.next")}
            </Button>
          </div>
        </div>
      </section>
    </div>
  );
}
