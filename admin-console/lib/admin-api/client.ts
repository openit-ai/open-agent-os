import { ApiFetchError, apiFetch } from "@/lib/api";
import {
  AdminErrorCode,
  type AdminError,
  type AdminReadiness,
  type ConnectionDiscovery,
  type ConnectionKind,
  type SetupProgress,
  type TestConnectionRequest,
  type TestConnectionResult,
} from "@/lib/admin-api/types";

const HTTP_SERVER_TIMEOUT_MS = 8_000;
const QUICK_SERVER_TIMEOUT_MS = 5_000;
const CLIENT_ABORT_GRACE_MS = 2_000;

const messageKeys: Record<AdminErrorCode, string> = {
  [AdminErrorCode.AUTH_REQUIRED]: "admin.common.error.authRequired",
  [AdminErrorCode.PERMISSION_DENIED]: "admin.common.error.permissionDenied",
  [AdminErrorCode.UNREACHABLE]: "admin.common.error.unreachable",
  [AdminErrorCode.TIMEOUT]: "admin.common.error.timeout",
  [AdminErrorCode.NOT_APPLIED]: "admin.common.error.notApplied",
  [AdminErrorCode.REVISION_CONFLICT]: "admin.common.error.notApplied",
  [AdminErrorCode.MISCONFIGURED]: "admin.common.error.misconfigured",
  [AdminErrorCode.UNKNOWN]: "admin.common.error.unknown",
};

export class AdminApiError extends Error {
  constructor(public readonly error: AdminError) {
    super(error.message_key);
    this.name = "AdminApiError";
  }
}

function codeForStatus(status: number): AdminErrorCode {
  if (status === 401) return AdminErrorCode.AUTH_REQUIRED;
  if (status === 403) return AdminErrorCode.PERMISSION_DENIED;
  if (status === 408 || status === 504) return AdminErrorCode.TIMEOUT;
  if (status === 409 || status === 412) return AdminErrorCode.REVISION_CONFLICT;
  if (status === 422) return AdminErrorCode.MISCONFIGURED;
  return AdminErrorCode.UNKNOWN;
}

export function normalizeAdminError(cause: unknown, timedOut = false): AdminError {
  if (cause instanceof AdminApiError) return cause.error;
  if (timedOut || (cause instanceof DOMException && cause.name === "TimeoutError")) {
    return { code: AdminErrorCode.TIMEOUT, message_key: messageKeys[AdminErrorCode.TIMEOUT] };
  }
  if (cause instanceof ApiFetchError) {
    const code = codeForStatus(cause.status);
    return { code, message_key: messageKeys[code], status: cause.status };
  }
  if (cause instanceof DOMException && cause.name === "AbortError") {
    return { code: AdminErrorCode.UNKNOWN, message_key: messageKeys[AdminErrorCode.UNKNOWN] };
  }
  if (cause instanceof TypeError || (cause instanceof Error && /dns|econnrefused|failed to fetch|network/i.test(cause.message))) {
    return { code: AdminErrorCode.UNREACHABLE, message_key: messageKeys[AdminErrorCode.UNREACHABLE] };
  }
  return { code: AdminErrorCode.UNKNOWN, message_key: messageKeys[AdminErrorCode.UNKNOWN] };
}

export interface AdminFetchOptions extends RequestInit {
  timeoutKind?: "http" | "quick";
  serverTimeoutMs?: number;
}

export async function adminFetch<T>(path: string, options: AdminFetchOptions = {}): Promise<T> {
  const {
    timeoutKind = "http",
    serverTimeoutMs = timeoutKind === "quick" ? QUICK_SERVER_TIMEOUT_MS : HTTP_SERVER_TIMEOUT_MS,
    signal: externalSignal,
    headers: suppliedHeaders,
    ...init
  } = options;
  const controller = new AbortController();
  let timedOut = false;
  const abortFromCaller = () => controller.abort(externalSignal?.reason);
  if (externalSignal?.aborted) abortFromCaller();
  else externalSignal?.addEventListener("abort", abortFromCaller, { once: true });
  const timeout = setTimeout(() => {
    timedOut = true;
    controller.abort(new DOMException("Client timeout", "TimeoutError"));
  }, serverTimeoutMs + CLIENT_ABORT_GRACE_MS);

  try {
    return await apiFetch<T>(path, {
      ...init,
      signal: controller.signal,
      headers: {
        "X-Request-Timeout-Ms": String(serverTimeoutMs),
        ...(suppliedHeaders as Record<string, string> | undefined),
      },
    });
  } catch (cause) {
    throw new AdminApiError(normalizeAdminError(cause, timedOut));
  } finally {
    clearTimeout(timeout);
    externalSignal?.removeEventListener("abort", abortFromCaller);
  }
}

export function testConnection(
  connectionId: string,
  request: TestConnectionRequest,
  signal?: AbortSignal,
): Promise<TestConnectionResult> {
  return adminFetch<TestConnectionResult>(`/v1/admin/connections/${encodeURIComponent(connectionId)}/test`, {
    method: "POST",
    body: JSON.stringify({ mode: "safe", ...request }),
    timeoutKind: "http",
    signal,
  });
}

export function getConnectionDiscovery(
  kind: ConnectionKind,
  signal?: AbortSignal,
): Promise<ConnectionDiscovery> {
  return adminFetch<ConnectionDiscovery>(
    `/v1/admin/connections/discovery?kind=${encodeURIComponent(kind)}`,
    { signal, timeoutKind: "quick" },
  );
}

export function getSetupProgress(signal?: AbortSignal): Promise<SetupProgress> {
  return adminFetch<SetupProgress>("/v1/setup/progress", { signal, timeoutKind: "quick" });
}

export function getAdminReadiness(signal?: AbortSignal): Promise<AdminReadiness> {
  return adminFetch<AdminReadiness>("/v1/admin/readiness", { signal, timeoutKind: "quick" });
}

export const adminTimeouts = {
  httpServerMs: HTTP_SERVER_TIMEOUT_MS,
  quickServerMs: QUICK_SERVER_TIMEOUT_MS,
  clientGraceMs: CLIENT_ABORT_GRACE_MS,
} as const;
