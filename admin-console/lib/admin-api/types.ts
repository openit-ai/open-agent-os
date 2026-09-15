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
