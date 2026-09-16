/** @type {import('next').NextConfig} */
const fs = require("fs");
const path = require("path");

let pkgVersion = "0.1.3";
try {
  const pkg = JSON.parse(fs.readFileSync(path.join(__dirname, "package.json"), "utf-8"));
  if (pkg.version) pkgVersion = String(pkg.version).trim();
} catch { /* ignore */ }

// Allow deploy-time override; if OAOS_VERSION is set at build, it takes precedence.
const installed = (process.env.OAOS_VERSION || process.env.NEXT_PUBLIC_OAOS_VERSION || pkgVersion).trim();

const legacyRedirects = [
  ["/runtime-config", "/control/runtime"],
  ["/fallback", "/connections/harness/llm-runtime"],
  ["/llm-usage", "/operations/usage"],
  ["/quota", "/operations/usage"],
  ["/policy", "/control/policy"],
  ["/approvals", "/control/approvals"],
  ["/audit", "/control/audit"],
  ["/embedding", "/connections/knowledge/embedding"],
  ["/knowledge-ops", "/connections/knowledge/operations"],
  ["/users", "/management/users"],
  ["/credentials", "/management/security-keys"],
  ["/secrets", "/management/security-keys"],
  ["/feature-flags", "/management/feature-flags"],
  ["/profile-ops", "/management/profile-operations"],
  ["/backup", "/management/backup"],
  ["/security-updates", "/management/updates"],
  ["/license", "/management/license"],
  ["/connections/notion", "/connections/knowledge/notion"],
  ["/knowledge/outline", "/connections/knowledge/outline"],
  ["/knowledge/embedding", "/connections/knowledge/embedding"],
  ["/knowledge/operations", "/connections/knowledge/operations"],
  ["/execution/providers", "/connections/harness/llm-runtime"],
  ["/execution/fallback", "/connections/harness/llm-runtime"],
  ["/execution/mcp", "/control/mcp"],
  ["/execution/usage", "/operations/usage"],
  ["/execution/quota", "/operations/usage"],
  ["/operations/services", "/control/services"],
  ["/management/credentials", "/management/security-keys"],
  ["/management/secrets", "/management/security-keys"],
  ["/operations/backup", "/management/backup"],
  ["/operations/security-updates", "/management/updates"],
  ["/operations/license", "/management/license"],
];

const nextConfig = {
  env: {
    // server/client available to route handler and fallback display at build/runtime
    OAOS_VERSION: installed,
  },
  async redirects() {
    return legacyRedirects.map(([source, destination]) => ({
      source,
      destination,
      permanent: false,
    }));
  },
};
module.exports = nextConfig;
