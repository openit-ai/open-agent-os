import type { AdminQuery } from "@/lib/admin-api/types";

export type ListAccessor<T> = (row: T) => unknown;

function comparable(value: unknown): string | number {
  if (typeof value === "number") return value;
  if (typeof value === "boolean") return value ? 1 : 0;
  return String(value ?? "").toLocaleLowerCase();
}

/**
 * Client-side compatibility adapter for list APIs that do not yet accept
 * additive search/sort/page parameters. Call sites must document that fact.
 */
export function applyClientListQuery<T>(
  rows: T[],
  query: AdminQuery,
  searchAccessors: Array<ListAccessor<T>>,
  sortAccessors: Record<string, ListAccessor<T>>,
): { rows: T[]; totalRows: number } {
  const needle = query.search?.trim().toLocaleLowerCase();
  const filtered = needle
    ? rows.filter((row) => searchAccessors.some((accessor) => String(accessor(row) ?? "").toLocaleLowerCase().includes(needle)))
    : rows.slice();

  const sortAccessor = query.sort ? sortAccessors[query.sort.id] : undefined;
  if (sortAccessor && query.sort) {
    const direction = query.sort.direction === "asc" ? 1 : -1;
    filtered.sort((left, right) => {
      const a = comparable(sortAccessor(left));
      const b = comparable(sortAccessor(right));
      if (typeof a === "number" && typeof b === "number") return (a - b) * direction;
      return String(a).localeCompare(String(b)) * direction;
    });
  }

  const start = (query.page - 1) * query.pageSize;
  return { rows: filtered.slice(start, start + query.pageSize), totalRows: filtered.length };
}
