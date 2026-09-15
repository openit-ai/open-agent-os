import { Suspense } from "react";
import { CredentialsFeature } from "@/components/admin/management/management-features";
import { Skeleton } from "@/components/admin";
export default function CredentialsPage() { return <Suspense fallback={<Skeleton variant="table" rows={5} />}><CredentialsFeature /></Suspense>; }
