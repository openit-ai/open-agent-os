import { screen } from "@testing-library/react";
import { RequiredConnectionsSummary } from "./required-connections-summary";
import type { AdminReadiness } from "@/lib/admin-api/types";
import { renderWithProviders } from "@/test/render";

const readiness: AdminReadiness = {
  schema_version: "1",
  required_complete: false,
  checked_at: "2026-09-15T00:00:00Z",
  connections: [
    { id: "cp", title_key: "admin.setup.steps.environment.title", description_key: "admin.setup.steps.environment.description", status: "healthy", required: true, configured: true, applied: true, secret_configured: true, source: "manifest", code: "OK", message_key: "admin.common.connectionTest.success" },
    { id: "runtime", title_key: "admin.setup.steps.runtime.title", description_key: "admin.setup.steps.runtime.description", status: "warning", required: true, configured: true, applied: false, secret_configured: true, source: "env", code: "NOT_APPLIED", message_key: "admin.common.error.notApplied", affected_services: ["ACP"], operation_href: "/infra" },
    { id: "ingress", title_key: "admin.setup.steps.ingress.title", description_key: "admin.setup.steps.ingress.description", status: "failed", required: true, configured: false, applied: false, secret_configured: false, source: "discovery", code: "AUTH_REQUIRED", message_key: "admin.common.error.authRequired", next_action: { label_key: "admin.readiness.summary.resolve", href: "/infra" } },
  ],
};

describe("RequiredConnectionsSummary", () => {
  it.each([
    ["healthy", "Healthy"],
    ["warning", "Warning"],
    ["failed", "Failed"],
  ] as const)("renders the %s summary state with icon and text", (status, label) => {
    const scenario: AdminReadiness = {
      ...readiness,
      required_complete: status === "healthy",
      connections: [{ ...readiness.connections[0], status, applied: status === "healthy", code: status === "healthy" ? "OK" : status === "warning" ? "NOT_APPLIED" : "UNREACHABLE" }],
    };
    renderWithProviders(<RequiredConnectionsSummary readiness={scenario} onRecheck={vi.fn()} />);
    expect(screen.getAllByText(label).length).toBeGreaterThan(0);
    expect(screen.getByText(status === "healthy" ? "1 required, 1 healthy, 0 need attention" : "1 required, 0 healthy, 1 need attention")).toBeVisible();
  });

  it("keeps an unapplied saved state in a warning surface", () => {
    renderWithProviders(<RequiredConnectionsSummary readiness={readiness} onRecheck={vi.fn()} />);
    const banner = screen.getByText("Saved, but not yet applied").closest("[role='status']");
    expect(banner).toHaveClass("bg-status-warn-surface");
    expect(banner).not.toHaveClass("bg-status-ok-surface");
    expect(screen.getByText("Affected services: ACP")).toBeVisible();
  });
});
