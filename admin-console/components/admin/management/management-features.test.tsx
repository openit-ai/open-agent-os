import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderWithProviders } from "@/test/render";
import { CredentialsFeature, SecretsFeature, UsersFeature } from "./management-features";
import * as api from "@/lib/api";

const navigation = vi.hoisted(() => ({ push: vi.fn(), replace: vi.fn(), params: new URLSearchParams() }));

vi.mock("next/navigation", () => ({
  usePathname: () => "/management/users",
  useRouter: () => ({ push: navigation.push, replace: navigation.replace }),
  useSearchParams: () => navigation.params,
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    getToken: vi.fn(() => "test-token"),
    getMe: vi.fn().mockResolvedValue({ id: "me", email: "me@example.com", display_name: "Me", role: "L5", created_at: "2026-09-15T00:00:00Z" }),
    listUsers: vi.fn().mockResolvedValue([
      { id: "me", email: "me@example.com", display_name: "Me", role: "L5", created_at: "2026-09-15T00:00:00Z" },
      { id: "other", email: "other@example.com", display_name: "Other", role: "L4", created_at: "2026-09-14T00:00:00Z" },
    ]),
    listMappings: vi.fn().mockResolvedValue([]),
    deleteUser: vi.fn().mockResolvedValue({ status: "deleted", id: "other" }),
    getCredentialsStatus: vi.fn().mockResolvedValue({
      providers: [{ provider: "notion", total: 1, active: 1, revoked: 0, expired: 0 }],
      total: 1, active: 1, revoked: 0, expired: 0,
      recent: [{ id: "cred-1", user_id: "user-1", agent_id: "agent-1", provider: "notion", scope: "read", status: "active", created_at: "2026-09-15T00:00:00Z", credential_value: "credential-secret" }],
    }),
    getSecretsStatus: vi.fn().mockResolvedValue({
      checked_at: "2026-09-15T00:00:00Z", count: 1, rotation_needed_count: 0,
      items: [{ name: "NOTION_TOKEN", configured: true, length: 32, source_env: "NOTION_TOKEN", rotation_needed: false, reason: "healthy", value: "raw-secret-value" }],
    }),
    getRotationGuide: vi.fn().mockResolvedValue({ overview: "Rotate outside the console", steps: [], checklist: [], executes_rotation: false }),
  };
});

describe("Phase 5 management features", () => {
  beforeEach(() => {
    navigation.push.mockReset();
    navigation.replace.mockReset();
    navigation.params = new URLSearchParams();
    vi.mocked(api.getToken).mockReturnValue("test-token");
    vi.mocked(api.getMe).mockResolvedValue({ id: "me", email: "me@example.com", display_name: "Me", role: "L5", created_at: "2026-09-15T00:00:00Z" });
    vi.mocked(api.listUsers).mockResolvedValue([
      { id: "me", email: "me@example.com", display_name: "Me", role: "L5", created_at: "2026-09-15T00:00:00Z" },
      { id: "other", email: "other@example.com", display_name: "Other", role: "L4", created_at: "2026-09-14T00:00:00Z" },
    ]);
    vi.mocked(api.listMappings).mockResolvedValue([]);
    vi.mocked(api.deleteUser).mockResolvedValue({ status: "deleted", id: "other" });
    vi.mocked(api.getCredentialsStatus).mockResolvedValue({ providers: [{ provider: "notion", total: 1, active: 1, revoked: 0, expired: 0 }], total: 1, active: 1, revoked: 0, expired: 0, recent: [{ id: "cred-1", user_id: "user-1", agent_id: "agent-1", provider: "notion", scope: "read", status: "active", created_at: "2026-09-15T00:00:00Z" }] });
    vi.mocked(api.getSecretsStatus).mockResolvedValue({ checked_at: "2026-09-15T00:00:00Z", count: 1, rotation_needed_count: 0, items: [{ name: "NOTION_TOKEN", configured: true, length: 32, source_env: "NOTION_TOKEN", rotation_needed: false, reason: "healthy", value: "raw-secret-value" } as api.SecretStatusItem] });
    vi.mocked(api.getRotationGuide).mockResolvedValue({ overview: "Rotate outside the console", steps: [], checklist: [], executes_rotation: false });
    vi.mocked(api.deleteUser).mockClear();
    vi.spyOn(window, "confirm");
    vi.spyOn(window, "alert");
  });

  it("uses a cancellable ConfirmDialog for account deletion", async () => {
    const user = userEvent.setup();
    renderWithProviders(<UsersFeature />);
    await screen.findByRole("table", { name: "User List" });
    await user.click(screen.getAllByRole("button", { name: "Delete" }).find((button) => !button.hasAttribute("disabled"))!);
    const dialog = screen.getByRole("dialog", { name: "Delete this user?" });
    await user.click(within(dialog).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(api.deleteUser).toHaveBeenCalledWith("other"));
    expect(window.confirm).not.toHaveBeenCalled();
    expect(window.alert).not.toHaveBeenCalled();
  });

  it("never renders credential or secret values and omits secret length metadata", async () => {
    const credentials = renderWithProviders(<CredentialsFeature />);
    await screen.findByRole("table", { name: "Provider Status" });
    expect(credentials.container).not.toHaveTextContent("credential-secret");
    credentials.unmount();

    const secrets = renderWithProviders(<SecretsFeature />);
    await screen.findByRole("table", { name: "Secrets" });
    expect(secrets.container).not.toHaveTextContent("raw-secret-value");
    expect(secrets.container).not.toHaveTextContent("32");
    expect(secrets.container).not.toHaveTextContent("NOTION_TOKENNOTION_TOKEN");
  });
});
