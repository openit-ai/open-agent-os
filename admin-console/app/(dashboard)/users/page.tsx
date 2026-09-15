"use client";
import { useEffect } from "react";
import { useRouter } from "next/navigation";
export default function UsersCompatibilityPage() { const router = useRouter(); useEffect(() => { router.replace(`/management/users${window.location.search}`); }, [router]); return null; }
