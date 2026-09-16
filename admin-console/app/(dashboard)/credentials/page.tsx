"use client";
import { useEffect } from "react";
import { useRouter } from "next/navigation";
export default function CredentialsCompatibilityPage() { const router = useRouter(); useEffect(() => { router.replace(`/management/security-keys${window.location.search}`); }, [router]); return null; }
