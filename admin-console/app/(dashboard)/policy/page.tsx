import { Suspense } from "react";
import { Skeleton } from "@/components/admin";
import { PolicyFeature } from "@/components/admin/control/control-features";

export default function PolicyPage() {
  return (
    <Suspense fallback={<Skeleton variant="table" rows={5} />}>
      <PolicyFeature />
    </Suspense>
  );
}
