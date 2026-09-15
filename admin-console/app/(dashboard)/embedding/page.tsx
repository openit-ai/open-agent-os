"use client";
import { useEffect } from "react";
import { useRouter } from "next/navigation";
export default function EmbeddingCompatibilityPage() { const router = useRouter(); useEffect(() => { router.replace(`/knowledge/embedding${window.location.search}`); }, [router]); return null; }
