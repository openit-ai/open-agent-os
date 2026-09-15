import { createRequire } from "node:module";
import path from "node:path";

interface RedirectRule {
  source: string;
  destination: string;
  permanent: boolean;
}

const expectedRedirects: RedirectRule[] = [
  { source: "/runtime-config", destination: "/control/runtime", permanent: false },
  { source: "/fallback", destination: "/execution/fallback", permanent: false },
  { source: "/llm-usage", destination: "/execution/usage", permanent: false },
  { source: "/quota", destination: "/execution/quota", permanent: false },
  { source: "/policy", destination: "/control/policy", permanent: false },
  { source: "/approvals", destination: "/control/approvals", permanent: false },
  { source: "/audit", destination: "/control/audit", permanent: false },
  { source: "/embedding", destination: "/knowledge/embedding", permanent: false },
  { source: "/knowledge-ops", destination: "/knowledge/operations", permanent: false },
  { source: "/users", destination: "/management/users", permanent: false },
  { source: "/credentials", destination: "/management/credentials", permanent: false },
  { source: "/secrets", destination: "/management/secrets", permanent: false },
  { source: "/feature-flags", destination: "/management/feature-flags", permanent: false },
  { source: "/profile-ops", destination: "/management/profile-operations", permanent: false },
  { source: "/backup", destination: "/operations/backup", permanent: false },
  { source: "/security-updates", destination: "/operations/security-updates", permanent: false },
  { source: "/license", destination: "/operations/license", permanent: false },
];

describe("legacy URL redirects", () => {
  it("uses temporary server redirects for every simple legacy path", async () => {
    const require = createRequire(path.join(process.cwd(), "next.config.test.ts"));
    const config = require("./next.config.js") as { redirects: () => Promise<RedirectRule[]> };
    expect(await config.redirects()).toEqual(expectedRedirects);
  });

  it("leaves fragment-dependent compatibility routes to client resolvers", async () => {
    const require = createRequire(path.join(process.cwd(), "next.config.test.ts"));
    const config = require("./next.config.js") as { redirects: () => Promise<RedirectRule[]> };
    const sources = (await config.redirects()).map((redirect) => redirect.source);
    expect(sources).not.toContain("/infra");
    expect(sources).not.toContain("/providers");
  });
});
