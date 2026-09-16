import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderWithProviders } from "@/test/render";
import { EmbeddingFeature, KnowledgeOperationsFeature } from "./knowledge-features";
import * as api from "@/lib/api";

const navigation = vi.hoisted(() => ({ push: vi.fn(), replace: vi.fn(), params: new URLSearchParams() }));
vi.mock("next/navigation", () => ({ usePathname: () => "/connections/knowledge/operations", useRouter: () => ({ push: navigation.push, replace: navigation.replace }), useSearchParams: () => navigation.params }));
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, getToken: vi.fn(), getKnowledgeOpsStatus: vi.fn(), postKnowledgeSync: vi.fn(), getEmbeddingConfig: vi.fn(), updateEmbeddingConfig: vi.fn() };
});

describe("Phase 5 knowledge features", () => {
  beforeEach(() => {
    navigation.params = new URLSearchParams(); navigation.push.mockReset(); navigation.replace.mockReset();
    vi.mocked(api.getToken).mockReturnValue("test-token");
    vi.mocked(api.getKnowledgeOpsStatus).mockResolvedValue({ tenant_id: "default", checkpoints: [{ tenant_id: "default", source_system: "notion", cursor: "cursor-1", last_sync_at: "2026-09-15T00:00:00Z", updated_at: "2026-09-15T00:00:00Z" }], checkpoint_count: 1, document_count: 10, known_connectors: ["notion", "outline"], synced_connectors: ["notion"], pending_connectors: ["outline"], source: "db" });
    vi.mocked(api.postKnowledgeSync).mockResolvedValue({ dry_run: true, enqueued: false, planned: { connector: "notion" } });
    vi.mocked(api.getEmbeddingConfig).mockResolvedValue({ provider: "ollama", model: "mxbai-embed-large", dim: 1024, api_url: "http://127.0.0.1:11434", source: "db", applied: false, restart_required: true });
    vi.mocked(api.updateEmbeddingConfig).mockResolvedValue({ provider: "openai-compatible", model: "text-embedding-3-small", dim: 1536, api_url: "", source: "db", applied: false, restart_required: true });
  });

  it("renders checkpoint history and distinguishes a dry-run result", async () => {
    const user = userEvent.setup();
    renderWithProviders(<KnowledgeOperationsFeature />);
    await screen.findByRole("table", { name: "Knowledge synchronization history" });
    await user.click(screen.getByRole("button", { name: "Enqueue sync" }));
    await waitFor(() => expect(api.postKnowledgeSync).toHaveBeenCalledWith({ connector: "notion", tenant_id: "default", dry_run: true }));
    expect(await screen.findByText("Sync planned (dry-run, nothing enqueued)")).toBeVisible();
  });

  it("fills provider defaults and validates the embedding form with FormField", async () => {
    const user = userEvent.setup();
    renderWithProviders(<EmbeddingFeature />);
    const provider = await screen.findByLabelText("Provider");
    await user.selectOptions(provider, "openai-compatible");
    expect(screen.getByLabelText(/^Model/)).toHaveValue("text-embedding-3-small");
    expect(screen.getByLabelText(/^Dim/)).toHaveValue("1536");
    await user.clear(screen.getByLabelText(/^Dim/));
    await user.type(screen.getByLabelText(/^Dim/), "0");
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(screen.getByText("Enter a positive whole-number dimension.")).toBeVisible();
    expect(api.updateEmbeddingConfig).not.toHaveBeenCalled();
  });
});
