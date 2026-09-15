"use client";
import { useEffect } from "react";
import { useRouter } from "next/navigation";
export default function KnowledgeOpsCompatibilityPage() { const router = useRouter(); useEffect(() => { router.replace(`/knowledge/operations${window.location.search}`); }, [router]); return null; }
