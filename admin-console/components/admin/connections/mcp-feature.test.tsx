import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderWithProviders } from "@/test/render";
import { McpPanel } from "./mcp-feature";
import * as api from "@/lib/api";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, listMcpServers: vi.fn(), deleteMcpServer: vi.fn() };
});

describe("MCP destructive actions", () => {
  beforeEach(() => {
    vi.mocked(api.listMcpServers).mockResolvedValue([{ name: "docs", transport: "streamable-http", url: "https://mcp.example.test", args: [], headers_set: [] }]);
    vi.mocked(api.deleteMcpServer).mockResolvedValue({ deleted: "docs", count: 0 });
    vi.spyOn(window, "confirm");
  });

  it("deletes only after ConfirmDialog confirmation", async () => {
    const user = userEvent.setup();
    renderWithProviders(<McpPanel />);
    await user.click(await screen.findByRole("button", { name: "Delete" }));
    const dialog = screen.getByRole("dialog", { name: "Delete?" });
    expect(api.deleteMcpServer).not.toHaveBeenCalled();
    await user.click(within(dialog).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(api.deleteMcpServer).toHaveBeenCalledWith("docs"));
    expect(window.confirm).not.toHaveBeenCalled();
  });
});
