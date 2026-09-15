import { readFileSync } from "node:fs";

function source(relativePath: string) {
  return readFileSync(new URL(relativePath, import.meta.url), "utf8");
}

describe("canonical route ownership", () => {
  it("keeps ACP out of providers and owns it under control/acp", () => {
    const providers = source("../app/(dashboard)/providers/page.tsx");
    const acp = source("../app/(dashboard)/control/acp/page.tsx");
    expect(providers).not.toContain("<AcpSection");
    expect(providers).toContain('href="/control/acp"');
    expect(acp).toContain("@/components/admin/connections/acp-feature");
  });

  it("keeps MCP out of infra and owns it under execution/mcp", () => {
    const infra = source("../app/(dashboard)/infra/page.tsx");
    const mcp = source("../app/(dashboard)/execution/mcp/page.tsx");
    expect(infra).not.toContain("McpPanel");
    expect(infra).not.toContain("TabsTrigger");
    expect(mcp).toContain("@/components/admin/connections/mcp-feature");
  });

  it("makes control/runtime the feature owner and runtime-config a compatibility redirect", () => {
    expect(source("../app/(dashboard)/control/runtime/page.tsx")).toContain("RuntimeConfigFeature");
    expect(source("../app/(dashboard)/runtime-config/page.tsx")).toContain('router.replace(`/control/runtime');
  });

  it("keeps setup canonical instead of redirecting it to infra", () => {
    const setup = source("../app/(dashboard)/setup/page.tsx");
    expect(setup).not.toContain('replace("/infra")');
    expect(setup).toContain("StepWizard");
  });
});
