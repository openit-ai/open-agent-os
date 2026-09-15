import { Suspense } from "react";
import { Skeleton } from "@/components/admin";
import { AuditFeature } from "@/components/admin/control/control-features";

export default function AuditPage() {
  return (
    <Suspense fallback={<Skeleton variant="table" rows={5} />}>
      <AuditFeature />
    </Suspense>
  );
}
