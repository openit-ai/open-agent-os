import { Suspense } from "react";
import { Skeleton } from "@/components/admin";
import { KnowledgeOperationsFeature } from "@/components/admin/knowledge/knowledge-features";
export default function KnowledgeOperationsPage() { return <Suspense fallback={<Skeleton variant="table" rows={5} />}><KnowledgeOperationsFeature /></Suspense>; }
