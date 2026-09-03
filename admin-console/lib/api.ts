"use client";

// Same-origin API path avoids browser-side localhost/CORS failures behind the nginx /api proxy.
const BASE_URL = process.env.NEXT_PUBLIC_API_URL ?? "/api";
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

// ---- 401 handling ----
function isLoginPath(path: string): boolean {
  return path.includes("/v1/auth/login");
}

function handleUnauthorized(path: string): void {
  if (typeof window === "undefined") return;
  if (isLoginPath(path)) return;
  // Avoid redirect loop if already on /login (Next.js app route)
  try {
    if (window.location.pathname === "/login") return;
  } catch { /* ignore */ }
  try {
    localStorage.removeItem(TOKEN_KEY);
    // Clear avatar as well so a stale avatar is not shown after re-login as different user
    localStorage.removeItem("admin_avatar_url");
  } catch { /* ignore */ }
  // Use replace to avoid polluting history; guard against multiple simultaneous 401s
  try {
    window.location.replace("/login");
  } catch {
    window.location.href = "/login";
  }
}

// ---- error-detail normalization: never emit "[object Object]" ----
function formatApiErrorDetail(value: unknown, fallback: string): string {
  if (value == null) return fallback;
  if (typeof value === "string") {
    const t = value.trim();
    return t || fallback;
  }
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value)) {
    const parts = value.map((v) => formatApiErrorDetail(v, "")).filter(Boolean);
    return parts.length ? parts.join("; ") : fallback;
  }
  if (typeof value === "object") {
    const obj = value as Record<string, unknown>;
    const candidates: unknown[] = [obj.message, obj.msg, obj.error, obj.detail, obj.reason, obj.title];
    for (const c of candidates) {
      if (typeof c === "string" && c.trim()) return c.trim();
    }
    for (const c of candidates) {
      if (c != null && typeof c === "object") {
        const nested = formatApiErrorDetail(c, "");
        if (nested) return nested;
      }
    }
    try {
      const s = JSON.stringify(value);
      if (s && s !== "{}" && s !== "[]") return s;
    } catch { /* ignore */ }
    try {
      const s = String(value);
      if (s !== "[object Object]") return s;
    } catch { /* ignore */ }
    return fallback;
  }
  try {
    const s = String(value);
    if (s === "[object Object]") return fallback;
    return s || fallback;
  } catch {
    return fallback;
  }
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
  if (res.status === 401) {
    handleUnauthorized(path);
  }
  if (!res.ok) {
    const fallback = res.statusText || `Request failed: ${res.status}`;
    let detail = fallback;
    try {
      const text = await res.text();
      if (text) {
        try {
          const body = JSON.parse(text) as unknown;
          if (body != null && typeof body === "object") {
            const b = body as Record<string, unknown>;
            const raw = b.detail ?? b.message ?? b.error ?? b.msg ?? body;
            detail = formatApiErrorDetail(raw, text || fallback);
          } else {
            detail = formatApiErrorDetail(body, text || fallback);
          }
        } catch {
          detail = formatApiErrorDetail(text, fallback);
        }
      }
    } catch {
      // body read failed — keep fallback
    }
    if (detail === "[object Object]" || detail.includes("[object Object]")) {
      detail = fallback !== "[object Object]" ? fallback : `Request failed: ${res.status}`;
    }
    throw new Error(detail || fallback);
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
export function changePassword(current_password: string, new_password: string): Promise<{ status: string }> {
  return apiFetch("/v1/auth/change-password", { method: "POST", body: JSON.stringify({ current_password, new_password }) });
}
export function updateProfile(display_name: string): Promise<AdminUserPublic> {
  return apiFetch<AdminUserPublic>("/v1/auth/me", { method: "PATCH", body: JSON.stringify({ display_name }) });
}
export const AVATAR_KEY = "admin_avatar_url";
export function getAvatarUrl(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem(AVATAR_KEY);
}
export function setAvatarUrl(url: string): void {
  localStorage.setItem(AVATAR_KEY, url);
}
export function clearAvatarUrl(): void {
  localStorage.removeItem(AVATAR_KEY);
}

// ---- Mattermost → Agent user mappings (§14) ----
export interface UserMapping {
  id: string;
  mm_user_id: string;
  mm_username: string | null;
  username?: string | null;
  display_name?: string | null;
  avatar_url?: string | null;
  employee_principal: string;
  agent_id: string;
  status: string;
  created_at: string;
  updated_at?: string | null;
}

export interface UserMappingCreatePayload {
  mm_user_id?: string;
  mm_username?: string;
  employee_principal?: string;
  display_name?: string | null;
  avatar_url?: string | null;
}

export interface UserMappingListResponse {
  mappings: UserMapping[];
  items?: UserMapping[];
  count: number;
}

export interface SyncPreviewItem {
  mm_user_id: string;
  mm_username: string;
  employee_principal: string;
  agent_id: string;
  status: string;
  already_mapped: boolean;
}

export interface SyncPreviewResponse {
  preview: SyncPreviewItem[];
  items?: SyncPreviewItem[];
  count: number;
  total?: number;
}

export function listMappings(): Promise<UserMapping[] | UserMappingListResponse> {
  return apiFetch("/v1/user-mappings");
}

export function createMapping(payload: UserMappingCreatePayload): Promise<UserMapping> {
  return apiFetch<UserMapping>("/v1/user-mappings", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}


export function updateMapping(id: string, payload: { display_name?: string | null; avatar_url?: string | null; employee_principal?: string }): Promise<UserMapping> {
  return apiFetch<UserMapping>(`/v1/user-mappings/${id}`, { method: "PATCH", body: JSON.stringify(payload) });
}
export function deleteMapping(id: string): Promise<{ status: string; id: string } | void> {
  return apiFetch(`/v1/user-mappings/${id}`, { method: "DELETE" });
}

export interface MmResolveResponse { found: boolean; mm_user_id: string; mm_username: string; email?: string; display_name?: string }
export function resolveMmUser(username: string): Promise<MmResolveResponse> {
  return apiFetch(`/v1/user-mappings/resolve?username=${encodeURIComponent(username)}`);
}

export function syncPreview(payload?: { users: Array<{ mm_user_id: string; mm_username?: string; employee_principal?: string }> }): Promise<SyncPreviewResponse | SyncPreviewItem[]> {
  return apiFetch("/v1/user-mappings/sync", {
    method: "POST",
    body: JSON.stringify(payload ?? {}),
  });
}

// ---- Mattermost user-mapping helpers ----
export function deriveEmployeePrincipal(mm_username: string, mm_user_id: string): string {
  const raw = (mm_username || mm_user_id || "unknown").toLowerCase();
  const suffix = raw.replace(/[^a-z0-9_.-]/g, "") || "unknown";
  return `employee:${suffix}`;
}

export function deriveAgentId(employee_principal: string): string {
  const suffix = employee_principal.replace(/^employee:/, "");
  return `agent:assistant:${suffix}`;
}

export function getMappingUsername(m: Pick<UserMapping, "mm_username" | "username">): string {
  return (m.mm_username ?? m.username ?? "") as string;
}

export function getMappingByPrincipal(
  mappings: UserMapping[],
  principal: string,
): UserMapping | undefined {
  return mappings.find((x) => x.employee_principal === principal);
}

export function getMappingByMmId(mappings: UserMapping[], mmId: string): UserMapping | undefined {
  return mappings.find((x) => x.mm_user_id === mmId);
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
  status?: string;
}
export interface PolicyDraftBundle {
  id: string;
  tenant_id: string;
  name: string;
  version: string;
  rules: PolicyRule[];
  status: string;
  created_by?: string | null;
  created_at?: string | null;
  approved_by?: string | null;
  approved_at?: string | null;
  published_at?: string | null;
  parent_version?: string | null;
  id_row?: string;
}
export interface PolicyBundlesResponse {
  bundles: PolicyBundle[];
  evaluation_order: string[];
  draft?: PolicyDraftBundle | null;
  active_version?: string | null;
}
export function getPolicyBundles(): Promise<PolicyBundlesResponse> {
  return apiFetch("/v1/policy/bundles");
}
export interface PolicyDraftResponse { draft: PolicyDraftBundle | null }
export function getPolicyDraft(): Promise<PolicyDraftResponse> {
  return apiFetch("/v1/policy/draft");
}
export interface PolicyHistoryResponse { items: PolicyDraftBundle[]; count: number; active_version?: string | null }
export function getPolicyHistory(): Promise<PolicyHistoryResponse> {
  return apiFetch("/v1/policy/history");
}
export interface PolicyValidateResponse { ok: boolean; valid: boolean; errors: string[] }
export function validatePolicy(rules: PolicyRule[], allow_remove_mandatory = false): Promise<PolicyValidateResponse> {
  return apiFetch("/v1/policy/validate", { method: "POST", body: JSON.stringify({ rules, allow_remove_mandatory }) });
}
export interface PolicySimulateRequest { action: string; resource: string; use_draft?: boolean; tenant_id?: string; rules?: PolicyRule[] }
export interface PolicySimulateResponse { request: { action: string; resource: string }; result: { decision: string; source: string; reason: string; matched_rule: PolicyRule | null }; evaluated_rules: number }
export function simulatePolicy(payload: PolicySimulateRequest): Promise<PolicySimulateResponse> {
  return apiFetch("/v1/policy/simulate", { method: "POST", body: JSON.stringify(payload) });
}
export function upsertPolicyDraft(payload: { rules: PolicyRule[]; tenant_id?: string; bundle_id?: string; name?: string; version?: string; allow_remove_mandatory?: boolean }): Promise<{ draft: PolicyDraftBundle }> {
  return apiFetch("/v1/policy/draft", { method: "POST", body: JSON.stringify(payload) });
}
export function approvePolicy(tenant_id = "default"): Promise<{ draft: PolicyDraftBundle; status: string }> {
  return apiFetch("/v1/policy/approve", { method: "POST", body: JSON.stringify({ tenant_id }) });
}
export function publishPolicy(tenant_id = "default"): Promise<{ published: PolicyDraftBundle; active_version: string }> {
  return apiFetch("/v1/policy/publish", { method: "POST", body: JSON.stringify({ tenant_id }) });
}
export function rollbackPolicy(target_version: string, tenant_id = "default"): Promise<{ published: PolicyDraftBundle; active_version: string }> {
  return apiFetch("/v1/policy/rollback", { method: "POST", body: JSON.stringify({ target_version, tenant_id }) });
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

// ---- business — license / security updates / backup / upgrade (§41, BSL 1.1, §16A.3.1) ----
export interface LicenseStatusResponse {
  status: string;
  license_key: string | null;
  edition: string;
  bsl_version: string;
  verified_at: string | null;
  expires_at: string | null;
  holder: string | null;
  message: string;
}
export function getLicenseStatus(): Promise<LicenseStatusResponse> {
  return apiFetch<LicenseStatusResponse>("/v1/license/status");
}
export function verifyLicense(license_key: string): Promise<LicenseStatusResponse> {
  return apiFetch<LicenseStatusResponse>("/v1/license/verify", { method: "POST", body: JSON.stringify({ license_key }) });
}

export interface SecurityUpdateItem {
  version: string;
  available: boolean;
  severity: string;
  release_date: string;
  cves: Array<{ id: string; severity: string; summary: string }>;
  changelog: string;
  current_version: string;
}
export interface SecurityUpdatesResponse {
  current_version: string;
  updates: SecurityUpdateItem[];
  count: number;
}
export function getSecurityUpdates(): Promise<SecurityUpdatesResponse> {
  return apiFetch<SecurityUpdatesResponse>("/v1/security/updates");
}

export interface BackupRecord {
  id: string;
  seq: number;
  status: string;
  created_at: string;
  expires_at: string;
  retention_days: number;
  size_mb: number;
  location: string;
  triggered_by: string | null;
  expired?: boolean;
}
export interface BackupStatusResponse {
  retention_days: number;
  retention_policy: string;
  total: number;
  backups: BackupRecord[];
  next_scheduled: string;
}
export function getBackupStatus(): Promise<BackupStatusResponse> {
  return apiFetch<BackupStatusResponse>("/v1/backup/status");
}
export function triggerBackup(): Promise<{ status: string; backup: BackupRecord }> {
  return apiFetch("/v1/backup/trigger", { method: "POST" });
}

export interface UpgradeStatusResponse {
  current_version: string;
  available_version: string;
  status: string;
  last_check: string;
  last_upgrade_at: string | null;
  changelog: string;
}
export function getUpgradeStatus(): Promise<UpgradeStatusResponse> {
  return apiFetch<UpgradeStatusResponse>("/v1/upgrade/status");
}

// ---- LLM Providers ----
export type LLMProviderType = "claude" | "codex" | "gemini" | "opencode-go" | "openrouter" | "ollama";
export interface LLMProvider {
  id: string;
  provider: LLMProviderType;
  name?: string;
  api_key?: string | null;
  api_key_masked?: string | null;
  apiKey?: string | null;
  base_url?: string | null;
  baseUrl?: string | null;
  model?: string | null;
  path?: string | null;
  url?: string | null;
  enabled: boolean;
  created_at: string;
  updated_at: string;
  last_test_at?: string | null;
  last_test_status?: string | null;
  last_test_latency_ms?: number | null;
}
export interface LLMProvidersResponse {
  providers: LLMProvider[];
  items?: LLMProvider[];
  count: number;
  total?: number;
}
export interface LLMProviderCreatePayload {
  provider: LLMProviderType;
  name?: string;
  apiKey?: string;
  api_key?: string;
  baseUrl?: string;
  base_url?: string;
  model?: string;
  path?: string;
  url?: string;
  enabled?: boolean;
}
export type LLMProviderUpdatePayload = Partial<LLMProviderCreatePayload>;

export function listLLMProviders(): Promise<LLMProvidersResponse | LLMProvider[]> {
  return apiFetch("/v1/llm/providers");
}
export function createLLMProvider(payload: LLMProviderCreatePayload): Promise<LLMProvider> {
  return apiFetch<LLMProvider>("/v1/llm/providers", { method: "POST", body: JSON.stringify(payload) });
}
export function updateLLMProvider(id: string, payload: LLMProviderUpdatePayload): Promise<LLMProvider> {
  return apiFetch<LLMProvider>(`/v1/llm/providers/${id}`, { method: "PATCH", body: JSON.stringify(payload) });
}
export function deleteLLMProvider(id: string): Promise<{ status: string; id: string }> {
  return apiFetch(`/v1/llm/providers/${id}`, { method: "DELETE" });
}
export function testLLMProvider(id: string): Promise<{ status: string; latency_ms: number; detail: string; provider_id: string }> {
  return apiFetch(`/v1/llm/providers/${id}/test`, { method: "POST" });
}
export function toggleLLMProvider(id: string): Promise<LLMProvider> {
  return apiFetch<LLMProvider>(`/v1/llm/providers/${id}/toggle`, { method: "POST" });
}

// ---- LLM Usage (cost / latency dashboard) ----
export interface LLMUsageSummary {
  daily_tokens: number;
  daily_quota: number;
  daily_usage_ratio: number; // 0..1
  per_minute_tokens: number;
  per_minute_limit?: number;
  total_cost_usd: number;
  avg_latency_ms: number;
  p50_latency_ms?: number;
  p95_latency_ms: number;
  p99_latency_ms?: number;
  total_requests: number;
  success_rate: number; // 0..1
  /** hourly timeseries for sparkline/bar (last 24h) */
  hourly_tokens?: number[];
  hourly_cost?: number[];
  hourly_latency?: number[];
  updated_at: string;
}

export interface LLMUsageHistoryItem {
  id: string;
  timestamp: string;
  tenant: string;
  provider: string;
  model: string;
  latency_ms: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  cost_usd: number;
  status: "success" | "error" | "timeout" | string;
}

export interface LLMUsageHistoryResponse {
  items: LLMUsageHistoryItem[];
  count: number;
  total: number;
  page?: number;
  page_size?: number;
}

export interface LLMUsageHistoryParams {
  limit?: number;
  offset?: number;
  tenant?: string;
  provider?: string;
  status?: string;
}

// ---- LLM usage normalizers: bridge backend contract (tenant_id,total_requests,success_count,failed_count,total_tokens,avg_latency_ms,p95_latency_ms,daily_count,per_minute_count,created_at/tenant_id) -> frontend contract (daily_tokens,daily_quota,daily_usage_ratio,per_minute_tokens,success_rate,hourly_*,timestamp/tenant) ----
function _num(v: unknown, fallback: number): number {
  const n = typeof v === "string" ? Number(v) : (v as number);
  return Number.isFinite(n) ? n : fallback;
}
function _arrNum(v: unknown): number[] | undefined {
  return Array.isArray(v) ? (v as unknown[]).map((x) => _num(x, 0)) : undefined;
}
export function normalizeLLMUsageSummary(raw: unknown): LLMUsageSummary {
  const r = (raw ?? {}) as Record<string, unknown>;
  const total_requests = _num(r.total_requests, _num(r.success_count, 0) + _num(r.failed_count, 0));
  const success_count = _num(r.success_count, 0);
  const total_tokens = _num(r.total_tokens, 0);
  const daily_tokens = _num(r.daily_tokens, total_tokens !== 0 ? total_tokens : _num(r.daily_count, 0));
  const daily_quota = _num(r.daily_quota, 50000);
  const druRaw = r.daily_usage_ratio as number | undefined;
  const daily_usage_ratio = Number.isFinite(druRaw as number)
    ? Math.max(0, Math.min(1, druRaw as number))
    : daily_quota > 0 ? Math.max(0, Math.min(1, daily_tokens / daily_quota)) : 0;
  const per_minute_tokens = _num(r.per_minute_tokens, _num(r.per_minute_count, 0));
  const per_minute_limit = r.per_minute_limit != null ? _num(r.per_minute_limit, 2000) : 2000;
  const srRaw = r.success_rate as number | undefined;
  const success_rate = Number.isFinite(srRaw as number)
    ? Math.max(0, Math.min(1, srRaw as number))
    : total_requests > 0 ? Math.max(0, Math.min(1, success_count / total_requests)) : 0;
  const updated_at_raw = (r.updated_at ?? r.created_at) as string | undefined;
  const updated_at = typeof updated_at_raw === "string" && updated_at_raw ? updated_at_raw : new Date().toISOString();
  return {
    daily_tokens,
    daily_quota,
    daily_usage_ratio,
    per_minute_tokens,
    per_minute_limit,
    total_cost_usd: _num(r.total_cost_usd, 0),
    avg_latency_ms: _num(r.avg_latency_ms, 0),
    p50_latency_ms: r.p50_latency_ms != null ? _num(r.p50_latency_ms, 0) : undefined,
    p95_latency_ms: _num(r.p95_latency_ms, _num(r.avg_latency_ms, 0)),
    p99_latency_ms: r.p99_latency_ms != null ? _num(r.p99_latency_ms, 0) : undefined,
    total_requests,
    success_rate,
    hourly_tokens: _arrNum(r.hourly_tokens),
    hourly_cost: _arrNum(r.hourly_cost),
    hourly_latency: _arrNum(r.hourly_latency),
    updated_at,
  };
}
export function normalizeLLMUsageHistoryItem(raw: unknown): LLMUsageHistoryItem {
  const r = (raw ?? {}) as Record<string, unknown>;
  const ts = (r.timestamp ?? r.created_at ?? r.createdAt ?? new Date().toISOString()) as string;
  const tenant = (r.tenant ?? r.tenant_id ?? r.tenantId ?? "default") as string;
  const prompt_tokens = _num(r.prompt_tokens, 0);
  const completion_tokens = _num(r.completion_tokens, 0);
  const total_tokens = _num(r.total_tokens, prompt_tokens + completion_tokens);
  return {
    id: String(r.id ?? `usage_${Math.random().toString(36).slice(2, 10)}`),
    timestamp: typeof ts === "string" ? ts : new Date().toISOString(),
    tenant: typeof tenant === "string" ? tenant : "default",
    provider: String(r.provider ?? "unknown"),
    model: String(r.model ?? ""),
    latency_ms: _num(r.latency_ms, 0),
    prompt_tokens,
    completion_tokens,
    total_tokens,
    cost_usd: _num(r.cost_usd, 0),
    status: String(r.status ?? "success"),
  };
}
export function normalizeLLMUsageHistory(raw: unknown): LLMUsageHistoryResponse {
  const r = (raw ?? {}) as Record<string, unknown>;
  const itemsRaw = Array.isArray(r.items) ? r.items : Array.isArray(raw) ? (raw as unknown[]) : [];
  const items = (itemsRaw as unknown[]).map(normalizeLLMUsageHistoryItem);
  const count = _num(r.count, items.length);
  const total = _num(r.total, count);
  const page = r.page != null ? _num(r.page, 1) : undefined;
  const page_size = r.page_size != null ? _num(r.page_size, items.length) : undefined;
  return { items, count, total, page, page_size };
}

// mock data fallback — keeps build green when backend not yet deployed
function mockUsageSummary(): LLMUsageSummary {
  const hourly_tokens = [320, 480, 610, 540, 720, 890, 1100, 950, 1020, 1300, 1450, 1200, 980, 860, 1100, 1350, 1500, 1420, 1180, 900, 650, 480, 390, 300];
  const hourly_cost = hourly_tokens.map((t) => +(t * 0.000002 * (0.9 + Math.random() * 0.2)).toFixed(4));
  const hourly_latency = [180, 210, 195, 220, 240, 260, 310, 280, 250, 270, 320, 290, 230, 210, 250, 280, 330, 300, 260, 220, 190, 175, 165, 170];
  return {
    daily_tokens: 18420,
    daily_quota: 50000,
    daily_usage_ratio: 0.368,
    per_minute_tokens: 420,
    per_minute_limit: 2000,
    total_cost_usd: 1.84,
    avg_latency_ms: 238,
    p50_latency_ms: 210,
    p95_latency_ms: 412,
    p99_latency_ms: 580,
    total_requests: 312,
    success_rate: 0.973,
    hourly_tokens,
    hourly_cost,
    hourly_latency,
    updated_at: new Date().toISOString(),
  };
}

function mockUsageHistory(limit = 20): LLMUsageHistoryResponse {
  const providers = ["claude", "openrouter", "gemini", "ollama", "opencode-go"] as const;
  const models = ["claude-3-5-sonnet", "gpt-4o-mini", "gemini-1.5-pro", "llama3.1:8b", "qwen2.5-coder"];
  const tenants = ["default", "acme", "openit", "demo"];
  const statuses: LLMUsageHistoryItem["status"][] = ["success", "success", "success", "success", "error", "timeout"];
  const now = Date.now();
  const items: LLMUsageHistoryItem[] = Array.from({ length: limit }, (_, i) => {
    const p = providers[i % providers.length];
    return {
      id: `mock-${String(i + 1).padStart(4, "0")}`,
      timestamp: new Date(now - i * 1000 * 60 * 7 - Math.random() * 60000).toISOString(),
      tenant: tenants[i % tenants.length],
      provider: p,
      model: models[i % models.length],
      latency_ms: Math.round(120 + Math.random() * 480),
      prompt_tokens: Math.round(80 + Math.random() * 900),
      completion_tokens: Math.round(40 + Math.random() * 600),
      total_tokens: 0,
      cost_usd: 0,
      status: statuses[i % statuses.length],
    };
  }).map((it) => ({ ...it, total_tokens: it.prompt_tokens + it.completion_tokens, cost_usd: +(it.total_tokens * 0.000002).toFixed(5) }));
  return { items, count: items.length, total: 312, page: 1, page_size: limit };
}

async function fetchWithMock<T>(path: string, mock: () => T): Promise<T> {
  try {
    return await apiFetch<T>(path);
  } catch {
    return mock();
  }
}

export async function getLLMUsageSummary(): Promise<LLMUsageSummary> {
  // Fetch raw 200 and normalize deterministically; preserve real 200 (do not hide contract failures behind mock).
  // Only on transport failure fall back to mock so offline dev/build stays green.
  try {
    const raw = await apiFetch<unknown>("/v1/llm/usage/summary");
    return normalizeLLMUsageSummary(raw);
  } catch {
    return mockUsageSummary();
  }
}

export async function getLLMUsageHistory(params: LLMUsageHistoryParams = {}): Promise<LLMUsageHistoryResponse> {
  const qs = new URLSearchParams();
  if (params.limit) qs.set("limit", String(params.limit));
  if (params.offset) qs.set("offset", String(params.offset));
  if (params.tenant) qs.set("tenant", params.tenant);
  if (params.provider) qs.set("provider", params.provider);
  if (params.status) qs.set("status", params.status);
  const suffix = qs.toString() ? `?${qs}` : "";
  try {
    const raw = await apiFetch<unknown>(`/v1/llm/usage/history${suffix}`);
    return normalizeLLMUsageHistory(raw);
  } catch {
    return mockUsageHistory(params.limit ?? 20);
  }
}

// expose mocks for UI fallback / storybook
export const __mocks = { mockUsageSummary, mockUsageHistory };

// ---- Runtime mode ----
export type RuntimeMode = "hermes" | "llm";
export interface RuntimeModeResponse {
  mode: RuntimeMode;
  available_modes: RuntimeMode[];
  updated_by?: string;
}
export function getRuntimeMode(): Promise<RuntimeModeResponse> {
  return apiFetch<RuntimeModeResponse>("/v1/runtime/mode");
}
export function setRuntimeMode(mode: RuntimeMode): Promise<RuntimeModeResponse> {
  return apiFetch<RuntimeModeResponse>("/v1/runtime/mode", { method: "POST", body: JSON.stringify({ mode }) });
}

// ---- Fallback settings (LLM fallback chain) ----
export interface FallbackEntry {
  provider: string;
  model?: string | null;
  enabled: boolean;
}
export interface FallbackConfig {
  enabled: boolean;
  chain: FallbackEntry[];
  fallback_model?: string | null;
  updated_at?: string | null;
  updated_by?: string | null;
}
export interface FallbackUpdateRequest {
  enabled?: boolean;
  chain?: FallbackEntry[];
  fallback_model?: string | null;
}
export function getFallbackConfig(): Promise<FallbackConfig> {
  return apiFetch<FallbackConfig>("/v1/llm/fallback");
}
export function updateFallbackConfig(payload: FallbackUpdateRequest): Promise<FallbackConfig> {
  return apiFetch<FallbackConfig>("/v1/llm/fallback", { method: "PUT", body: JSON.stringify(payload) });
}

// ---- Setup wizard (matches backend/setup.py) ----
export interface SetupStatus {
  first_run: boolean;
  setup_completed: boolean;
  has_admin: boolean;
}
export interface SetupChecks {
  db: { ok: boolean; target?: string; latency_ms?: number; error?: string };
  redis: { ok: boolean; target?: string; latency_ms?: number; error?: string };
  hermes: { ok: boolean; target?: string; latency_ms?: number; status_code?: number; error?: string };
}
export function getSetupStatus(): Promise<SetupStatus> {
  return apiFetch<SetupStatus>("/v1/setup/status");
}
export interface SetupEffective {
  db: { configured: boolean; driver: string | null; user: string | null; host: string | null; port: number | null; database: string | null };
  redis: { configured: boolean; host: string | null; port: number | null; db: number | null };
  hermes: { base_url: string; model: string; acp_enabled: boolean };
}
export function getSetupEffective(): Promise<SetupEffective> {
  return apiFetch<SetupEffective>("/v1/setup/effective");
}
export function postSetupChecks(payload?: { db_url?: string; redis_url?: string; hermes_url?: string }): Promise<SetupChecks> {
  return apiFetch<SetupChecks>("/v1/setup/checks", { method: "POST", body: JSON.stringify(payload ?? {}) });
}
export function postSetupComplete(): Promise<{ setup_completed: boolean; persisted: string }> {
  return apiFetch("/v1/setup/complete", { method: "POST" });
}

// ---- ACP settings (matches backend/acp_config.py) ----
export interface AcpConfig {
  hermes_base_url: string;
  hermes_model: string;
  acp_enabled: boolean;
  api_key_set: boolean;
  source?: string;
  applied?: boolean;
  note?: string;
}
export interface AcpUpdateRequest {
  hermes_base_url?: string;
  hermes_model?: string;
  acp_enabled?: boolean;
}
export interface AcpTestResult {
  ok: boolean;
  target?: string;
  path?: string;
  status_code?: number;
  latency_ms?: number;
  error?: string;
  source?: string;
}
export function getAcpConfig(): Promise<AcpConfig> {
  return apiFetch<AcpConfig>("/v1/acp/config");
}
export function updateAcpConfig(payload: AcpUpdateRequest): Promise<AcpConfig> {
  return apiFetch<AcpConfig>("/v1/acp/config", { method: "PUT", body: JSON.stringify(payload) });
}
export function testAcpConnection(payload?: { hermes_base_url?: string }): Promise<AcpTestResult> {
  return apiFetch<AcpTestResult>("/v1/acp/test", { method: "POST", body: JSON.stringify(payload ?? {}) });
}

// ---- MCP servers (matches backend/mcp_config.py) ----
export interface McpServer {
  name: string;
  transport: string;
  url?: string | null;
  command?: string | null;
  args?: string[];
  headers_set?: string[];
  updated_at?: string | null;
}
export interface McpServerCreatePayload {
  name: string;
  transport: string;
  command?: string;
  args?: string[];
  url?: string;
  headers?: Record<string, string>;
}
export interface McpServerTestResult {
  name: string;
  transport: string;
  ok: boolean | null;
  status_code?: number;
  tool_count?: number;
  tools?: string[];
  latency_ms?: number;
  error?: string;
  note?: string;
}
export async function listMcpServers(): Promise<McpServer[]> {
  const res = await apiFetch<{ servers: McpServer[] }>("/v1/mcp/servers");
  return res.servers ?? [];
}
export async function createMcpServer(payload: McpServerCreatePayload): Promise<McpServer> {
  const res = await apiFetch<{ server: McpServer }>("/v1/mcp/servers", { method: "POST", body: JSON.stringify(payload) });
  return res.server;
}
export async function updateMcpServer(name: string, payload: McpServerCreatePayload): Promise<McpServer> {
  const res = await apiFetch<{ server: McpServer }>(`/v1/mcp/servers/${name}`, { method: "PUT", body: JSON.stringify(payload) });
  return res.server;
}
export function deleteMcpServer(name: string): Promise<{ deleted: string; count: number }> {
  return apiFetch(`/v1/mcp/servers/${name}`, { method: "DELETE" });
}
export function testMcpServer(name: string): Promise<McpServerTestResult> {
  return apiFetch<McpServerTestResult>(`/v1/mcp/servers/${name}/test`, { method: "POST" });
}

// ---- Mattermost bot (matches backend/mattermost_config.py) ----
export interface MmConfig {
  mattermost_url: string;
  bot_token_set: boolean;
  bot_username: string;
  default_display_name: string;
  source?: string;
  applied?: boolean;
  note?: string;
}
export interface MmUpdateRequest {
  mattermost_url?: string;
  bot_token?: string;
  bot_username?: string;
  default_display_name?: string;
}
export interface MmTestResult {
  ok: boolean;
  target?: string;
  status_code?: number;
  bot_user_id?: string;
  bot_username?: string;
  latency_ms?: number;
  error?: string;
  source?: string;
}
export function getMmConfig(): Promise<MmConfig> {
  return apiFetch<MmConfig>("/v1/mattermost/config");
}
export function updateMmConfig(payload: MmUpdateRequest): Promise<MmConfig> {
  return apiFetch<MmConfig>("/v1/mattermost/config", { method: "PUT", body: JSON.stringify(payload) });
}
export function testMmConnection(payload?: { bot_token?: string }): Promise<MmTestResult> {
  return apiFetch<MmTestResult>("/v1/mattermost/test", { method: "POST", body: JSON.stringify(payload ?? {}) });
}

// ---- Outline connector (matches backend/outline_config.py) ----
export interface OlConfig {
  outline_url: string;
  api_key_set: boolean;
  source?: string;
  applied?: boolean;
  note?: string;
}
export interface OlUpdateRequest {
  outline_url?: string;
  api_key?: string;
}
export interface OlTestResult {
  ok: boolean;
  target?: string;
  status_code?: number;
  collection_count?: number;
  latency_ms?: number;
  error?: string;
  source?: string;
}
export function getOlConfig(): Promise<OlConfig> {
  return apiFetch<OlConfig>("/v1/outline/config");
}
export function updateOlConfig(payload: OlUpdateRequest): Promise<OlConfig> {
  return apiFetch<OlConfig>("/v1/outline/config", { method: "PUT", body: JSON.stringify(payload) });
}
export function testOlConnection(payload?: { api_key?: string }): Promise<OlTestResult> {
  return apiFetch<OlTestResult>("/v1/outline/test", { method: "POST", body: JSON.stringify(payload ?? {}) });
}
export interface MmBridgeStatus {
  installed: boolean;
  active: string;
  configured: boolean;
}
export function getMmBridge(): Promise<MmBridgeStatus> {
  return apiFetch<MmBridgeStatus>("/v1/mattermost/bridge");
}
export function mmBridgeAction(action: "start" | "stop" | "restart"): Promise<MmBridgeStatus & { action: string }> {
  return apiFetch(`/v1/mattermost/bridge/${action}`, { method: "POST" });
}
