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

export function getLLMUsageSummary(): Promise<LLMUsageSummary> {
  return fetchWithMock<LLMUsageSummary>("/v1/llm/usage/summary", mockUsageSummary);
}

export function getLLMUsageHistory(params: LLMUsageHistoryParams = {}): Promise<LLMUsageHistoryResponse> {
  const qs = new URLSearchParams();
  if (params.limit) qs.set("limit", String(params.limit));
  if (params.offset) qs.set("offset", String(params.offset));
  if (params.tenant) qs.set("tenant", params.tenant);
  if (params.provider) qs.set("provider", params.provider);
  if (params.status) qs.set("status", params.status);
  const suffix = qs.toString() ? `?${qs}` : "";
  return fetchWithMock<LLMUsageHistoryResponse>(`/v1/llm/usage/history${suffix}`, () => mockUsageHistory(params.limit ?? 20));
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
