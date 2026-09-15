import { render, screen, within } from "@testing-library/react";
import { readFileSync } from "node:fs";
import path from "node:path";
import { ADMIN_NAV_FIXED_ITEMS, ADMIN_NAV_GROUPS, AdminNavigation } from "./admin-navigation";

const EXPECTED_HREFS = [
  "/",
  "/setup",
  "/connections/mattermost",
  "/connections/slack",
  "/connections/notion",
  "/connections/oauth",
  "/connections/smtp",
  "/control/acp",
  "/control/runtime",
  "/control/policy",
  "/control/approvals",
  "/control/audit",
  "/execution/mcp",
  "/execution/providers",
  "/execution/fallback",
  "/execution/usage",
  "/execution/quota",
  "/knowledge/outline",
  "/knowledge/embedding",
  "/knowledge/operations",
  "/operations/health",
  "/operations/services",
  "/operations/backup",
  "/operations/security-updates",
  "/operations/license",
  "/management/users",
  "/management/credentials",
  "/management/secrets",
  "/management/feature-flags",
  "/management/profile-operations",
];

describe("AdminNavigation", () => {
  it("defines the master six-group IA with two fixed canonical links", () => {
    expect(ADMIN_NAV_FIXED_ITEMS).toHaveLength(2);
    expect(ADMIN_NAV_GROUPS.map((group) => group.id)).toEqual([
      "connections",
      "control",
      "execution",
      "knowledge",
      "operations",
      "management",
    ]);
    expect([
      ...ADMIN_NAV_FIXED_ITEMS,
      ...ADMIN_NAV_GROUPS.flatMap((group) => group.items),
    ].map((item) => item.href)).toEqual(EXPECTED_HREFS);
  });

  it("renders accessible named groups, canonical links, and the active page", () => {
    render(<AdminNavigation idPrefix="desktop" pathname="/execution/providers" t={(key) => key} />);

    expect(screen.getByRole("navigation", { name: "admin.nav.label" })).toBeInTheDocument();
    expect(screen.getAllByRole("group")).toHaveLength(6);
    for (const group of ADMIN_NAV_GROUPS) {
      const region = screen.getByRole("group", { name: group.labelKey });
      expect(within(region).getAllByRole("link")).toHaveLength(group.items.length);
    }

    const links = screen.getAllByRole("link");
    expect(links.map((link) => link.getAttribute("href"))).toEqual(EXPECTED_HREFS);
    expect(screen.getByRole("link", { current: "page" })).toHaveAttribute("href", "/execution/providers");
    expect(links.every((link) => link.className.includes("focus-visible:ring-2"))).toBe(true);
  });

  it("shares the same grouped component between desktop and mobile navigation", () => {
    const layout = readFileSync(path.join(process.cwd(), "app/(dashboard)/layout.tsx"), "utf8");
    expect(layout.match(/<AdminNavigation/g)).toHaveLength(2);
  });
});
