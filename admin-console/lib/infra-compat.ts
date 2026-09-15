export const INFRA_COMPAT_ROUTES = {
  setup: "/setup",
  mcp: "/execution/mcp",
  mm: "/connections/mattermost",
  slack: "/connections/slack",
  notion: "/connections/notion",
  oauth: "/connections/oauth",
  smtp: "/connections/smtp",
  ol: "/knowledge/outline",
  services: "/operations/services",
  live: "/operations/health",
  unified: "/operations/health",
} as const;

export function resolveInfraDestination(search: string, hash: string): string {
  const params = new URLSearchParams(search.startsWith("?") ? search.slice(1) : search);
  const tab = params.get("tab")?.trim().toLowerCase();
  const fragment = hash.replace(/^#/, "").trim().toLowerCase();
  const key = tab || fragment;
  const destination = INFRA_COMPAT_ROUTES[key as keyof typeof INFRA_COMPAT_ROUTES] ?? "/operations/health";
  params.delete("tab");
  const remaining = params.toString();
  return remaining ? `${destination}?${remaining}` : destination;
}
