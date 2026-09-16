import { screen } from "@testing-library/react";
import { renderWithProviders } from "@/test/render";
import * as api from "@/lib/api";
import {
  OperationsHealthFeature,
  serviceDisplayLabel,
  type InfrastructureRow,
} from "./infrastructure-features";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, apiFetch: vi.fn() };
});

describe("serviceDisplayLabel", () => {
  it("returns one token when display_name and service match", () => {
    const row = { display_name: "outline", service: "outline" };
    expect(serviceDisplayLabel(row)).toBe("outline");
    expect(serviceDisplayLabel(row)).not.toBe("outlineoutline");
  });

  it("prefers display_name over service and name", () => {
    expect(serviceDisplayLabel({ display_name: "Outline", service: "outline", name: "notes" })).toBe("Outline");
  });

  it("falls back from service to name", () => {
    expect(serviceDisplayLabel({ service: "outline", name: "notes" })).toBe("outline");
    expect(serviceDisplayLabel({ name: "notes" })).toBe("notes");
  });
});

describe("OperationsHealthFeature", () => {
  it("renders a duplicate-valued service label exactly once", async () => {
    const outline: InfrastructureRow = {
      id: "outline",
      display_name: "outline",
      service: "outline",
      name: "outline",
      host: "note.oaos.cloud",
      port: 443,
      status: "healthy",
    };
    vi.mocked(api.apiFetch).mockResolvedValue({ items: [outline] });

    const view = renderWithProviders(<OperationsHealthFeature />);

    await screen.findByText("outline", { exact: true });
    expect(screen.getAllByText("outline", { exact: true })).toHaveLength(1);
    expect(view.container).not.toHaveTextContent("outlineoutline");
  });
});
