"use client";

import { AcpSection } from "../../providers/acp-section";

// Alias view: Control Plane > ACP (canonical editor stays in providers/acp-section).
// Existing /providers URL untouched. No DB/secret change.
export default function ControlAcpPage() {
  return (
    <div className="space-y-6 p-6">
      <AcpSection />
    </div>
  );
}
