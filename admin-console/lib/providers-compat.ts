export function resolveProvidersDestination(search: string, hash: string): string {
  const query = search.startsWith("?") ? search.slice(1) : search;
  const querySuffix = query ? `?${query}` : "";
  const fragment = hash.replace(/^#/, "").trim();

  if (fragment.toLowerCase() === "acp") {
    return `/control/acp${querySuffix}`;
  }

  const hashSuffix = fragment ? `#${fragment}` : "";
  return `/connections/harness/llm-runtime${querySuffix}${hashSuffix}`;
}
