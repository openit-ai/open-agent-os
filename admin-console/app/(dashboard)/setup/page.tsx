"use client";
import { useEffect } from "react";
import { useRouter } from "next/navigation";

// /setup merged into /infra tabs — keep bookmarked URLs working.
export default function SetupRedirect() {
  const router = useRouter();
  useEffect(() => {
    router.replace("/infra");
  }, [router]);
  return null;
}
