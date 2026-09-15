import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderWithProviders } from "@/test/render";
import ProvidersPage from "./providers/page";
import FallbackPage from "./fallback/page";
import PolicyPage from "./policy/page";
import { RuntimeConfigFeature } from "@/components/admin/runtime-config-feature";
import * as api from "@/lib/api";

const navigation = vi.hoisted(() => ({
  push: vi.fn(),
  replace: vi.fn(),
  params: new URLSearchParams(),
}));

vi.mock("next/navigation", () => ({
  usePathname: () => "/control/runtime",
  useRouter: () => ({ push: navigation.push, replace: navigation.replace }),
  useSearchParams: () => navigation.params,
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  const snapshot = {
    tenant_id: "default", version: 1, created_at: "2026-09-15T00:00:00Z", created_by: "admin",
    parent_version: null, config: {}, config_hash: "hash", signature: "signature", published: true,
    published_at: "2026-09-15T00:00:00Z", published_by: "admin",
  };
  const policyVersion = {
    id: "bundle", tenant_id: "default", name: "Default", version: "1.0", rules: [],
    status: "published", created_by: "admin", created_at: "2026-09-15T00:00:00Z",
  };
  return {
    ...actual,
    getToken: () => "test-token",
    getRuntimeMode: vi.fn().mockResolvedValue({ mode: "llm", available_modes: ["hermes", "llm"] }),
    listLLMProviders: vi.fn().mockResolvedValue({ providers: [{
      id: "provider-1", provider: "claude", name: "Primary", model: "model-a", enabled: true,
      api_key: "actual-secret-value", api_key_masked: "***", created_at: "2026-09-15T00:00:00Z", updated_at: "2026-09-15T00:00:00Z",
    }], count: 1 }),
    deleteLLMProvider: vi.fn().mockResolvedValue({ status: "deleted", id: "provider-1" }),
    getFallbackConfig: vi.fn().mockResolvedValue({ enabled: true, chain: [{ provider: "claude", model: "model-a", enabled: true }], updated_at: "2026-09-15T00:00:00Z" }),
    updateFallbackConfig: vi.fn(),
    listRuntimeSnapshots: vi.fn().mockResolvedValue({ tenant_id: "default", count: 1, items: [snapshot] }),
    getRuntimeSnapshot: vi.fn().mockResolvedValue(snapshot),
    getRuntimeStatus: vi.fn().mockResolvedValue({ tenant_id: "default", published_version: 1, has_snapshot: true, config_hash: "hash" }),
    getRuntimeAppliedStatus: vi.fn().mockResolvedValue({ tenant_id: "default", published_version: 1, applied_version: 1 }),
    getRuntimeConfig: vi.fn().mockResolvedValue(snapshot),
    publishRuntimeSnapshot: vi.fn().mockResolvedValue({ tenant_id: "default", published_version: 1, snapshot }),
    rollbackRuntimeSnapshot: vi.fn().mockResolvedValue({ tenant_id: "default", published_version: 2, snapshot }),
    getPolicyBundles: vi.fn().mockResolvedValue({ bundles: [], evaluation_order: [], active_version: "1.0" }),
    getPolicyDraft: vi.fn().mockResolvedValue({ draft: { ...policyVersion, version: "2.0", status: "approved" } }),
    getPolicyHistory: vi.fn().mockResolvedValue({ items: [policyVersion], count: 1, active_version: "1.0" }),
    publishPolicy: vi.fn().mockResolvedValue({ published: policyVersion, active_version: "2.0" }),
    rollbackPolicy: vi.fn().mockResolvedValue({ published: policyVersion, active_version: "2.1" }),
  };
});

describe("Phase 4 destructive action dialogs", () => {
  beforeEach(() => {
    navigation.push.mockReset();
    navigation.replace.mockReset();
    navigation.params = new URLSearchParams();
    vi.mocked(api.deleteLLMProvider).mockClear();
    vi.mocked(api.publishRuntimeSnapshot).mockClear();
    vi.mocked(api.rollbackRuntimeSnapshot).mockClear();
    vi.mocked(api.publishPolicy).mockClear();
    vi.mocked(api.rollbackPolicy).mockClear();
    const snapshot = {
      tenant_id: "default", version: 1, created_at: "2026-09-15T00:00:00Z", created_by: "admin",
      parent_version: null, config: {}, config_hash: "hash", signature: "signature", published: true,
      published_at: "2026-09-15T00:00:00Z", published_by: "admin",
    };
    const policyVersion = {
      id: "bundle", tenant_id: "default", name: "Default", version: "1.0", rules: [],
      status: "published", created_by: "admin", created_at: "2026-09-15T00:00:00Z",
    };
    vi.mocked(api.getRuntimeMode).mockResolvedValue({ mode: "llm", available_modes: ["hermes", "llm"] });
    vi.mocked(api.listLLMProviders).mockResolvedValue({ providers: [{
      id: "provider-1", provider: "claude", name: "Primary", model: "model-a", enabled: true,
      api_key: "actual-secret-value", api_key_masked: "***", created_at: "2026-09-15T00:00:00Z", updated_at: "2026-09-15T00:00:00Z",
    }], count: 1 });
    vi.mocked(api.deleteLLMProvider).mockResolvedValue({ status: "deleted", id: "provider-1" });
    vi.mocked(api.getFallbackConfig).mockResolvedValue({ enabled: true, chain: [{ provider: "claude", model: "model-a", enabled: true }], updated_at: "2026-09-15T00:00:00Z" });
    vi.mocked(api.listRuntimeSnapshots).mockResolvedValue({ tenant_id: "default", count: 1, items: [snapshot] });
    vi.mocked(api.getRuntimeSnapshot).mockResolvedValue(snapshot);
    vi.mocked(api.getRuntimeStatus).mockResolvedValue({ tenant_id: "default", published_version: 1, has_snapshot: true, config_hash: "hash" });
    vi.mocked(api.getRuntimeAppliedStatus).mockResolvedValue({ tenant_id: "default", published_version: 1, applied_version: 1 });
    vi.mocked(api.getRuntimeConfig).mockResolvedValue(snapshot);
    vi.mocked(api.publishRuntimeSnapshot).mockResolvedValue({ tenant_id: "default", published_version: 1, snapshot });
    vi.mocked(api.rollbackRuntimeSnapshot).mockResolvedValue({ tenant_id: "default", published_version: 2, snapshot });
    vi.mocked(api.getPolicyBundles).mockResolvedValue({ bundles: [], evaluation_order: [], active_version: "1.0" });
    vi.mocked(api.getPolicyDraft).mockResolvedValue({ draft: { ...policyVersion, version: "2.0", status: "approved" } });
    vi.mocked(api.getPolicyHistory).mockResolvedValue({ items: [policyVersion], count: 1, active_version: "1.0" });
    vi.mocked(api.publishPolicy).mockResolvedValue({ published: policyVersion, active_version: "2.0" });
    vi.mocked(api.rollbackPolicy).mockResolvedValue({ published: policyVersion, active_version: "2.1" });
    vi.spyOn(window, "confirm");
    vi.spyOn(window, "alert");
  });

  it("cancels and confirms provider deletion without native popups and never renders a secret value", async () => {
    const user = userEvent.setup();
    const { container } = renderWithProviders(<ProvidersPage />);
    await screen.findByRole("table", { name: "LLM Providers" });
    expect(container).not.toHaveTextContent("actual-secret-value");

    await user.click(screen.getByRole("button", { name: "Delete" }));
    let dialog = screen.getByRole("dialog", { name: "Delete this provider?" });
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(api.deleteLLMProvider).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Delete" }));
    dialog = screen.getByRole("dialog", { name: "Delete this provider?" });
    await user.click(within(dialog).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(api.deleteLLMProvider).toHaveBeenCalledWith("provider-1"));
    expect(window.confirm).not.toHaveBeenCalled();
    expect(window.alert).not.toHaveBeenCalled();
  });

  it("routes fallback removal through a cancellable ConfirmDialog", async () => {
    const user = userEvent.setup();
    renderWithProviders(<FallbackPage />);
    await screen.findByRole("table", { name: "Fallback Chain (ordered)" });
    await user.click(screen.getAllByRole("button", { name: "Delete" })[0]);
    const dialog = screen.getByRole("dialog", { name: "Remove this entry?" });
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(screen.getByText("model-a")).toBeVisible();
    expect(window.confirm).not.toHaveBeenCalled();
  });

  it("requires typed confirmation for runtime publish and rollback", async () => {
    const user = userEvent.setup();
    renderWithProviders(<RuntimeConfigFeature />);
    await screen.findByRole("table", { name: "Snapshots (DB list)" });

    await user.click(screen.getAllByRole("button", { name: "Publish" })[0]);
    let dialog = screen.getByRole("dialog", { name: "Publish" });
    const publish = within(dialog).getByRole("button", { name: "Publish" });
    expect(publish).toBeDisabled();
    await user.type(within(dialog).getByLabelText("Type 1 to continue"), "1");
    await user.click(publish);
    await waitFor(() => expect(api.publishRuntimeSnapshot).toHaveBeenCalled());

    await user.click(screen.getAllByRole("button", { name: "Rollback" })[0]);
    dialog = screen.getByRole("dialog", { name: "Rollback" });
    const rollback = within(dialog).getByRole("button", { name: "Rollback" });
    expect(rollback).toBeDisabled();
    await user.type(within(dialog).getByLabelText("Type 1 to continue"), "1");
    await user.click(rollback);
    await waitFor(() => expect(api.rollbackRuntimeSnapshot).toHaveBeenCalled());
    expect(window.confirm).not.toHaveBeenCalled();
  });

  it("requires typed confirmation for policy publish and rollback", async () => {
    const user = userEvent.setup();
    renderWithProviders(<PolicyPage />);
    await screen.findByText(/Active published version/);

    await user.click(screen.getAllByRole("button", { name: /Publish \(L5\)/ })[0]);
    let dialog = screen.getByRole("dialog", { name: "Publish policy" });
    const publish = within(dialog).getByRole("button", { name: "Publish" });
    expect(publish).toBeDisabled();
    await user.type(within(dialog).getByLabelText("Type 2.0 to continue"), "2.0");
    await user.click(publish);
    await waitFor(() => expect(api.publishPolicy).toHaveBeenCalled());

    await user.click(screen.getByRole("button", { name: /History/ }));
    await user.click(await screen.findByRole("button", { name: "Rollback" }));
    dialog = screen.getByRole("dialog", { name: "Rollback policy" });
    const rollback = within(dialog).getByRole("button", { name: "Rollback" });
    await user.type(within(dialog).getByLabelText("Type 1.0 to continue"), "1.0");
    await user.click(rollback);
    await waitFor(() => expect(api.rollbackPolicy).toHaveBeenCalled());
    expect(window.confirm).not.toHaveBeenCalled();
    expect(window.alert).not.toHaveBeenCalled();
  });
});
