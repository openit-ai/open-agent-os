import { screen } from "@testing-library/react";
import { Input } from "@/components/ui/input";
import { FormField } from "./form-field";
import { renderWithProviders } from "@/test/render";

describe("FormField", () => {
  it("connects label, hint, and error accessibility attributes", () => {
    renderWithProviders(<FormField id="endpoint" label="Endpoint" hint="HTTPS only" error="Invalid endpoint" required><Input /></FormField>);
    const input = screen.getByLabelText(/Endpoint/);
    expect(input).toHaveAttribute("id", "endpoint");
    expect(input).toHaveAttribute("aria-invalid", "true");
    expect(input).toHaveAccessibleDescription("HTTPS only Invalid endpoint");
    expect(input).toBeRequired();
  });
});
