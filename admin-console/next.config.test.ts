import { createRequire } from "node:module";
import path from "node:path";

interface RedirectRule {
  source: string;
  destination: string;
  permanent: boolean;
}

const expectedRedirects: RedirectRule[] = [
  { source: "/runtime-config", destination: "/control/runtime", permanent: false },
  { source: "/fallback", destination: "/connections/harness/llm-runtime", permanent: false },
  { source: "/llm-usage", destination: "/operations/usage", permanent: false },
  { source: "/quota", destination: "/operations/usage", permanent: false },
  { source: "/policy", destination: "/control/policy", permanent: false },
  { source: "/approvals", destination: "/control/approvals", permanent: false },
  { source: "/audit", destination: "/control/audit", permanent: false },
  { source: "/embedding", destination: "/connections/knowledge/embedding", permanent: false },
  { source: "/knowledge-ops", destination: "/connections/knowledge/operations", permanent: false },
  { source: "/users", destination: "/management/users", permanent: false },
  { source: "/credentials", destination: "/management/security-keys", permanent: false },
  { source: "/secrets", destination: "/management/security-keys", permanent: false },
  { source: "/feature-flags", destination: "/management/feature-flags", permanent: false },
  { source: "/profile-ops", destination: "/management/profile-operations", permanent: false },
  { source: "/backup", destination: "/management/backup", permanent: false },
  { source: "/security-updates", destination: "/management/updates", permanent: false },
  { source: "/license", destination: "/management/license", permanent: false },
  { source: "/connections/notion", destination: "/connections/knowledge/notion", permanent: false },
  { source: "/knowledge/outline", destination: "/connections/knowledge/outline", permanent: false },
  { source: "/knowledge/embedding", destination: "/connections/knowledge/embedding", permanent: false },
  { source: "/knowledge/operations", destination: "/connections/knowledge/operations", permanent: false },
  { source: "/execution/providers", destination: "/connections/harness/llm-runtime", permanent: false },
  { source: "/execution/fallback", destination: "/connections/harness/llm-runtime", permanent: false },
  { source: "/execution/mcp", destination: "/control/mcp", permanent: false },
  { source: "/execution/usage", destination: "/operations/usage", permanent: false },
  { source: "/execution/quota", destination: "/operations/usage", permanent: false },
  { source: "/operations/services", destination: "/control/services", permanent: false },
  { source: "/management/credentials", destination: "/management/security-keys", permanent: false },
  { source: "/management/secrets", destination: "/management/security-keys", permanent: false },
  { source: "/operations/backup", destination: "/management/backup", permanent: false },
  { source: "/operations/security-updates", destination: "/management/updates", permanent: false },
  { source: "/operations/license", destination: "/management/license", permanent: false },
];

describe("legacy URL redirects", () => {
  it("uses temporary, flattened server redirects for every simple legacy path", async () => {
    const require = createRequire(path.join(process.cwd(), "next.config.test.ts"));
    const config = require("./next.config.js") as { redirects: () => Promise<RedirectRule[]> };
    expect(await config.redirects()).toEqual(expectedRedirects);
    const sources = new Set(expectedRedirects.map((redirect) => redirect.source));
    expect(expectedRedirects.every((redirect) => !sources.has(redirect.destination))).toBe(true);
  });

  it("leaves fragment-dependent compatibility routes to client resolvers", async () => {
    const require = createRequire(path.join(process.cwd(), "next.config.test.ts"));
    const config = require("./next.config.js") as { redirects: () => Promise<RedirectRule[]> };
    const sources = (await config.redirects()).map((redirect) => redirect.source);
    expect(sources).not.toContain("/infra");
    expect(sources).not.toContain("/providers");
  });
});
