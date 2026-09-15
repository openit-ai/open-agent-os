"use client";
import { useEffect } from "react";
import { useRouter } from "next/navigation";
export default function ProfileOpsCompatibilityPage() { const router = useRouter(); useEffect(() => { router.replace(`/management/profile-operations${window.location.search}`); }, [router]); return null; }
