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

    const status = await screen.findByRole("status");
    expect(status).toHaveTextContent("OK · Connection is healthy");
    expect(status.querySelector("svg")).toBeInTheDocument();
  });

  it("does not render an unapplied successful probe as healthy", async () => {
    vi.mocked(testConnection).mockResolvedValue({
      ok: true,
      status: "healthy",
      code: "OK",
      summary: "Draft target responded",
      message_key: "missing.translation.key",
      checked_at: "2026-09-15T00:00:00Z",
      applied: false,
      requires_restart: true,
      correlation_id: "corr-unapplied",
    });
    renderWithProviders(<TestConnectionButton connectionId="mattermost" />);
    fireEvent.click(screen.getByRole("button", { name: "Test connection" }));
    const status = await screen.findByRole("status");
    expect(status).toHaveClass("text-status-warn-text");
    expect(screen.getByText("Saved, but not yet applied")).toBeVisible();
  });

  it.each([
    ["AUTH_REQUIRED", "Authorization is required"],
    ["UNREACHABLE", "The target cannot be reached"],
    ["TIMEOUT", "The connection check timed out"],
    ["NOT_APPLIED", "The saved revision is not the effective revision"],
    ["PERMISSION_DENIED", "The provider denied access"],
    ["MISCONFIGURED", "The connection is not configured correctly"],
  ] as const)("renders %s with its translated next action", async (code, message) => {
    vi.mocked(testConnection).mockResolvedValue({
      ok: false,
      status: code === "NOT_APPLIED" ? "warning" : "failed",
      code,
      summary: code,
      message_key: `admin.connections.test.${code === "AUTH_REQUIRED" ? "authRequired" : code === "PERMISSION_DENIED" ? "permissionDenied" : code === "NOT_APPLIED" ? "notApplied" : code.toLowerCase()}`,
      checked_at: "2026-09-15T00:00:00Z",
      applied: code !== "NOT_APPLIED",
      requires_restart: code === "NOT_APPLIED",
      next_action: { label_key: "admin.connections.overview.open", href: "/operations/health" },
      correlation_id: `corr-${code}`,
    });
    renderWithProviders(<TestConnectionButton connectionId="mattermost" />);
    fireEvent.click(screen.getByRole("button", { name: "Test connection" }));
    const status = await screen.findByRole("status");
    expect(status).toHaveTextContent(code);
    expect(status).toHaveTextContent(message);
    expect(screen.getByRole("link", { name: "Open connection" })).toHaveAttribute("href", "/operations/health");
  });
});
