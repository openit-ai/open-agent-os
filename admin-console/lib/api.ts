"use client";

const BASE_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
const TOKEN_KEY = "admin_token";

// ---- token helpers ----
export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem(TOKEN_KEY);
}
export function setToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token);
}
export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY);
}

// ---- generic fetch ----
export async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(init.headers as Record<string, string> | undefined),
  };
  const token = getToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;

  const res = await fetch(`${BASE_URL}${path}`, { ...init, headers });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail ?? body.message ?? JSON.stringify(body);
    } catch {
      try { detail = await res.text(); } catch { /* ignore */ }
    }
    throw new Error(detail || `Request failed: ${res.status}`);
  }
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

// ---- auth ----
export interface LoginResponse {
  access_token: string;
  token_type: string;
}
export function login(email: string, password: string): Promise<LoginResponse> {
  return apiFetch<LoginResponse>("/v1/auth/login", {
    method: "POST",
    body: JSON.stringify({ email, password }),
  });
}

// ---- infra ----
export type InfraStatus = "healthy" | "unhealthy" | "unknown";
export interface InfraService {
  id: string;
  service: string;
  host: string;
  port: number;
  health_path: string;
  status: InfraStatus;
  latency_ms: number | null;
  last_check: string | null;
  last_error?: string | null;
}
export interface InfraListResponse { items: InfraService[] }
export interface InfraCreatePayload { service: string; host: string; port: number; health_path: string }
export type InfraUpdatePayload = Partial<InfraCreatePayload>;

export function listInfra(): Promise<InfraListResponse | InfraService[]> {
  return apiFetch("/v1/infra");
}
export function createInfra(payload: InfraCreatePayload) {
  return apiFetch<InfraService>("/v1/infra", { method: "POST", body: JSON.stringify(payload) });
}
export function updateInfra(id: string, payload: InfraUpdatePayload) {
  return apiFetch<InfraService>(`/v1/infra/${id}`, { method: "PATCH", body: JSON.stringify(payload) });
}
export function deleteInfra(id: string) {
  return apiFetch<void>(`/v1/infra/${id}`, { method: "DELETE" });
}
export function probeInfra(id: string) {
  return apiFetch<InfraService>(`/v1/infra/${id}/probe`, { method: "POST" });
}

// ---- dashboard ----
export interface DashboardStats {
  total_users: number;
  total_agents: number;
  pending_approvals: number;
  audit_events_today: number;
}
export interface ApprovalItem { id: string; user_id: string; resource: string; action: string; status: string; created_at: string }
export interface AuditChainStatus { head_hash: string | null; chain_length: number; verified: boolean; last_checkpoint: string | null }

export function getDashboardStats(): Promise<DashboardStats> {
  return apiFetch("/v1/dashboard/stats");
}
export function getRecentApprovals(): Promise<{ items: ApprovalItem[] } | ApprovalItem[]> {
  return apiFetch("/v1/approvals?limit=5");
}
export function getAuditChain(): Promise<AuditChainStatus> {
  return apiFetch("/v1/audit/chain");
}

// ---- users ----
export interface AdminUserPublic {
  id: string;
  email: string;
  display_name: string;
  role: "L5" | "L4";
  created_at: string;
}
export function listUsers(): Promise<AdminUserPublic[] | { users: AdminUserPublic[] }> {
  return apiFetch("/v1/auth/users");
}
export function getMe(): Promise<AdminUserPublic> {
  return apiFetch("/v1/auth/me");
}
export function registerUser(payload: { email: string; password: string; display_name: string; role: "L5" | "L4" }): Promise<AdminUserPublic> {
  return apiFetch("/v1/auth/register", { method: "POST", body: JSON.stringify(payload) });
}
export function deleteUser(id: string): Promise<{ status: string; id: string }> {
  return apiFetch(`/v1/auth/users/${id}`, { method: "DELETE" });
}

// ---- policy ----
export interface PolicyRule {
  id: string;
  source: string;
  action: string;
  resource_pattern: string;
  effect: "ALLOW" | "DENY" | "APPROVAL_REQUIRED";
  priority: number;
  description?: string | null;
}
export interface PolicyBundle {
  id: string;
  tenant_id: string;
  name: string;
  version: string;
  rules: PolicyRule[];
}
export function getPolicyBundles(): Promise<{ bundles: PolicyBundle[]; evaluation_order: string[] }> {
  return apiFetch("/v1/policy/bundles");
}

// ---- approvals (Section 23-24) ----
export type ApprovalDecisionType = "DENIED" | "APPROVED_ONCE" | "APPROVED_USER_ALWAYS" | "APPROVED_GROUP_ALWAYS";
export type RiskLevel = "HIGH" | "MEDIUM" | "LOW";

export interface ApprovalRequestItem {
  approval_id: string;
  user_id: string;
  agent_id: string;
  action: string;
  resource: string;
  risk: RiskLevel | string;
  request_hash?: string;
  nonce?: string;
  expires_at: string;
  signature?: string | null;
  decision?: string;
  status?: string;
  decided_at?: string | null;
  decided_by?: string | null;
  created_at?: string;
}

export interface ApprovalsPendingResponse {
  pending: ApprovalRequestItem[];
  count: number;
}

export function getPendingApprovals(): Promise<ApprovalsPendingResponse> {
  return apiFetch<ApprovalsPendingResponse>("/v1/approvals/pending");
}

export function decideApproval(payload: { approval_id: string; decision: ApprovalDecisionType; decided_by?: string; group_id?: string }): Promise<ApprovalRequestItem> {
  return apiFetch<ApprovalRequestItem>("/v1/approvals/decide", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

// ---- audit (Section 30-31) ----
export interface AuditEventItem {
  event_id: string;
  event_type: string;
  timestamp: string;
  tenant_id?: string;
  user_id?: string | null;
  agent_id?: string | null;
  resource?: string | null;
  action?: string | null;
  decision?: string | null;
  policy_version?: string | null;
  delegation_id?: string | null;
  previous_hash?: string | null;
  event_hash?: string | null;
}

export interface AuditEventsResponse {
  events: AuditEventItem[];
  count: number;
  head: string | null;
}

export interface AuditVerifyResponse {
  chain_valid: boolean;
  checkpoint_valid?: boolean;
  event_count: number;
  head: string | null;
  checkpoint?: AuditCheckpoint | null;
}

export interface AuditCheckpoint {
  chain_head_hash: string;
  event_count: number;
  created_at: string;
  signature: string;
}

export function getAuditEvents(): Promise<AuditEventsResponse> {
  return apiFetch<AuditEventsResponse>("/v1/audit/events");
}

export function verifyAuditChain(): Promise<AuditVerifyResponse> {
  return apiFetch<AuditVerifyResponse>("/v1/audit/verify");
}

export function getAuditCheckpoint(): Promise<AuditCheckpoint> {
  return apiFetch<AuditCheckpoint>("/v1/audit/checkpoint");
}

// ---- credentials (delegation) ----
export interface CredentialProviderStatus {
  provider: string;
  total: number;
  active: number;
  revoked: number;
  expired: number;
  bindings?: number;
}

export interface CredentialsStatusResponse {
  providers: CredentialProviderStatus[];
  total: number;
  active: number;
  revoked: number;
  expired: number;
  recent: Array<{
    id: string;
    user_id: string;
    agent_id: string;
    provider: string;
    scope: string;
    status: string;
    created_at: string;
    expires_at?: string | null;
    revoked_at?: string | null;
  }>;
}

export function getCredentialsStatus(): Promise<CredentialsStatusResponse> {
  return apiFetch<CredentialsStatusResponse>("/v1/credentials/status");
}
