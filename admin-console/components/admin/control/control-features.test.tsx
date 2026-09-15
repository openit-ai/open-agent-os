/**
 * Control group features (policy / approvals / audit).
 *
 * Two things are worth guarding here:
 *  1. The screens render their labels through the catalog. Approvals and audit
 *     had English column headers inline before, so a regression shows up as an
 *     English header on a Korean screen.
 *  2. Approvals and audit have no other render coverage; policy is also
 *     exercised through the route by app/(dashboard)/phase4-list-actions.test.tsx.
 *
 * `DataTable` renders its empty state instead of a header row when there are no
 * rows, so the header assertions below supply one row and the empty-state copy is
 * asserted separately.
 */
import { screen, waitFor } from "@testing-library/react";
import { renderWithProviders } from "@/test/render";
import { ApprovalsFeature, AuditFeature, PolicyFeature } from "@/components/admin/control/control-features";
import { LANG_STORAGE_KEY } from "@/lib/i18n";

const navigation = {
  params: new URLSearchParams(),
  push: vi.fn(),
  replace: vi.fn(),
};

const approvalRow = {
  approval_id: "ap-1",
  user_id: "user-1",
  agent_id: "agent-1",
  action: "read",
  resource: "doc:1",
  risk: "HIGH",
  created_at: "2026-09-15T00:00:00Z",
  expires_at: "2026-09-15T01:00:00Z",
};

const auditRow = {
  event_type: "INTERACT",
  user_id: "user-1",
  agent_id: null,
  action: "read",
  resource: "doc:1",
  decision: "ALLOW",
  timestamp: "2026-09-15T00:00:00Z",
};

vi.mock("next/navigation", () => ({
  usePathname: () => "/control/policy",
  useRouter: () => ({ push: navigation.push, replace: navigation.replace }),
  useSearchParams: () => navigation.params,
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    getToken: vi.fn(() => "test-token"),
    decideApproval: vi.fn().mockResolvedValue({ status: "ok" }),
    verifyAuditChain: vi.fn().mockResolvedValue({ chain_valid: true }),
    getPendingApprovals: vi.fn().mockResolvedValue({ pending: [] }),
    getAuditEvents: vi.fn().mockResolvedValue({ events: [], count: 0 }),
    getAuditCheckpoint: vi.fn().mockResolvedValue({ head: null }),
    getPolicyBundles: vi.fn().mockResolvedValue({ bundles: [], evaluation_order: [] }),
    getPolicyDraft: vi.fn().mockResolvedValue({ draft: null }),
    getPolicyHistory: vi.fn().mockResolvedValue({ items: [], active_version: null }),
  };
});

const api = await import("@/lib/api");

describe("control group features", () => {
  beforeEach(() => {
    navigation.params = new URLSearchParams();
    localStorage.setItem(LANG_STORAGE_KEY, "ko");
    vi.mocked(api.getPendingApprovals).mockResolvedValue({ pending: [] } as never);
    vi.mocked(api.getAuditEvents).mockResolvedValue({ events: [], count: 0 } as never);
  });

  it("exports one feature per screen so the routes stay thin", () => {
    expect(typeof PolicyFeature).toBe("function");
    expect(typeof ApprovalsFeature).toBe("function");
    expect(typeof AuditFeature).toBe("function");
  });

  it("renders the approvals headers in Korean", async () => {
    vi.mocked(api.getPendingApprovals).mockResolvedValue({ pending: [approvalRow] } as never);
    renderWithProviders(<ApprovalsFeature />);

    await waitFor(() => expect(screen.getByRole("heading", { name: "승인" })).toBeInTheDocument());
    // The table renders after the query resolves.
    await waitFor(() =>
      expect(screen.getByRole("columnheader", { name: "요청자" })).toBeInTheDocument(),
    );
    for (const header of ["에이전트", "동작", "리소스", "위험도"]) {
      expect(screen.getByRole("columnheader", { name: header })).toBeInTheDocument();
    }
  });

  it("renders the approvals empty state when nothing is pending", async () => {
    renderWithProviders(<ApprovalsFeature />);

    await waitFor(() =>
      expect(screen.getByText("대기 중인 승인이 없습니다")).toBeInTheDocument(),
    );
  });

  it("renders the audit headers in Korean", async () => {
    vi.mocked(api.getAuditEvents).mockResolvedValue({ events: [auditRow], count: 1 } as never);
    renderWithProviders(<AuditFeature />);

    await waitFor(() => expect(screen.getByRole("heading", { name: "감사 로그" })).toBeInTheDocument());
    await waitFor(() =>
      expect(screen.getByRole("columnheader", { name: "이벤트" })).toBeInTheDocument(),
    );
    for (const header of ["주체", "동작", "리소스", "판정"]) {
      expect(screen.getByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    // audit.timelineTitle is the table's accessible name, not visible copy.
    expect(screen.getByRole("table", { name: "감사 이벤트 타임라인" })).toBeInTheDocument();
  });

  it("renders the audit empty state when there are no events", async () => {
    renderWithProviders(<AuditFeature />);

    await waitFor(() => expect(screen.getByText("감사 이벤트가 없습니다")).toBeInTheDocument());
  });

  it("renders the policy screen with its Korean heading and tabs", async () => {
    renderWithProviders(<PolicyFeature />);

    await waitFor(() =>
      expect(screen.getByRole("heading", { name: "정책 번들" })).toBeInTheDocument(),
    );
    // The tab strip renders as buttons, not role="tab".
    for (const tab of ["게시됨", "초안", "시뮬레이션", "이력 / 롤백"]) {
      expect(screen.getByRole("button", { name: tab })).toBeInTheDocument();
    }
    expect(screen.getByRole("heading", { name: /평가 순서/ })).toBeInTheDocument();
    expect(screen.getByText("게시된 번들이 없습니다.")).toBeInTheDocument();
  });

  it("renders the same screens in English when that is the saved language", async () => {
    localStorage.setItem(LANG_STORAGE_KEY, "en");
    vi.mocked(api.getAuditEvents).mockResolvedValue({ events: [auditRow], count: 1 } as never);
    renderWithProviders(<AuditFeature />);

    await waitFor(() => expect(screen.getByRole("heading", { name: "Audit" })).toBeInTheDocument());
    await waitFor(() =>
      expect(screen.getByRole("columnheader", { name: "Event" })).toBeInTheDocument(),
    );
    for (const header of ["Actor", "Action", "Resource", "Decision"]) {
      expect(screen.getByRole("columnheader", { name: header })).toBeInTheDocument();
    }
  });
});
