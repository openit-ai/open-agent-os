import { fireEvent, screen, waitFor } from "@testing-library/react";
import { ConnectionFlow } from "./connection-flow";
import { getConnectionDiscovery } from "@/lib/admin-api/client";
import type { ConnectionDiscoveryCandidate } from "@/lib/admin-api/types";
import { renderWithProviders } from "@/test/render";

vi.mock("@/lib/admin-api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/admin-api/client")>();
  return { ...actual, getConnectionDiscovery: vi.fn(), testConnection: vi.fn() };
});

describe("ConnectionFlow", () => {
  it("shows a spinning loading indicator while discovery is pending", () => {
    vi.mocked(getConnectionDiscovery).mockReturnValue(new Promise(() => undefined));

    const { container } = renderWithProviders(
      <ConnectionFlow kind="notion" title="Notion" description="Connection" serviceLabel="Notion">
        <div>Notion settings</div>
      </ConnectionFlow>,
    );

    expect(screen.getByRole("status", { name: "Discovering connection candidates" })).toBeVisible();
    expect(container.querySelector(".animate-spin")).toBeTruthy();
  });

  it("renders masked discovery metadata before closed advanced inputs", async () => {
    vi.mocked(getConnectionDiscovery).mockResolvedValue({
      kind: "mattermost",
      candidates: [{
        candidate_id: "cand-safe",
        kind: "mattermost",
        display_target: "mattermost.internal:8065",
        source: "env",
        confidence: "high",
        credential_state: "available",
        applied: false,
        requires_restart: true,
        secret_value: "secret-token-value",
      } as ConnectionDiscoveryCandidate & { secret_value: string }],
      reasons: [],
    });

    const { container } = renderWithProviders(
      <ConnectionFlow kind="mattermost" title="Mattermost" description="Connection" serviceLabel="Mattermost">
        <label>Token<input type="password" /></label>
      </ConnectionFlow>,
    );

    expect(await screen.findByText("mattermost.internal:8065")).toBeVisible();
    expect(screen.getByText("Recommended")).toBeVisible();
    expect(container).toHaveTextContent("Saved, but not yet applied");
    expect(container.querySelector("details")).not.toHaveAttribute("open");
    expect(container.textContent).not.toContain("secret-token-value");
    await waitFor(() => expect(screen.getByRole("button", { name: "Test connection" })).toBeEnabled());
  });

  it("renders authorization-required candidates as a next step, not a secret input", async () => {
    vi.mocked(getConnectionDiscovery).mockResolvedValue({
      kind: "oauth",
      candidates: [{
        candidate_id: "cand-auth",
        kind: "oauth",
        display_target: "oauth-provider",
        source: "default",
        confidence: "high",
        credential_state: "authorization_required",
        applied: false,
        requires_restart: false,
      }],
      reasons: [],
    });
    renderWithProviders(<ConnectionFlow kind="oauth" title="OAuth" description="Connection" serviceLabel="OAuth"><div>Provider settings</div></ConnectionFlow>);
    expect(await screen.findByText("Authorization required")).toBeVisible();
    fireEvent.click(screen.getByText("oauth-provider"));
    expect(screen.queryByDisplayValue(/token|secret/i)).not.toBeInTheDocument();
  });
});
