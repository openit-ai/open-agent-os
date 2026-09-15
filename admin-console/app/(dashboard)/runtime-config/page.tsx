"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

export default function RuntimeConfigCompatibilityPage() {
  const router = useRouter();
  useEffect(() => { router.replace(`/control/runtime${window.location.search}`); }, [router]);
  return null;
}
