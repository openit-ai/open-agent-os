"use client";

import * as React from "react";
import {
  QueryClient,
  QueryClientProvider,
  type QueryKey,
} from "@tanstack/react-query";
import { AdminApiError } from "@/lib/admin-api/client";
import { AdminErrorCode } from "@/lib/admin-api/types";

export function createAdminQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 30_000,
        refetchOnWindowFocus: false,
        retry(failureCount, error) {
          if (error instanceof AdminApiError && [AdminErrorCode.AUTH_REQUIRED, AdminErrorCode.PERMISSION_DENIED].includes(error.error.code)) return false;
          return failureCount < 1;
        },
      },
      mutations: { retry: false },
    },
  });
}

export function AdminQueryProvider({ children }: { children: React.ReactNode }) {
  const [client] = React.useState(createAdminQueryClient);
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

export function invalidateAdminQueries(queryClient: QueryClient, queryKey: QueryKey) {
  return queryClient.invalidateQueries({ queryKey });
}
