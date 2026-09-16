import { resolveProvidersDestination } from "./providers-compat";

describe("providers compatibility resolver", () => {
  it("routes the ACP fragment to its canonical screen and preserves query values", () => {
    expect(resolveProvidersDestination("?tenant=acme&view=detail", "#ACP")).toBe("/control/acp?tenant=acme&view=detail");
  });

  it("routes the legacy page to canonical providers with its query intact", () => {
    expect(resolveProvidersDestination("?tenant=acme&sort=name", "")).toBe("/connections/harness/llm-runtime?tenant=acme&sort=name");
  });

  it("preserves unknown fragments on the canonical providers screen", () => {
    expect(resolveProvidersDestination("", "#custom-section")).toBe("/connections/harness/llm-runtime#custom-section");
  });
});
