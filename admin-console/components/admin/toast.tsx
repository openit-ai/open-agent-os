"use client";

import * as React from "react";
import { CheckCircle2, CircleAlert, Info, TriangleAlert, X } from "lucide-react";
import { cn } from "@/lib/utils";
import { useI18n } from "@/lib/i18n";

export interface ToastInput {
  id?: string;
  title: string;
  description?: string;
  variant: "success" | "warning" | "error" | "info";
  action?: { label: string; onClick(): void };
  durationMs?: number;
}

interface ToastItem extends ToastInput {
  id: string;
  count: number;
}

interface ToastContextValue {
  toast(input: ToastInput): string;
  dismiss(id: string): void;
}

const ToastContext = React.createContext<ToastContextValue | null>(null);
let toastSequence = 0;

const variantStyle = {
  success: "border-status-ok bg-status-ok-surface text-status-ok-text",
  warning: "border-status-warn bg-status-warn-surface text-status-warn-text",
  error: "border-status-danger bg-status-danger-surface text-status-danger-text",
  info: "border-status-neutral-border bg-status-neutral-surface text-status-neutral-text",
};

const variantIcon = {
  success: CheckCircle2,
  warning: TriangleAlert,
  error: CircleAlert,
  info: Info,
};

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [items, setItems] = React.useState<ToastItem[]>([]);
  const { t } = useI18n();

  const dismiss = React.useCallback((id: string) => {
    setItems((current) => current.filter((item) => item.id !== id));
  }, []);

  const toast = React.useCallback((input: ToastInput) => {
    let resultId = input.id ?? "";
    setItems((current) => {
      const duplicate = current.find((item) =>
        item.variant === input.variant && item.title === input.title && item.description === input.description,
      );
      if (duplicate) {
        resultId = duplicate.id;
        return current.map((item) => item.id === duplicate.id ? { ...item, ...input, id: item.id, count: item.count + 1 } : item);
      }
      resultId = input.id ?? `admin-toast-${++toastSequence}`;
      return [...current, { ...input, id: resultId, count: 1 }];
    });
    return resultId;
  }, []);

  React.useEffect(() => {
    const timers = items.flatMap((item) => {
      const duration = item.durationMs ?? (item.variant === "error" ? 0 : 5000);
      return duration > 0 ? [window.setTimeout(() => dismiss(item.id), duration)] : [];
    });
    return () => timers.forEach(window.clearTimeout);
  }, [dismiss, items]);

  const value = React.useMemo(() => ({ toast, dismiss }), [dismiss, toast]);
  const assertive = items.some((item) => item.variant === "error");

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div
        className="fixed right-4 top-4 z-[60] flex w-[min(24rem,calc(100vw-2rem))] flex-col gap-2"
        aria-live={assertive ? "assertive" : "polite"}
        aria-atomic="false"
        role={assertive ? "alert" : "status"}
      >
        {items.map((item) => {
          const Icon = variantIcon[item.variant];
          return (
            <div key={item.id} className={cn("flex gap-3 rounded-lg border p-4 shadow-lg", variantStyle[item.variant])}>
              <Icon aria-hidden="true" className="mt-0.5 h-5 w-5 shrink-0" />
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2 font-medium">
                  <span>{item.title}</span>
                  {item.count > 1 ? <span className="text-xs">{t("admin.toast.duplicateCount", { count: item.count })}</span> : null}
                </div>
                {item.description ? <p className="mt-1 text-sm">{item.description}</p> : null}
                {item.action ? (
                  <button type="button" className="mt-2 text-sm font-medium underline" onClick={item.action.onClick}>
                    {item.action.label}
                  </button>
                ) : null}
              </div>
              <button type="button" className="self-start rounded-sm p-1" aria-label={t("admin.toast.dismiss")} onClick={() => dismiss(item.id)}>
                <X aria-hidden="true" className="h-4 w-4" />
              </button>
            </div>
          );
        })}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast(): ToastContextValue {
  const context = React.useContext(ToastContext);
  if (!context) throw new Error("useToast must be used within ToastProvider");
  return context;
}
