"use client";
import { useEffect } from "react";
import { useRouter } from "next/navigation";
export default function FeatureFlagsCompatibilityPage() { const router = useRouter(); useEffect(() => { router.replace(`/management/feature-flags${window.location.search}`); }, [router]); return null; }
