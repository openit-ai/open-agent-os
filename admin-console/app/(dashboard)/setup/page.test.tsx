import { fireEvent, screen, waitFor } from "@testing-library/react";
import SetupPage from "./page";
import { getSetupProgress } from "@/lib/admin-api/client";
import type { SetupProgress } from "@/lib/admin-api/types";
import { renderWithProviders } from "@/test/render";

const push = vi.fn();
const replace = vi.fn();
let requestedStep = "environment";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push, replace }),
  useSearchParams: () => new URLSearchParams(`step=${requestedStep}`),
}));

vi.mock("@/lib/admin-api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/admin-api/client")>();
  return { ...actual, getSetupProgress: vi.fn() };
});

const progress: SetupProgress = {
  schema_version: "1",
  current_step: "environment",
  required_complete: false,
  percent: 0,
  steps: [
    { id: "environment", status: "pending", blocking_checks: [], optional_skipped: false },
    { id: "runtime", status: "pending", blocking_checks: [], optional_skipped: false },
    { id: "ingress", status: "pending", blocking_checks: [], optional_skipped: false },
    { id: "policy", status: "pending", blocking_checks: [], optional_skipped: false },
    { id: "mcp", status: "pending", blocking_checks: [], optional_skipped: false },
    { id: "knowledge", status: "pending", blocking_checks: [], optional_skipped: false },
    { id: "notifications", status: "pending", blocking_checks: [], optional_skipped: false },
    { id: "verify", status: "pending", blocking_checks: [], optional_skipped: false },
  ],
};

describe("SetupPage", () => {
  beforeEach(() => {
    push.mockReset();
    replace.mockReset();
    requestedStep = "environment";
    sessionStorage.clear();
    vi.mocked(getSetupProgress).mockResolvedValue(progress);
  });

  it("syncs step movement to the URL and keeps next disabled for an incomplete required step", async () => {
    renderWithProviders(<SetupPage />);
    expect(await screen.findByRole("heading", { name: "Set up Open Agent OS" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: /2\. Execution path/ }));
    expect(push).toHaveBeenCalledWith("/setup?step=runtime", { scroll: false });
  });

  it("defers the current session without marking a required step complete", async () => {
    renderWithProviders(<SetupPage />);
    await screen.findByRole("heading", { name: "Set up Open Agent OS" });
    fireEvent.click(screen.getByRole("button", { name: "Later" }));

    expect(sessionStorage.getItem("oaos_setup_deferred")).toBe("true");
    expect(push).toHaveBeenCalledWith("/");
    await waitFor(() => expect(screen.getAllByText("Pending").length).toBeGreaterThan(0));
    expect(progress.steps[0].status).toBe("pending");
  });

  it("does not allow final verification while ingress is incomplete", async () => {
    renderWithProviders(<SetupPage />);
    await screen.findByRole("heading", { name: "Set up Open Agent OS" });
    fireEvent.click(screen.getByRole("button", { name: /8\. Final verification/ }));
    expect(push).not.toHaveBeenCalledWith("/setup?step=verify", { scroll: false });
  });

  it("explains the problem and exact action before showing the diagnostic code", async () => {
    vi.mocked(getSetupProgress).mockResolvedValue({
      ...progress,
      steps: progress.steps.map((step) => step.id === "environment" ? {
        ...step,
        status: "needs_attention",
        blocking_checks: [{
          code: "CHECK_REQUIRED",
          message_key: "admin.setup.checks.check_required",
          next_action: { label_key: "admin.readiness.actions.openHealth", href: "/operations/health" },
        }],
      } : step),
    });

    renderWithProviders(<SetupPage />);

    expect(await screen.findByText("No recent health check is available for this required connection.")).toBeVisible();
    expect(screen.getByText(/click Refresh, and confirm that the database and Control Plane show Healthy/)).toBeVisible();
    expect(screen.getByText("Diagnostic code: CHECK_REQUIRED")).toBeVisible();
    expect(screen.getByRole("link", { name: "Open the action screen" })).toHaveAttribute("href", "/operations/health");
  });

  it("gives Korean L5 publish instructions without auto-publishing policy", async () => {
    requestedStep = "policy";
    localStorage.setItem("oaos_lang", "ko");
    vi.mocked(getSetupProgress).mockResolvedValue({
      ...progress,
      current_step: "policy",
      steps: progress.steps.map((step) => step.id === "policy" ? {
        ...step,
        status: "needs_attention",
        blocking_checks: [{
          code: "ACTIVE_POLICY_MISSING",
          message_key: "admin.setup.checks.active_policy_missing",
          next_action: { label_key: "admin.readiness.actions.configurePolicy", href: "/control/policy" },
        }],
      } : step),
    });

    renderWithProviders(<SetupPage />);

    expect(await screen.findByText("발행되어 활성화된 정책 번들이 없습니다.")).toBeVisible();
    expect(screen.getByText(/L5 관리자로 정책 > 초안을 열어/)).toBeVisible();
    expect(screen.getByText(/OAOS는 정책을 자동 발행하지 않습니다/)).toBeVisible();
    expect(screen.getByText("진단 코드: ACTIVE_POLICY_MISSING")).toBeVisible();
    expect(screen.getByRole("link", { name: "조치 화면 열기" })).toHaveAttribute("href", "/control/policy");
  });
});
