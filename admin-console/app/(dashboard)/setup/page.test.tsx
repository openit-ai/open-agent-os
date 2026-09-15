import { fireEvent, screen, waitFor } from "@testing-library/react";
import SetupPage from "./page";
import { getSetupProgress } from "@/lib/admin-api/client";
import type { SetupProgress } from "@/lib/admin-api/types";
import { renderWithProviders } from "@/test/render";

const push = vi.fn();
const replace = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push, replace }),
  useSearchParams: () => new URLSearchParams("step=environment"),
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
});
