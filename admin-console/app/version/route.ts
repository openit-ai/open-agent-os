import { NextResponse } from "next/server";
import fs from "fs";
import path from "path";

export const dynamic = "force-static";
export const revalidate = 3600; // cache latest check 1h

// Authoritative installed version: OAOS_VERSION env > admin-console/package.json (0.1.3) > fallback
// v1.7.2 is architecture doc version, not product version.
function getInstalledVersion(): string {
  // 1) env (set at deploy / systemd / docker)
  const env = process.env.OAOS_VERSION || process.env.NEXT_PUBLIC_OAOS_VERSION;
  if (env && /^\d+\.\d+/.test(env.trim())) return env.trim().replace(/^v/, "");
  // 2) package.json in admin-console — single source of truth for product version (현재 0.1.3)
  try {
    const pkgPath = path.join(process.cwd(), "package.json");
    const raw = fs.readFileSync(pkgPath, "utf-8");
    const pkg = JSON.parse(raw) as { version?: string };
    if (pkg.version && /^\d+\.\d+/.test(pkg.version.trim())) return pkg.version.trim().replace(/^v/, "");
  } catch { /* ignore */ }
  // 3) fallback — product initial version
  return "0.1.3";
}

function normalizeTag(tag: string): string | null {
  const t = tag.trim().replace(/^v/i, "");
  // accept semver like 0.1.1, 0.1.3 etc. ignore non-semver
  if (!/^\d+\.\d+\.\d+/.test(t)) return null;
  const m = t.match(/^(\d+\.\d+\.\d+[^ ]*)/);
  return m ? m[1] : null;
}

function compareSemver(a: string, b: string): number {
  const pa = a.split(".").map((x) => parseInt(x, 10) || 0);
  const pb = b.split(".").map((x) => parseInt(x, 10) || 0);
  for (let i = 0; i < 3; i++) {
    if ((pa[i] ?? 0) > (pb[i] ?? 0)) return 1;
    if ((pa[i] ?? 0) < (pb[i] ?? 0)) return -1;
  }
  return 0;
}

const GITHUB_REPO = process.env.GITHUB_REPO || "openit-ai/open-agent-os";
const GITHUB_API_BASE = "https://api.github.com";

async function fetchLatestGithubVersion(): Promise<string | null> {
  // Bounded, non-blocking: 3s timeout, no secrets forwarded, official repo semver only
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 3000);
  try {
    // 1) try releases/latest (official)
    try {
      const res = await fetch(`${GITHUB_API_BASE}/repos/${GITHUB_REPO}/releases/latest`, {
        headers: {
          Accept: "application/vnd.github.v3+json",
          "User-Agent": "open-agent-os-admin-console/version-check",
        },
        signal: controller.signal,
        next: { revalidate: 3600 },
      });
      if (res.ok) {
        const data = (await res.json()) as { tag_name?: string; name?: string };
        const raw = data.tag_name || data.name || "";
        const norm = normalizeTag(raw);
        if (norm) return norm;
      }
    } catch { /* try tags fallback */ }

    // 2) fallback: tags list (public, no auth) — only semver tags considered
    try {
      const res2 = await fetch(`${GITHUB_API_BASE}/repos/${GITHUB_REPO}/tags?per_page=20`, {
        headers: {
          Accept: "application/vnd.github.v3+json",
          "User-Agent": "open-agent-os-admin-console/version-check",
        },
        signal: controller.signal,
        next: { revalidate: 3600 },
      });
      if (res2.ok) {
        const tags = (await res2.json()) as Array<{ name: string }>;
        const versions: string[] = [];
        for (const t of tags) {
          const n = normalizeTag(t.name);
          if (n) versions.push(n);
        }
        if (versions.length) {
          versions.sort((a, b) => compareSemver(b, a));
          return versions[0];
        }
      }
    } catch { /* ignore, will return null */ }

    return null;
  } finally {
    clearTimeout(timeout);
  }
}

export async function GET() {
  const installedVersion = getInstalledVersion();
  let latestVersion: string | null = null;
  try {
    latestVersion = await fetchLatestGithubVersion();
  } catch {
    latestVersion = null;
  }

  let updateAvailable = false;
  if (latestVersion && installedVersion) {
    const normInstalled = normalizeTag(installedVersion) ?? installedVersion.replace(/^v/, "");
    const cmp = compareSemver(latestVersion, normInstalled);
    // only when latest is actually higher; scheduled 0.1.3 is NOT considered unless released
    updateAvailable = cmp > 0;
  }

  // When latest is not confirmed (null) or not higher, latestVersion stays null or equal — consumer shows only installed
  const body: Record<string, unknown> = {
    installedVersion,
    latestVersion,
    updateAvailable,
    repo: GITHUB_REPO,
  };

  return NextResponse.json(body, {
    headers: {
      "Cache-Control": "public, s-maxage=3600, stale-while-revalidate=600",
    },
  });
}
