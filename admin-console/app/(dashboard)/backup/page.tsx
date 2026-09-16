"use client";
import { useEffect } from "react";
import { useRouter } from "next/navigation";
export default function BackupCompatibilityPage() { const router = useRouter(); useEffect(() => { router.replace(`/management/backup${window.location.search}`); }, [router]); return null; }
