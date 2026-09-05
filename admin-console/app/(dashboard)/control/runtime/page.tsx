"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

// /control/runtime is an alias of /runtime-config (single source of truth).
// Keeps bookmarked /runtime-config working; no snapshot logic duplicated.
export default function ControlRuntimeRedirect() {
  const router = useRouter();
  useEffect(() => {
    router.replace("/runtime-config");
  }, [router]);
  return null;
}
