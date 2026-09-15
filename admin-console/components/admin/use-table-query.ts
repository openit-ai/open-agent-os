"use client";

import * as React from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import type { AdminQuery } from "@/lib/admin-api/types";

function positiveInteger(value: string | null, fallback: number) {
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : fallback;
}

export interface UseTableQueryOptions {
  defaultPageSize?: number;
}

export interface UseTableQueryResult {
  query: AdminQuery;
  onQueryChange(next: AdminQuery): void;
}

export function useTableQuery({ defaultPageSize = 20 }: UseTableQueryOptions = {}): UseTableQueryResult {
  const pathname = usePathname();
  const router = useRouter();
  const searchParams = useSearchParams();

  const query = React.useMemo<AdminQuery>(() => {
    const search = searchParams.get("search")?.trim() || undefined;
    const sortId = searchParams.get("sort")?.trim();
    const direction = searchParams.get("direction");
    return {
      search,
      sort: sortId && (direction === "asc" || direction === "desc") ? { id: sortId, direction } : undefined,
      page: positiveInteger(searchParams.get("page"), 1),
      pageSize: positiveInteger(searchParams.get("pageSize"), defaultPageSize),
    };
  }, [defaultPageSize, searchParams]);

  const onQueryChange = React.useCallback((next: AdminQuery) => {
    const params = new URLSearchParams(searchParams.toString());
    if (next.search) params.set("search", next.search);
    else params.delete("search");
    if (next.sort) {
      params.set("sort", next.sort.id);
      params.set("direction", next.sort.direction);
    } else {
      params.delete("sort");
      params.delete("direction");
    }
    if (next.page > 1) params.set("page", String(next.page));
    else params.delete("page");
    if (next.pageSize !== defaultPageSize) params.set("pageSize", String(next.pageSize));
    else params.delete("pageSize");
    const nextSearch = params.toString();
    router.push(nextSearch ? `${pathname}?${nextSearch}` : pathname, { scroll: false });
  }, [defaultPageSize, pathname, router, searchParams]);

  return { query, onQueryChange };
}

export function useUrlFilter(key: string, defaultValue = "") {
  const pathname = usePathname();
  const router = useRouter();
  const searchParams = useSearchParams();
  const value = searchParams.get(key) ?? defaultValue;
  const setValue = React.useCallback((next: string) => {
    const params = new URLSearchParams(searchParams.toString());
    if (next && next !== defaultValue) params.set(key, next);
    else params.delete(key);
    params.delete("page");
    const nextSearch = params.toString();
    router.push(nextSearch ? `${pathname}?${nextSearch}` : pathname, { scroll: false });
  }, [defaultValue, key, pathname, router, searchParams]);
  return [value, setValue] as const;
}
