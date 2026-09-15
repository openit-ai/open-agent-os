import { Suspense } from "react";
import { Skeleton } from "@/components/admin";
import { SecurityUpdatesFeature } from "@/components/admin/operations/phase5-operations-features";
export default function SecurityUpdatesPage() { return <Suspense fallback={<Skeleton variant="table" rows={5} />}><SecurityUpdatesFeature /></Suspense>; }
