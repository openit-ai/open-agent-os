import { screen, waitFor } from "@testing-library/react";
import DashboardPage from "./page";
import { getAdminReadiness, getSetupProgress } from "@/lib/admin-api/client";
import type { AdminReadiness, SetupProgress } from "@/lib/admin-api/types";
import { renderWithProviders } from "@/test/render";

const replace = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace }),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, getToken: () => "test-token", apiFetch: vi.fn().mockResolvedValue({}) };
});

vi.mock("@/lib/admin-api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/admin-api/client")>();
  return { ...actual, getAdminReadiness: vi.fn(), getSetupProgress: vi.fn() };
});

const progress: SetupProgress = {
  schema_version: "1",
  current_step: "ingress",
  required_complete: false,
  percent: 40,
  steps: [],
};

const readiness: AdminReadiness = {
  schema_version: "1",
  required_complete: false,
  checked_at: "2026-09-15T00:00:00Z",
  connections: [],
};

describe("DashboardPage onboarding readiness", () => {
  beforeEach(() => {
    replace.mockReset();
    sessionStorage.clear();
    vi.mocked(getSetupProgress).mockResolvedValue(progress);
    vi.mocked(getAdminReadiness).mockResolvedValue(readiness);
  });

  it("replaces the first incomplete session with setup and fetches readiness once", async () => {
    renderWithProviders(<DashboardPage />);
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/setup"));
    expect(getAdminReadiness).toHaveBeenCalledOnce();
  });

  it("shows continue setup after Later without completing required setup", async () => {
    sessionStorage.setItem("oaos_setup_deferred", "true");
    renderWithProviders(<DashboardPage />);
    expect(await screen.findByText("Required setup is not complete")).toBeVisible();
    expect(screen.getByRole("link", { name: "Continue setup" })).toHaveAttribute("href", "/setup?step=ingress");
    expect(progress.required_complete).toBe(false);
    expect(replace).not.toHaveBeenCalledWith("/setup");
  });
});
