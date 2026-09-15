import { fireEvent, screen } from "@testing-library/react";
import { TestConnectionButton } from "./test-connection-button";
import { testConnection } from "@/lib/admin-api/client";
import type { TestConnectionResult } from "@/lib/admin-api/types";
import { renderWithProviders } from "@/test/render";

vi.mock("@/lib/admin-api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/admin-api/client")>();
  return { ...actual, testConnection: vi.fn() };
});

describe("TestConnectionButton", () => {
  it("blocks duplicate clicks immediately and renders an icon with the result text", async () => {
    let resolve!: (result: TestConnectionResult) => void;
    vi.mocked(testConnection).mockReturnValue(new Promise((done) => { resolve = done; }));
    renderWithProviders(<TestConnectionButton connectionId="provider-a" />);

    const button = screen.getByRole("button", { name: "Test connection" });
    fireEvent.click(button);
    fireEvent.click(button);
    expect(testConnection).toHaveBeenCalledOnce();
    expect(screen.getByRole("button", { name: "Checking connection" })).toBeDisabled();

    resolve({
      ok: true,
      status: "healthy",
      code: "OK",
      summary: "Connection is healthy",
      message_key: "admin.connection.test.ok",
      checked_at: "2026-09-15T00:00:00Z",
      applied: true,
      requires_restart: false,
      correlation_id: "corr-safe",
    });

    expect(await screen.findByText("Connection is healthy")).toBeVisible();
    expect(screen.getByText("Connection is healthy").parentElement?.querySelector("svg")).toBeInTheDocument();
  });
});
