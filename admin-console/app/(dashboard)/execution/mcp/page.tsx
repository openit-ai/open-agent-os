"use client";

import { McpPanel } from "../../infra/mcp-panel";

// Alias view: Execution > MCP (canonical panel stays in infra/mcp-panel).
// Existing /infra#mcp URL untouched. No DB/secret change.
export default function ExecutionMcpPage() {
  return (
    <div className="space-y-6 p-6">
      <McpPanel />
    </div>
  );
}
