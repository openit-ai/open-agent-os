"use client";
import { useEffect } from "react";
import { useRouter } from "next/navigation";
export default function LicenseCompatibilityPage() { const router = useRouter(); useEffect(() => { router.replace(`/management/license${window.location.search}`); }, [router]); return null; }
