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

const nextConfig = {
  env: {
    // server/client available to route handler and fallback display at build/runtime
    OAOS_VERSION: installed,
  },
};
module.exports = nextConfig;
