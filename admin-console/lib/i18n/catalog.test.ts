import en from "./en.json";
import ko from "./ko.json";

function leafKeys(value: unknown, prefix = ""): string[] {
  if (value == null || typeof value !== "object" || Array.isArray(value)) return [prefix];
  return Object.entries(value).flatMap(([key, child]) => leafKeys(child, prefix ? `${prefix}.${key}` : key));
}

describe("i18n catalogs", () => {
  it("keeps the English and Korean key sets identical", () => {
    expect(leafKeys(en).sort()).toEqual(leafKeys(ko).sort());
  });
});
