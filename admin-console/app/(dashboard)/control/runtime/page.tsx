import { Suspense } from "react";
import { RuntimeConfigFeature } from "@/components/admin/runtime-config-feature";
import { Skeleton } from "@/components/admin";

/**
 * `RuntimeConfigFeature` reads URL query state via `useSearchParams()`, which Next.js 15
 * requires to sit under a Suspense boundary during prerendering.
 */
export default function ControlRuntimePage() {
  return (
    <Suspense fallback={<Skeleton variant="table" ariaLabel="Loading" />}>
      <RuntimeConfigFeature />
    </Suspense>
  );
}
