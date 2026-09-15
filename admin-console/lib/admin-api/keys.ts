import type { AdminQuery } from "@/lib/admin-api/types";

export const adminKeys = {
  all: ["admin"] as const,
  feature: (feature: string) => [...adminKeys.all, feature] as const,
  lists: (feature: string) => [...adminKeys.feature(feature), "list"] as const,
  list: (feature: string, query: AdminQuery) => [...adminKeys.lists(feature), query] as const,
  details: (feature: string) => [...adminKeys.feature(feature), "detail"] as const,
  detail: (feature: string, id: string) => [...adminKeys.details(feature), id] as const,
  connection: (connectionId: string) => [...adminKeys.feature("connections"), connectionId] as const,
};
