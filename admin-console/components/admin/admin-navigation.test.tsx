import { render, screen, within } from "@testing-library/react";
import { readFileSync } from "node:fs";
import path from "node:path";
import { ADMIN_NAV_FIXED_ITEMS, ADMIN_NAV_GROUPS, AdminNavigation } from "./admin-navigation";

const EXPECTED_HREFS = [
  "/",
  "/setup",
  "/connections/mattermost",
  "/connections/slack",
  "/connections/knowledge/outline",
  "/connections/knowledge/notion",
  "/connections/knowledge/embedding",
  "/connections/knowledge/operations",
  "/connections/harness/hermes-agent",
  "/connections/harness/llm-runtime",
  "/connections/oauth",
  "/connections/smtp",
  "/control/services",
  "/control/acp",
  "/control/mcp",
  "/control/runtime",
  "/control/policy",
  "/control/approvals",
  "/control/audit",
  "/operations/health",
  "/operations/usage",
  "/management/users",
  "/management/security-keys",
  "/management/feature-flags",
  "/management/profile-operations",
  "/management/backup",
  "/management/updates",
  "/management/license",
];

const allNavigationItems = [
  ...ADMIN_NAV_FIXED_ITEMS,
  ...ADMIN_NAV_GROUPS.flatMap((group) => [
    ...(group.subgroups?.flatMap((subgroup) => subgroup.items) ?? []),
    ...group.items,
  ]),
];

describe("AdminNavigation", () => {
  it("defines the four target groups and nested connection IA", () => {
    expect(ADMIN_NAV_FIXED_ITEMS).toHaveLength(2);
    expect(ADMIN_NAV_GROUPS.map((group) => group.id)).toEqual(["connections", "control", "operations", "management"]);
    expect(ADMIN_NAV_GROUPS[0].subgroups?.map((subgroup) => subgroup.id)).toEqual(["community", "knowledge", "harness"]);
    expect(allNavigationItems.map((item) => item.href)).toEqual(EXPECTED_HREFS);
  });

  it("renders accessible named groups, canonical links, and the active page", () => {
    render(<AdminNavigation idPrefix="desktop" pathname="/connections/harness/llm-runtime" t={(key) => key} />);

    expect(screen.getByRole("navigation", { name: "admin.nav.label" })).toBeInTheDocument();
    expect(screen.getAllByRole("group")).toHaveLength(7);
    for (const group of ADMIN_NAV_GROUPS) {
      const region = screen.getByRole("group", { name: group.labelKey });
      const nestedCount = group.subgroups?.reduce((count, subgroup) => count + subgroup.items.length, 0) ?? 0;
      expect(within(region).getAllByRole("link")).toHaveLength(group.items.length + nestedCount);
      for (const subgroup of group.subgroups ?? []) {
        const nestedRegion = within(region).getByRole("group", { name: subgroup.labelKey });
        expect(within(nestedRegion).getAllByRole("link")).toHaveLength(subgroup.items.length);
      }
    }

    const links = screen.getAllByRole("link");
    expect(links.map((link) => link.getAttribute("href"))).toEqual(EXPECTED_HREFS);
    expect(screen.getByRole("link", { current: "page" })).toHaveAttribute("href", "/connections/harness/llm-runtime");
    expect(links.every((link) => link.className.includes("focus-visible:ring-2"))).toBe(true);
  });

  it("shares the same grouped component between desktop and mobile navigation", () => {
    const layout = readFileSync(path.join(process.cwd(), "app/(dashboard)/layout.tsx"), "utf8");
    expect(layout.match(/<AdminNavigation/g)).toHaveLength(2);
  });
});
