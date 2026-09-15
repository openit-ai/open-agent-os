import { Suspense } from "react";
import { Skeleton } from "@/components/admin";
import { ApprovalsFeature } from "@/components/admin/control/control-features";

export default function ApprovalsPage() {
  return (
    <Suspense fallback={<Skeleton variant="table" rows={5} />}>
      <ApprovalsFeature />
    </Suspense>
  );
}
