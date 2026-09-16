"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { CheckCircle2, ExternalLink, RefreshCw, TriangleAlert } from "lucide-react";
import { Button } from "@/components/ui/button";
import { ErrorState, Skeleton, StepWizard } from "@/components/admin";
import { getSetupProgress, normalizeAdminError } from "@/lib/admin-api/client";
import { adminKeys } from "@/lib/admin-api/keys";
import type { SetupProgressStep, SetupStepId } from "@/lib/admin-api/types";
import { deferSetupForSession } from "@/lib/setup-session";
import { useI18n } from "@/lib/i18n";

const STEP_IDS: SetupStepId[] = ["environment", "runtime", "ingress", "policy", "mcp", "knowledge", "notifications", "verify"];
const REQUIRED_STEPS = new Set<SetupStepId>(["environment", "runtime", "ingress", "policy", "verify"]);
const FINAL_PREREQUISITES = new Set<SetupStepId>(["environment", "runtime", "ingress", "policy"]);

const ACTION_HREF: Record<SetupStepId, string> = {
  environment: "/operations/health",
  runtime: "/connections/harness/llm-runtime",
  ingress: "/connections",
  policy: "/control/policy",
  mcp: "/control/mcp",
  knowledge: "/connections/knowledge/operations",
  notifications: "/connections",
  verify: "/setup",
};

function isStepId(value: string | null): value is SetupStepId {
  return value != null && STEP_IDS.includes(value as SetupStepId);
}

function nextStep(id: SetupStepId) {
  return STEP_IDS[Math.min(STEP_IDS.indexOf(id) + 1, STEP_IDS.length - 1)];
}

function previousStep(id: SetupStepId) {
  return STEP_IDS[Math.max(STEP_IDS.indexOf(id) - 1, 0)];
}

export default function SetupPage() {
  const { t } = useI18n();
  return (
    <React.Suspense fallback={<Skeleton variant="form" rows={6} ariaLabel={t("admin.setup.page.loading")} />}>
      <SetupPageContent />
    </React.Suspense>
  );
}

function SetupPageContent() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const { t } = useI18n();
  const requestedStep = searchParams.get("step");
  const [localSkipped, setLocalSkipped] = React.useState<Set<SetupStepId>>(() => new Set());
  const initialStep = isStepId(requestedStep) ? requestedStep : "environment";
  const [currentStep, setCurrentStep] = React.useState<SetupStepId>(initialStep);
  const previousCheckedStep = React.useRef<SetupStepId>(initialStep);

  const progressQuery = useQuery({
    queryKey: adminKeys.setupProgress(),
    queryFn: ({ signal }) => getSetupProgress(signal),
    retry: false,
  });
  const progress = progressQuery.data;

  React.useEffect(() => {
    if (!progress) return;
    const next = isStepId(requestedStep) ? requestedStep : progress.current_step;
    setCurrentStep(next);
    if (!isStepId(requestedStep)) router.replace(`/setup?step=${next}`, { scroll: false });
  }, [progress, requestedStep, router]);

  React.useEffect(() => {
    if (!isStepId(requestedStep) || requestedStep === currentStep) return;
    setCurrentStep(requestedStep);
  }, [currentStep, requestedStep]);

  React.useEffect(() => {
    if (previousCheckedStep.current === currentStep) return;
    previousCheckedStep.current = currentStep;
    void progressQuery.refetch();
  }, [currentStep, progressQuery]);

  function changeStep(id: string) {
    if (!isStepId(id)) return;
    const prerequisitesComplete = progress?.steps
      .filter((step) => FINAL_PREREQUISITES.has(step.id))
      .every((step) => step.status === "complete");
    if (id === "verify" && !prerequisitesComplete) return;
    router.push(`/setup?step=${id}`, { scroll: false });
  }

  if (progressQuery.isPending) {
    return <div aria-live="polite"><Skeleton variant="form" rows={6} ariaLabel={t("admin.setup.page.loading")} /></div>;
  }

  if (progressQuery.isError || !progress) {
    const error = normalizeAdminError(progressQuery.error);
    return <ErrorState title={t("admin.setup.page.loadFailed")} description={t(error.message_key)} code={error.code} correlationId={error.correlation_id} retry={() => void progressQuery.refetch()} />;
  }

  const stepsById = new Map(progress.steps.map((step) => [step.id, step]));
  const current = stepsById.get(currentStep) ?? progress.steps[0];
  const requiredSteps = progress.steps.filter((step) => REQUIRED_STEPS.has(step.id));
  const optionalSteps = progress.steps.filter((step) => !REQUIRED_STEPS.has(step.id));
  const prerequisitesComplete = progress.steps.filter((step) => FINAL_PREREQUISITES.has(step.id)).every((step) => step.status === "complete");
  const canContinue = Boolean(current && (
    !REQUIRED_STEPS.has(current.id)
    || (current.status === "complete" && (current.id !== "verify" || prerequisitesComplete))
  ));

  if (progress.required_complete) {
    return (
      <div className="space-y-6">
        <div><h1 className="text-2xl font-semibold">{t("admin.setup.complete.title")}</h1><p className="mt-1 text-sm text-muted-foreground">{t("admin.setup.complete.description")}</p></div>
        <div className="rounded-lg border border-status-ok bg-status-ok-surface p-5 text-status-ok-text">
          <div className="flex gap-3"><CheckCircle2 aria-hidden="true" className="h-5 w-5" /><p className="font-semibold">{t("admin.setup.complete.requiredReady")}</p></div>
          <p className="mt-2 text-sm">{t("admin.setup.complete.automaticStatus")}</p>
        </div>
        <SetupStatusList steps={progress.steps} />
        <Button type="button" variant="outline" disabled={progressQuery.isFetching} onClick={() => void progressQuery.refetch()}>
          <RefreshCw aria-hidden="true" className={progressQuery.isFetching ? "h-4 w-4 animate-spin motion-reduce:animate-none" : "h-4 w-4"} />
          {progressQuery.isFetching ? t("admin.setup.check.checking") : t("admin.setup.check.recheck")}
        </Button>
      </div>
    );
  }

  if (!current) return <ErrorState title={t("admin.setup.page.invalidProgress")} description={t("admin.setup.page.invalidProgressDescription")} />;

  return (
    <div className="space-y-6">
      <div><h1 className="text-2xl font-semibold">{t("admin.setup.page.title")}</h1><p className="mt-1 text-sm text-muted-foreground">{t("admin.setup.page.description")}</p></div>
      <StepWizard
        steps={STEP_IDS.map((id) => {
          const item = stepsById.get(id);
          return { id, label: t(`admin.setup.steps.${id}.title`), required: REQUIRED_STEPS.has(id), status: localSkipped.has(id) && !REQUIRED_STEPS.has(id) ? "skipped" : item?.status ?? "pending" };
        })}
        currentStep={currentStep}
        progress={{ requiredDone: requiredSteps.filter((step) => step.status === "complete").length, requiredTotal: requiredSteps.length, optionalDone: optionalSteps.filter((step) => step.status === "complete").length, optionalTotal: optionalSteps.length }}
        onStepChange={changeStep}
        onBack={() => changeStep(previousStep(currentStep))}
        onNext={() => currentStep === "verify" ? router.push("/") : changeStep(nextStep(currentStep))}
        onSkip={() => {
          if (REQUIRED_STEPS.has(currentStep)) { deferSetupForSession(); router.push("/"); return; }
          setLocalSkipped((value) => new Set(value).add(currentStep));
          changeStep(nextStep(currentStep));
        }}
        canContinue={canContinue}
        busy={progressQuery.isFetching}
      >
        <div aria-live="polite">
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div>
              <p className="text-sm font-medium text-primary">{t("admin.setup.wizard.stepNumber", { current: STEP_IDS.indexOf(currentStep) + 1, total: STEP_IDS.length })}</p>
              <h2 id="setup-current-step" className="mt-1 text-xl font-semibold">{t(`admin.setup.steps.${currentStep}.title`)}</h2>
              <p className="mt-2 text-sm text-muted-foreground">{t(`admin.setup.steps.${currentStep}.description`)}</p>
            </div>
            <Button type="button" variant="outline" disabled={progressQuery.isFetching} onClick={() => void progressQuery.refetch()}>
              <RefreshCw aria-hidden="true" className={progressQuery.isFetching ? "h-4 w-4 animate-spin motion-reduce:animate-none" : "h-4 w-4"} />
              {progressQuery.isFetching ? t("admin.setup.check.checking") : t("admin.setup.check.recheck")}
            </Button>
          </div>
          {progressQuery.isFetching ? <div className="mt-6"><Skeleton variant="card" ariaLabel={t("admin.setup.check.checking")} /></div> : <StepCheckResult step={current} actionHref={ACTION_HREF[currentStep]} />}
          {currentStep === "verify" && !prerequisitesComplete ? <div role="alert" className="mt-4 rounded-md border border-status-danger bg-status-danger-surface p-4 text-sm text-status-danger-text">{t("admin.setup.steps.verify.ingressRequired")}</div> : null}
        </div>
      </StepWizard>
    </div>
  );
}

function StepCheckResult({ step, actionHref }: { step: SetupProgressStep; actionHref: string }) {
  const { t, lang } = useI18n();
  const checkedDate = step.checked_at ? new Date(step.checked_at) : null;
  const checkedAt = checkedDate && !Number.isNaN(checkedDate.getTime()) ? new Intl.DateTimeFormat(lang, { dateStyle: "medium", timeStyle: "short" }).format(checkedDate) : t("admin.setup.check.neverChecked");
  return (
    <div className="mt-6 space-y-4">
      <div className={step.status === "complete" ? "rounded-md border border-status-ok bg-status-ok-surface p-4 text-status-ok-text" : "rounded-md border border-status-warn bg-status-warn-surface p-4 text-status-warn-text"}>
        <div className="flex gap-3">{step.status === "complete" ? <CheckCircle2 aria-hidden="true" className="h-5 w-5 shrink-0" /> : <TriangleAlert aria-hidden="true" className="h-5 w-5 shrink-0" />}<div><p className="font-semibold">{t(`admin.setup.status.${step.status}`)}</p><p className="mt-1 text-sm">{t("admin.setup.check.checkedAt", { time: checkedAt })}</p></div></div>
      </div>
      {step.blocking_checks.length > 0 ? (
        <ul className="space-y-3" aria-label={t("admin.setup.check.blockingIssues")}>
          {step.blocking_checks.map((check) => {
            const problemKey = `admin.setup.guidance.problems.${check.code}`;
            const translatedProblem = t(problemKey);
            const legacyMessage = t(check.message_key);
            const problem = translatedProblem !== problemKey
              ? translatedProblem
              : legacyMessage !== check.message_key ? legacyMessage : check.summary ?? check.code;
            const specificActionKey = `admin.setup.guidance.actions.${step.id}.${check.code}`;
            const defaultActionKey = `admin.setup.guidance.actions.${step.id}.default`;
            const specificAction = t(specificActionKey);
            const action = specificAction !== specificActionKey ? specificAction : t(defaultActionKey);
            return (
              <li key={`${step.id}-${check.code}`} className="rounded-md border border-status-danger bg-status-danger-surface p-4 text-status-danger-text">
                <p className="font-semibold">{problem}</p>
                <p className="mt-2 text-sm"><span className="font-semibold">{t("admin.setup.guidance.whatToDo")}</span> {action}</p>
                <p className="mt-2 font-mono text-xs opacity-80">{t("admin.setup.guidance.codeLabel")}: {check.code}</p>
                <Link href={actionHref} className="mt-3 inline-flex items-center gap-1 text-sm font-medium underline">{t("admin.setup.guidance.openAction")}<ExternalLink aria-hidden="true" className="h-3.5 w-3.5" /></Link>
              </li>
            );
          })}
        </ul>
      ) : null}
      {step.blocking_checks.length === 0 ? <Link href={actionHref} className="inline-flex items-center gap-1 text-sm font-medium text-primary underline">{t("admin.setup.check.openSettings")}<ExternalLink aria-hidden="true" className="h-3.5 w-3.5" /></Link> : null}
    </div>
  );
}

function SetupStatusList({ steps }: { steps: SetupProgressStep[] }) {
  const { t } = useI18n();
  return <ul className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">{steps.map((step) => <li key={step.id} className="rounded-lg border bg-card p-4"><p className="font-medium">{t(`admin.setup.steps.${step.id}.title`)}</p><p className="mt-1 text-sm text-muted-foreground">{t(`admin.setup.status.${step.status}`)}</p></li>)}</ul>;
}
