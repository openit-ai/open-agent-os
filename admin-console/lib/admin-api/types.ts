import type * as React from "react";

export enum AdminErrorCode {
  AUTH_REQUIRED = "AUTH_REQUIRED",
  PERMISSION_DENIED = "PERMISSION_DENIED",
  UNREACHABLE = "UNREACHABLE",
  TIMEOUT = "TIMEOUT",
  NOT_APPLIED = "NOT_APPLIED",
  REVISION_CONFLICT = "REVISION_CONFLICT",
  MISCONFIGURED = "MISCONFIGURED",
  UNKNOWN = "UNKNOWN",
}

export interface AdminError {
  code: AdminErrorCode;
  message_key: string;
  status?: number;
  correlation_id?: string;
  detail?: Record<string, string | number | boolean | null>;
}

export interface TestConnectionRequest {
  candidate_id?: string;
  config_revision?: string;
  mode?: "safe" | "write_probe";
}

export type TestConnectionCode =
  | "OK"
  | "AUTH_REQUIRED"
  | "UNREACHABLE"
  | "TIMEOUT"
  | "NOT_APPLIED"
  | "PERMISSION_DENIED"
  | "MISCONFIGURED"
  | "UNKNOWN";

export interface TestConnectionResult {
  ok: boolean;
  status: "healthy" | "warning" | "failed";
  code: TestConnectionCode;
  summary: string;
  message_key: string;
  checked_at: string;
  latency_ms?: number;
  target_display?: string;
  applied: boolean;
  requires_restart: boolean;
  next_action?: { label_key: string; href: string };
  correlation_id: string;
}

export interface ApplyState {
  persisted: boolean;
  applied: boolean;
  config_revision: string;
  effective_revision: string;
  requires_restart: boolean;
  apply_strategy: "immediate" | "reload" | "restart_required" | "external_action";
  updated_at: string;
  applied_at?: string;
}

export type ConnectionKind =
  | "acp"
  | "mattermost"
  | "slack"
  | "notion"
  | "oauth"
  | "smtp"
  | "outline"
  | "mcp";

export interface ConnectionDiscoveryCandidate {
  candidate_id: string;
  kind: ConnectionKind;
  display_target: string;
  source: "env" | "manifest" | "registry" | "default" | "saved";
  confidence: "high" | "medium" | "low";
  credential_state: "available" | "authorization_required" | "missing";
  applied: boolean;
  requires_restart: boolean;
}

export interface ConnectionDiscovery {
  kind: ConnectionKind;
  candidates: ConnectionDiscoveryCandidate[];
  reasons: string[];
}

export type SetupStepId =
  | "environment"
  | "runtime"
  | "ingress"
  | "policy"
  | "mcp"
  | "knowledge"
  | "notifications"
  | "verify";

export type SetupStepStatus = "pending" | "checking" | "complete" | "needs_attention" | "skipped";

export interface SetupProgressStep {
  id: SetupStepId;
  status: SetupStepStatus;
  checked_at?: string;
  blocking_checks: TestConnectionResult[];
  optional_skipped: boolean;
}

export interface SetupProgress {
  schema_version: string;
  current_step: SetupStepId;
  steps: SetupProgressStep[];
  required_complete: boolean;
  percent: number;
}

export interface ReadinessConnection {
  id: string;
  title_key: string;
  description_key: string;
  status: "healthy" | "warning" | "failed";
  required: boolean;
  configured: boolean;
  applied: boolean;
  secret_configured: boolean;
  source: string;
  checked_at?: string;
  latency_ms?: number;
  code: TestConnectionCode;
  message_key: string;
  next_action?: { label_key: string; href: string };
  affected_services?: string[];
  operation_href?: string;
}

export interface AdminReadiness {
  schema_version: string;
  required_complete: boolean;
  checked_at: string;
  connections: ReadinessConnection[];
}

export interface PageMeta {
  page: number;
  page_size: number;
  total: number;
}

export interface ColumnDef<T> {
  id: string;
  header: React.ReactNode;
  accessor?: (row: T) => unknown;
  cell?: (row: T) => React.ReactNode;
  sortable?: boolean;
  hideable?: boolean;
  width?: number | string;
  align?: "start" | "center" | "end";
}

export interface ActionProps {
  label: string;
  onClick?: () => void;
  href?: string;
  disabled?: boolean;
}

export interface RowAction {
  id: string;
  label: string;
  onSelect: () => void;
  icon?: React.ComponentType<{ className?: string; "aria-hidden"?: boolean | "true" | "false" }>;
  disabled?: boolean;
  tone?: "default" | "danger";
}

export interface AdminQuery {
  search?: string;
  sort?: { id: string; direction: "asc" | "desc" };
  page: number;
  pageSize: number;
}
