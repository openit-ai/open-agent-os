import { readFileSync } from "node:fs";

function source(relativePath: string) {
  return readFileSync(new URL(relativePath, import.meta.url), "utf8");
}

const routes = [
  ["users", "management/users", "UsersFeature", true],
  ["credentials", "management/credentials", "CredentialsFeature", true],
  ["secrets", "management/secrets", "SecretsFeature", true],
  ["feature-flags", "management/feature-flags", "FeatureFlagsFeature", true],
  ["profile-ops", "management/profile-operations", "ProfileOperationsFeature", false],
  ["backup", "operations/backup", "BackupFeature", true],
  ["security-updates", "operations/security-updates", "SecurityUpdatesFeature", true],
  ["license", "operations/license", "LicenseFeature", false],
  ["knowledge-ops", "knowledge/operations", "KnowledgeOperationsFeature", true],
  ["embedding", "knowledge/embedding", "EmbeddingFeature", false],
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
