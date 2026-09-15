import { Suspense } from "react";
import { SecretsFeature } from "@/components/admin/management/management-features";
import { Skeleton } from "@/components/admin";
export default function SecretsPage() { return <Suspense fallback={<Skeleton variant="table" rows={5} />}><SecretsFeature /></Suspense>; }
