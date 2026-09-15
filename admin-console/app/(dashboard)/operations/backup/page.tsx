import { Suspense } from "react";
import { Skeleton } from "@/components/admin";
import { BackupFeature } from "@/components/admin/operations/phase5-operations-features";
export default function BackupPage() { return <Suspense fallback={<Skeleton variant="table" rows={5} />}><BackupFeature /></Suspense>; }
