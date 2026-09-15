"use client";

import * as React from "react";
import { useToast } from "@/components/admin/toast";

export function useBackgroundQueryToast(error: unknown, hasData: boolean, title: string) {
  const { toast } = useToast();
  const lastError = React.useRef<unknown>(undefined);

  React.useEffect(() => {
    if (!error || !hasData || lastError.current === error) return;
    lastError.current = error;
    toast({
      title,
      description: error instanceof Error ? error.message : undefined,
      variant: "error",
    });
  }, [error, hasData, title, toast]);
}
