import { ApiFetchError } from "@/lib/api";
import { AdminErrorCode } from "./types";
import { normalizeAdminError } from "./client";

describe("normalizeAdminError", () => {
  it.each([
    [401, AdminErrorCode.AUTH_REQUIRED],
    [403, AdminErrorCode.PERMISSION_DENIED],
    [504, AdminErrorCode.TIMEOUT],
    [409, AdminErrorCode.REVISION_CONFLICT],
  ])("maps HTTP %s without exposing the response body", (status, code) => {
    const normalized = normalizeAdminError(new ApiFetchError("secret upstream response", status));
    expect(normalized).toEqual(expect.objectContaining({ status, code }));
    expect(JSON.stringify(normalized)).not.toContain("secret upstream response");
  });

  it("maps refused and DNS-style network failures to unreachable", () => {
    expect(normalizeAdminError(new TypeError("Failed to fetch"))).toEqual({
      code: AdminErrorCode.UNREACHABLE,
      message_key: "admin.common.error.unreachable",
    });
  });
});
