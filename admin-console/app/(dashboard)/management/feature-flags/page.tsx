import { Suspense } from "react";
import { FeatureFlagsFeature } from "@/components/admin/management/management-features";
import { Skeleton } from "@/components/admin";
export default function FeatureFlagsPage() { return <Suspense fallback={<Skeleton variant="table" rows={5} />}><FeatureFlagsFeature /></Suspense>; }
