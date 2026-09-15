import { INFRA_COMPAT_ROUTES, resolveInfraDestination } from "./infra-compat";

describe("infra compatibility resolver", () => {
  it.each(Object.entries(INFRA_COMPAT_ROUTES))("resolves ?tab=%s to %s", (tab, destination) => {
    expect(resolveInfraDestination(`?tab=${tab}`, "")).toBe(destination);
  });

  it.each(Object.entries(INFRA_COMPAT_ROUTES))("resolves #%s to %s", (hash, destination) => {
    expect(resolveInfraDestination("", `#${hash}`)).toBe(destination);
  });

  it("falls back to operations health and preserves non-tab query values", () => {
    expect(resolveInfraDestination("?tab=not-a-tab&tenant=acme", "")).toBe("/operations/health?tenant=acme");
    expect(resolveInfraDestination("?tenant=acme", "#unknown")).toBe("/operations/health?tenant=acme");
  });

  it("gives the query tab precedence over the fragment", () => {
    expect(resolveInfraDestination("?tab=mcp", "#slack")).toBe("/execution/mcp");
  });
});
