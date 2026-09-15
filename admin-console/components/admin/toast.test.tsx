import * as React from "react";
import { act, fireEvent, screen } from "@testing-library/react";
import { useToast } from "./toast";
import { renderWithProviders } from "@/test/render";

function ToastHarness() {
  const { toast } = useToast();
  return <button onClick={() => toast({ title: "Saved", variant: "success", durationMs: 1000 })}>Notify</button>;
}

describe("Toast", () => {
  afterEach(() => vi.useRealTimers());

  it("renders in a polite live region, merges duplicates, and auto-dismisses", () => {
    vi.useFakeTimers();
    renderWithProviders(<ToastHarness />);
    fireEvent.click(screen.getByRole("button", { name: "Notify" }));
    fireEvent.click(screen.getByRole("button", { name: "Notify" }));
    expect(screen.getByRole("status")).toHaveAttribute("aria-live", "polite");
    expect(screen.getByText("Saved")).toBeVisible();
    expect(screen.getByText("2 times")).toBeVisible();
    act(() => vi.advanceTimersByTime(1000));
    expect(screen.queryByText("Saved")).not.toBeInTheDocument();
  });

  it("uses an assertive live region for errors", async () => {
    function ErrorHarness() {
      const { toast } = useToast();
      React.useEffect(() => { toast({ title: "Failed", variant: "error" }); }, [toast]);
      return null;
    }
    renderWithProviders(<ErrorHarness />);
    expect(await screen.findByRole("alert")).toHaveAttribute("aria-live", "assertive");
  });
});
