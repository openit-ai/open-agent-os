import { Suspense } from "react";
import { UsersFeature } from "@/components/admin/management/management-features";
import { Skeleton } from "@/components/admin";
export default function UsersPage() { return <Suspense fallback={<Skeleton variant="table" rows={5} />}><UsersFeature /></Suspense>; }
