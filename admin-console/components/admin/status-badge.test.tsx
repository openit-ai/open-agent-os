import { screen } from "@testing-library/react";
import { StatusBadge } from "./status-badge";
import { renderWithProviders } from "@/test/render";

describe("StatusBadge", () => {
  it("always renders a text label with the status icon", () => {
    renderWithProviders(<StatusBadge status="healthy" />);
    expect(screen.getByText("Healthy")).toBeVisible();
    expect(screen.getByText("Healthy").parentElement?.querySelector("svg")).toBeInTheDocument();
  });
});
