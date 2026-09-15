"use client";
import { useEffect } from "react";
import { useRouter } from "next/navigation";
export default function SecurityUpdatesCompatibilityPage() { const router = useRouter(); useEffect(() => { router.replace(`/operations/security-updates${window.location.search}`); }, [router]); return null; }
