import { Suspense } from "react";
import { SecurityKeysFeature } from "@/components/admin/management/security-keys-feature";
import { Skeleton } from "@/components/admin";

export default function SecurityKeysPage() {
  return (
    <Suspense fallback={<Skeleton variant="table" rows={5} />}>
      <SecurityKeysFeature />
    </Suspense>
  );
}
