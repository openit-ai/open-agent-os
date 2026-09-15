import { screen } from "@testing-library/react";
import { ConnectionStatusCard } from "./connection-status-card";
import type { ReadinessConnection } from "@/lib/admin-api/types";
import { renderWithProviders } from "@/test/render";

describe("ConnectionStatusCard", () => {
  it("shows credential presence without rendering the secret value", () => {
    const connection = {
      id: "runtime",
      title_key: "admin.setup.steps.runtime.title",
      description_key: "admin.setup.steps.runtime.description",
      status: "healthy",
      required: true,
      configured: true,
      applied: true,
      secret_configured: true,
      source: "env",
      checked_at: "2026-09-15T00:00:00Z",
      latency_ms: 18,
      code: "OK",
      message_key: "admin.common.connectionTest.success",
      secret_value: "must-never-reach-the-dom",
    } satisfies ReadinessConnection & { secret_value: string };

    renderWithProviders(<ConnectionStatusCard connection={connection} />);
    expect(screen.getByText("Configured")).toBeVisible();
    expect(screen.getByText("18 ms")).toBeVisible();
    expect(screen.queryByText("must-never-reach-the-dom")).not.toBeInTheDocument();
  });
});
