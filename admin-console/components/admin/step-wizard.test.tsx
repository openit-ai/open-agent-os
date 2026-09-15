import { fireEvent, screen } from "@testing-library/react";
import { StepWizard } from "./step-wizard";
import { renderWithProviders } from "@/test/render";

const steps = [
  { id: "environment", label: "Environment check", required: true, status: "pending" as const },
  { id: "runtime", label: "Execution path", required: true, status: "complete" as const },
];

describe("StepWizard", () => {
  it("moves between steps and disables next while a required check is incomplete", () => {
    const onStepChange = vi.fn();
    const onNext = vi.fn();
    renderWithProviders(
      <StepWizard
        steps={steps}
        currentStep="environment"
        progress={{ requiredDone: 1, requiredTotal: 2, optionalDone: 0, optionalTotal: 0 }}
        onStepChange={onStepChange}
        onNext={onNext}
        onBack={vi.fn()}
        canContinue={false}
      >
        <h2 id="setup-current-step">Environment</h2>
      </StepWizard>,
    );

    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: /2\. Execution path/ }));
    expect(onStepChange).toHaveBeenCalledWith("runtime");
    expect(onNext).not.toHaveBeenCalled();
  });
});
