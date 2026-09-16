import { readFileSync } from "node:fs";

function source(relativePath: string) {
  return readFileSync(new URL(relativePath, import.meta.url), "utf8");
}

describe("canonical route ownership", () => {
  it("keeps ACP out of providers and owns it under control/acp and Hermes Agent", () => {
    const providers = source("../components/admin/harness/providers-feature.tsx");
    const acp = source("../app/(dashboard)/control/acp/page.tsx");
    const hermesAgent = source("../components/admin/harness/hermes-agent-feature.tsx");
    expect(providers).not.toContain("<AcpSection");
    expect(providers).not.toContain("admin.providers.acpSummary");
    expect(acp).toContain("@/components/admin/connections/acp-feature");
    expect(hermesAgent).toContain('href="/control/acp"');
    expect(hermesAgent).toContain("getRuntimeMode");
    expect(hermesAgent).toContain("getAcpConfig");
  });

  it("keeps MCP out of infra and owns it under control/mcp", () => {
    const infra = source("../app/(dashboard)/infra/page.tsx");
    const mcp = source("../app/(dashboard)/control/mcp/page.tsx");
    expect(infra).not.toContain("McpPanel");
    expect(infra).not.toContain("TabsTrigger");
    expect(mcp).toContain("@/components/admin/connections/mcp-feature");
  });

  it("combines providers and fallback under the Harness LLM Runtime screen", () => {
    const feature = source("../components/admin/harness/llm-runtime-feature.tsx");
    const providers = source("../components/admin/harness/providers-feature.tsx");
    const fallback = source("../components/admin/harness/fallback-feature.tsx");
    expect(feature).toContain("ProvidersFeature");
    expect(feature).toContain("FallbackFeature");
    expect(source("../app/(dashboard)/connections/harness/llm-runtime/page.tsx")).toContain("LLMRuntimeFeature");
    expect(providers).toContain("runtimeBannerHermes");
    expect(providers).toContain("isHermes ? (");
    expect(fallback).toContain('runtimeMode === "hermes"');
  });

  it("combines credentials and secret metadata under security keys", () => {
    const feature = source("../components/admin/management/security-keys-feature.tsx");
    expect(feature).toContain("CredentialsFeature");
    expect(feature).toContain("SecretsFeature");
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
