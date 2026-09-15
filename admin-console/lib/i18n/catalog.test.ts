/// <reference types="vite/client" />
import en from "./en.json";
import ko from "./ko.json";

function leafKeys(value: unknown, prefix = ""): string[] {
  if (value == null || typeof value !== "object" || Array.isArray(value)) return [prefix];
  return Object.entries(value).flatMap(([key, child]) => leafKeys(child, prefix ? `${prefix}.${key}` : key));
}

// Raw source of every screen and component, so a key that is rendered but never
// added to a catalog is caught at build time rather than showing a raw key.
const sources = import.meta.glob("/{app,components,lib}/**/*.{ts,tsx}", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

const sourceText = Object.entries(sources)
  .filter(([path]) => !/\.test\.tsx?$/.test(path) && !path.includes("/lib/i18n/"))
  .map(([, text]) => text)
  .join("\n");

function literalKeys(text: string): string[] {
  const found = new Set<string>();
  for (const match of text.matchAll(/\bt\(\s*"([^"$]+)"/g)) found.add(match[1]);
  for (const match of text.matchAll(/\bt\(\s*'([^'$]+)'/g)) found.add(match[1]);
  return [...found];
}

function dynamicPrefixes(text: string): string[] {
  const found = new Set<string>();
  for (const match of text.matchAll(/\bt\(\s*`([^`$]*)\$\{/g)) {
    const head = match[1];
    if (head.endsWith(".")) found.add(head);
  }
  return [...found];
}

describe("i18n catalogs", () => {
  const koKeys = leafKeys(ko).sort();

  it("keeps the English and Korean key sets identical", () => {
    expect(leafKeys(en).sort()).toEqual(koKeys);
  });

  it("defines every key a screen looks up as a literal", () => {
    const missing = literalKeys(sourceText).filter((key) => !koKeys.includes(key));
    expect(missing).toEqual([]);
  });

  it("resolves every dynamic key prefix to at least one entry", () => {
    const unresolved = dynamicPrefixes(sourceText).filter(
      (prefix) => !koKeys.some((key) => key.startsWith(prefix)),
    );
    expect(unresolved).toEqual([]);
  });
});
