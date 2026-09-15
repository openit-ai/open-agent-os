import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderWithProviders } from "@/test/render";
import { BackupFeature, LicenseFeature, SecurityUpdatesFeature } from "./phase5-operations-features";
import * as api from "@/lib/api";

const navigation = vi.hoisted(() => ({ push: vi.fn(), replace: vi.fn(), params: new URLSearchParams() }));
vi.mock("next/navigation", () => ({ usePathname: () => "/operations/test", useRouter: () => ({ push: navigation.push, replace: navigation.replace }), useSearchParams: () => navigation.params }));
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, getToken: vi.fn(), getBackupStatus: vi.fn(), getUpgradeStatus: vi.fn(), triggerBackup: vi.fn(), getSecurityUpdates: vi.fn(), getLicenseStatus: vi.fn(), verifyLicense: vi.fn() };
});

const licenseStatus: api.LicenseStatusResponse = { status: "valid", license_key: "license-secret-value", edition: "business", bsl_version: "1.1", verified_at: "2026-09-15T00:00:00Z", expires_at: "2027-09-15T00:00:00Z", holder: "OAOS", message: "valid" };

describe("Phase 5 operations features", () => {
  beforeEach(() => {
    navigation.params = new URLSearchParams(); navigation.push.mockReset(); navigation.replace.mockReset();
    vi.mocked(api.getToken).mockReturnValue("test-token");
    vi.mocked(api.getBackupStatus).mockResolvedValue({ retention_days: 30, retention_policy: "30 days", total: 1, next_scheduled: "2026-09-16T00:00:00Z", backups: [{ id: "backup-1", seq: 1, status: "completed", created_at: "2026-09-15T00:00:00Z", expires_at: "2026-10-15T00:00:00Z", retention_days: 30, size_mb: 8, location: "internal", triggered_by: "admin" }] });
    vi.mocked(api.getUpgradeStatus).mockResolvedValue({ current_version: "1.0", available_version: "1.1", status: "idle", last_check: "2026-09-15T00:00:00Z", last_upgrade_at: null, changelog: "fixes" });
    vi.mocked(api.triggerBackup).mockResolvedValue({ status: "triggered", backup: { id: "backup-2", seq: 2, status: "pending", created_at: "2026-09-15T00:00:00Z", expires_at: "2026-10-15T00:00:00Z", retention_days: 30, size_mb: 0, location: "internal", triggered_by: "admin" } });
    vi.mocked(api.getSecurityUpdates).mockResolvedValue({ current_version: "1.0", count: 1, updates: [{ version: "1.1", available: true, severity: "high", release_date: "2026-09-15", changelog: "security", current_version: "1.0", cves: [{ id: "CVE-2026-1234", severity: "high", summary: "Security fix" }] }] });
    vi.mocked(api.getLicenseStatus).mockResolvedValue(licenseStatus);
    vi.mocked(api.verifyLicense).mockResolvedValue(licenseStatus);
  });

  it("renders searchable backup history and standardizes trigger feedback", async () => {
    const user = userEvent.setup();
    renderWithProviders(<BackupFeature />);
    await screen.findByRole("table", { name: "Backup history" });
    await user.click(screen.getByRole("button", { name: "Trigger backup" }));
    await waitFor(() => expect(api.triggerBackup).toHaveBeenCalled());
    expect(await screen.findByText("Backup triggered")).toBeVisible();
  });

  it("provides an accessible external CVE detail link", async () => {
    renderWithProviders(<SecurityUpdatesFeature />);
    const link = await screen.findByRole("link", { name: "Open CVE-2026-1234 details in a new tab" });
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
  });

  it("never displays a stored license value and clears the verification input", async () => {
    const user = userEvent.setup();
    const view = renderWithProviders(<LicenseFeature />);
    await screen.findByText("Current License Status");
    expect(view.container).not.toHaveTextContent("license-secret-value");
    const input = screen.getByLabelText(/License Key/);
    await user.type(input, "submitted-license-secret");
    await user.click(screen.getByRole("button", { name: "Verify" }));
    await waitFor(() => expect(api.verifyLicense).toHaveBeenCalledWith("submitted-license-secret"));
    expect(input).toHaveValue("");
    expect(view.container).not.toHaveTextContent("submitted-license-secret");
  });
});
