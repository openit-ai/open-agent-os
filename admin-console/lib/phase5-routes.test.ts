import { readFileSync } from "node:fs";

function source(relativePath: string) {
  return readFileSync(new URL(relativePath, import.meta.url), "utf8");
}

const routes = [
  ["users", "management/users", "UsersFeature", true],
  ["credentials", "management/security-keys", "SecurityKeysFeature", false],
  ["secrets", "management/security-keys", "SecurityKeysFeature", false],
  ["feature-flags", "management/feature-flags", "FeatureFlagsFeature", true],
  ["profile-ops", "management/profile-operations", "ProfileOperationsFeature", false],
  ["backup", "management/backup", "BackupFeature", true],
  ["security-updates", "management/updates", "SecurityUpdatesFeature", true],
  ["license", "management/license", "LicenseFeature", false],
  ["knowledge-ops", "connections/knowledge/operations", "KnowledgeOperationsFeature", true],
  ["embedding", "connections/knowledge/embedding", "EmbeddingFeature", false],
] as const;

describe("Phase 5 canonical and compatibility routes", () => {
  it.each(routes)("preserves /%s and routes it to /%s with its query", (legacy, canonical, feature, needsSuspense) => {
    const compatibilityPage = source(`../app/(dashboard)/${legacy}/page.tsx`);
    const canonicalPage = source(`../app/(dashboard)/${canonical}/page.tsx`);
    expect(compatibilityPage).toContain(`router.replace(\`/${canonical}\${window.location.search}\`)`);
    expect(canonicalPage).toContain(feature);
    if (needsSuspense) expect(canonicalPage).toContain("<Suspense");
  });
});
