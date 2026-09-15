import * as React from "react";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ConfirmDialog } from "./confirm-dialog";
import { renderWithProviders } from "@/test/render";

describe("ConfirmDialog", () => {
  it("uses danger treatment and blocks confirmation until required text matches", async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();
    renderWithProviders(
      <ConfirmDialog
        open
        title="Delete provider"
        description="This cannot be undone."
        consequence="Requests will stop."
        confirmLabel="Delete"
        tone="danger"
        requireText="provider-a"
        onConfirm={onConfirm}
        onOpenChange={vi.fn()}
      />,
    );

    expect(screen.getByText("Requests will stop.").parentElement).toHaveClass("text-status-danger-text");
    const confirm = screen.getByRole("button", { name: "Delete" });
    expect(confirm).toBeDisabled();
    await user.type(screen.getByLabelText(/provider-a/), "provider-a");
    expect(confirm).toBeEnabled();
    await user.click(confirm);
    expect(onConfirm).toHaveBeenCalledOnce();
  });
});
