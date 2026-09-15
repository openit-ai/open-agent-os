"use client";
import { useEffect } from "react";
import { useRouter } from "next/navigation";
export default function SecretsCompatibilityPage() { const router = useRouter(); useEffect(() => { router.replace(`/management/secrets${window.location.search}`); }, [router]); return null; }
