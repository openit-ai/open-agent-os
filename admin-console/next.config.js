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
  ["/fallback", "/execution/fallback"],
  ["/llm-usage", "/execution/usage"],
  ["/quota", "/execution/quota"],
  ["/policy", "/control/policy"],
  ["/approvals", "/control/approvals"],
  ["/audit", "/control/audit"],
  ["/embedding", "/knowledge/embedding"],
  ["/knowledge-ops", "/knowledge/operations"],
  ["/users", "/management/users"],
  ["/credentials", "/management/credentials"],
  ["/secrets", "/management/secrets"],
  ["/feature-flags", "/management/feature-flags"],
  ["/profile-ops", "/management/profile-operations"],
  ["/backup", "/operations/backup"],
  ["/security-updates", "/operations/security-updates"],
  ["/license", "/operations/license"],
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
