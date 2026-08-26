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
