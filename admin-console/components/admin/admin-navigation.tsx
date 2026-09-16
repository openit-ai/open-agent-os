import Link from "next/link";
import {
  Activity,
  BadgeCheck,
  BarChart3,
  BookOpen,
  Bot,
  ClipboardCheck,
  Cpu,
  Database,
  DatabaseBackup,
  FileText,
  Flag,
  Globe,
  KeyRound,
  LayoutDashboard,
  Mail,
  MessageSquare,
  RefreshCcw,
  Rocket,
  ScrollText,
  Server,
  Settings2,
  Shield,
  ShieldAlert,
  Slack,
  UserCog,
  Users,
  type LucideIcon,
} from "lucide-react";
import { cn } from "@/lib/utils";

export interface AdminNavigationItem {
  href: string;
  labelKey: string;
  icon: LucideIcon;
}

export interface AdminNavigationSubgroup {
  id: string;
  labelKey: string;
  items: readonly AdminNavigationItem[];
}

export interface AdminNavigationGroup {
  id: string;
  labelKey: string;
  subgroups?: readonly AdminNavigationSubgroup[];
  items: readonly AdminNavigationItem[];
}

export const ADMIN_NAV_FIXED_ITEMS: readonly AdminNavigationItem[] = [
  { href: "/", labelKey: "admin.nav.item.dashboard", icon: LayoutDashboard },
  { href: "/setup", labelKey: "admin.nav.item.gettingStarted", icon: Rocket },
];

export const ADMIN_NAV_GROUPS: readonly AdminNavigationGroup[] = [
  {
    id: "connections",
    labelKey: "admin.nav.group.connections",
    subgroups: [
      {
        id: "community",
        labelKey: "admin.nav.subgroup.community",
        items: [
          { href: "/connections/mattermost", labelKey: "admin.nav.item.mattermost", icon: MessageSquare },
          { href: "/connections/slack", labelKey: "admin.nav.item.slack", icon: Slack },
        ],
      },
      {
        id: "knowledge",
        labelKey: "admin.nav.subgroup.knowledge",
        items: [
          { href: "/connections/knowledge/outline", labelKey: "admin.nav.item.outline", icon: BookOpen },
          { href: "/connections/knowledge/notion", labelKey: "admin.nav.item.notion", icon: FileText },
          { href: "/connections/knowledge/embedding", labelKey: "admin.nav.item.embedding", icon: Database },
          { href: "/connections/knowledge/operations", labelKey: "admin.nav.item.knowledgeOperations", icon: RefreshCcw },
        ],
      },
      {
        id: "harness",
        labelKey: "admin.nav.subgroup.harness",
        items: [
          { href: "/connections/harness/hermes-agent", labelKey: "admin.nav.item.hermesAgent", icon: Bot },
          { href: "/connections/harness/llm-runtime", labelKey: "admin.nav.item.llmRuntime", icon: Cpu },
        ],
      },
    ],
    items: [
      { href: "/connections/oauth", labelKey: "admin.nav.item.oauth", icon: KeyRound },
      { href: "/connections/smtp", labelKey: "admin.nav.item.smtp", icon: Mail },
    ],
  },
  {
    id: "control",
    labelKey: "admin.nav.group.control",
    items: [
      { href: "/control/services", labelKey: "admin.nav.item.services", icon: Server },
      { href: "/control/acp", labelKey: "admin.nav.item.acp", icon: Globe },
      { href: "/control/mcp", labelKey: "admin.nav.item.mcp", icon: Database },
      { href: "/control/runtime", labelKey: "admin.nav.item.runtime", icon: Settings2 },
      { href: "/control/policy", labelKey: "admin.nav.item.policy", icon: Shield },
      { href: "/control/approvals", labelKey: "admin.nav.item.approvals", icon: ClipboardCheck },
      { href: "/control/audit", labelKey: "admin.nav.item.audit", icon: ScrollText },
    ],
  },
  {
    id: "operations",
    labelKey: "admin.nav.group.operations",
    items: [
      { href: "/operations/health", labelKey: "admin.nav.item.health", icon: Activity },
      { href: "/operations/usage", labelKey: "admin.nav.item.usage", icon: BarChart3 },
    ],
  },
  {
    id: "management",
    labelKey: "admin.nav.group.management",
    items: [
      { href: "/management/users", labelKey: "admin.nav.item.users", icon: Users },
      { href: "/management/security-keys", labelKey: "admin.nav.item.securityKeys", icon: KeyRound },
      { href: "/management/feature-flags", labelKey: "admin.nav.item.featureFlags", icon: Flag },
      { href: "/management/profile-operations", labelKey: "admin.nav.item.profileOperations", icon: UserCog },
      { href: "/management/backup", labelKey: "admin.nav.item.backup", icon: DatabaseBackup },
      { href: "/management/updates", labelKey: "admin.nav.item.securityUpdates", icon: ShieldAlert },
      { href: "/management/license", labelKey: "admin.nav.item.license", icon: BadgeCheck },
    ],
  },
];

const linkClassName = "flex items-center gap-2 rounded-md px-3 py-2 text-sm transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2";

interface AdminNavigationProps {
  idPrefix: string;
  pathname: string;
  t: (key: string) => string;
  onNavigate?: () => void;
  className?: string;
}

function NavigationLink({ item, pathname, t, onNavigate }: Pick<AdminNavigationProps, "pathname" | "t" | "onNavigate"> & { item: AdminNavigationItem }) {
  const active = pathname === item.href;
  return (
    <Link
      href={item.href}
      onClick={onNavigate}
      aria-current={active ? "page" : undefined}
      className={cn(linkClassName, active && "bg-primary text-primary-foreground hover:bg-primary")}
    >
      <item.icon aria-hidden="true" className="h-4 w-4 shrink-0" />
      <span>{t(item.labelKey)}</span>
    </Link>
  );
}

function NavigationItems({ items, ...props }: Pick<AdminNavigationProps, "pathname" | "t" | "onNavigate"> & { items: readonly AdminNavigationItem[] }) {
  return (
    <div className="space-y-1">
      {items.map((item) => <NavigationLink key={item.href} item={item} {...props} />)}
    </div>
  );
}

export function AdminNavigation({ idPrefix, pathname, t, onNavigate, className }: AdminNavigationProps) {
  const navigationProps = { pathname, t, onNavigate };
  return (
    <nav aria-label={t("admin.nav.label")} className={cn("space-y-4", className)}>
      <NavigationItems items={ADMIN_NAV_FIXED_ITEMS} {...navigationProps} />

      {ADMIN_NAV_GROUPS.map((group) => {
        const headingId = `${idPrefix}-${group.id}-heading`;
        return (
          <section key={group.id} role="group" aria-labelledby={headingId}>
            <h2 id={headingId} className="mb-1 px-3 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              {t(group.labelKey)}
            </h2>
            {group.subgroups?.map((subgroup) => {
              const subgroupHeadingId = `${idPrefix}-${group.id}-${subgroup.id}-heading`;
              return (
                <section key={subgroup.id} role="group" aria-labelledby={subgroupHeadingId} className="mb-2">
                  <h3 id={subgroupHeadingId} className="mb-1 px-3 pl-5 text-[11px] font-medium text-muted-foreground">
                    {t(subgroup.labelKey)}
                  </h3>
                  <div className="pl-2"><NavigationItems items={subgroup.items} {...navigationProps} /></div>
                </section>
              );
            })}
            <NavigationItems items={group.items} {...navigationProps} />
          </section>
        );
      })}
    </nav>
  );
}
