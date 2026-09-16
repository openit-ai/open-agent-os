import { screen } from "@testing-library/react";
import { getAcpConfig, getRuntimeMode } from "@/lib/api";
import { renderWithProviders } from "@/test/render";
import { HermesAgentFeature } from "./hermes-agent-feature";

const replace = vi.fn();

vi.mock("next/navigation", () => ({ useRouter: () => ({ replace }) }));
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, getToken: () => "token", getRuntimeMode: vi.fn(), getAcpConfig: vi.fn() };
});

describe("HermesAgentFeature", () => {
  beforeEach(() => {
    replace.mockReset();
    vi.mocked(getAcpConfig).mockResolvedValue({
      hermes_base_url: "http://127.0.0.1:8642",
      hermes_model: "muse-spark-1.2-contributor",
      acp_enabled: true,
      api_key_set: true,
      applied: true,
      source: "env",
    });
  });

  it("derives the active Hermes path, status, and default model from existing APIs", async () => {
    vi.mocked(getRuntimeMode).mockResolvedValue({ mode: "hermes", available_modes: ["hermes", "llm"] });
    renderWithProviders(<HermesAgentFeature />);

    expect(await screen.findByText("muse-spark-1.2-contributor")).toBeVisible();
    expect(screen.getAllByText("Hermes Agent").length).toBeGreaterThan(0);
    expect(screen.getByText("Active")).toBeVisible();
    expect(screen.getByText(/ACP is the adapter protocol for Hermes/)).toBeVisible();
    expect(screen.getByRole("link", { name: /Open ACP technical settings/ })).toHaveAttribute("href", "/control/acp");
  });

  it("shows external LLM and standby when Hermes is not the selected runtime", async () => {
    vi.mocked(getRuntimeMode).mockResolvedValue({ mode: "llm", available_modes: ["hermes", "llm"] });
    renderWithProviders(<HermesAgentFeature />);

    expect(await screen.findByText("external LLM")).toBeVisible();
    expect(screen.getByText("Standby")).toBeVisible();
  });
});
