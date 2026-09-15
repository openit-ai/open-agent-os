import Link from "next/link";
import {
  Activity,
  BadgeCheck,
  BarChart3,
  BookOpen,
  ClipboardCheck,
  Cpu,
  Database,
  DatabaseBackup,
  FileText,
  Flag,
  Gauge,
  Globe,
  KeyRound,
  Layers,
  LayoutDashboard,
  Lock,
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

export interface AdminNavigationGroup {
  id: string;
  labelKey: string;
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
    items: [
      { href: "/connections/mattermost", labelKey: "admin.nav.item.mattermost", icon: MessageSquare },
      { href: "/connections/slack", labelKey: "admin.nav.item.slack", icon: Slack },
      { href: "/connections/notion", labelKey: "admin.nav.item.notion", icon: FileText },
      { href: "/connections/oauth", labelKey: "admin.nav.item.oauth", icon: KeyRound },
      { href: "/connections/smtp", labelKey: "admin.nav.item.smtp", icon: Mail },
    ],
  },
  {
    id: "control",
    labelKey: "admin.nav.group.control",
    items: [
      { href: "/control/acp", labelKey: "admin.nav.item.acp", icon: Globe },
      { href: "/control/runtime", labelKey: "admin.nav.item.runtime", icon: Settings2 },
      { href: "/control/policy", labelKey: "admin.nav.item.policy", icon: Shield },
      { href: "/control/approvals", labelKey: "admin.nav.item.approvals", icon: ClipboardCheck },
      { href: "/control/audit", labelKey: "admin.nav.item.audit", icon: ScrollText },
    ],
  },
  {
    id: "execution",
    labelKey: "admin.nav.group.execution",
    items: [
      { href: "/execution/mcp", labelKey: "admin.nav.item.mcp", icon: Database },
      { href: "/execution/providers", labelKey: "admin.nav.item.providers", icon: Cpu },
      { href: "/execution/fallback", labelKey: "admin.nav.item.fallback", icon: Layers },
      { href: "/execution/usage", labelKey: "admin.nav.item.usage", icon: BarChart3 },
      { href: "/execution/quota", labelKey: "admin.nav.item.quota", icon: Gauge },
    ],
  },
  {
    id: "knowledge",
    labelKey: "admin.nav.group.knowledge",
    items: [
      { href: "/knowledge/outline", labelKey: "admin.nav.item.outline", icon: BookOpen },
      { href: "/knowledge/embedding", labelKey: "admin.nav.item.embedding", icon: Database },
      { href: "/knowledge/operations", labelKey: "admin.nav.item.knowledgeOperations", icon: RefreshCcw },
    ],
  },
  {
    id: "operations",
    labelKey: "admin.nav.group.operations",
    items: [
      { href: "/operations/health", labelKey: "admin.nav.item.health", icon: Activity },
      { href: "/operations/services", labelKey: "admin.nav.item.services", icon: Server },
      { href: "/operations/backup", labelKey: "admin.nav.item.backup", icon: DatabaseBackup },
      { href: "/operations/security-updates", labelKey: "admin.nav.item.securityUpdates", icon: ShieldAlert },
      { href: "/operations/license", labelKey: "admin.nav.item.license", icon: BadgeCheck },
    ],
  },
  {
    id: "management",
    labelKey: "admin.nav.group.management",
    items: [
      { href: "/management/users", labelKey: "admin.nav.item.users", icon: Users },
      { href: "/management/credentials", labelKey: "admin.nav.item.credentials", icon: KeyRound },
      { href: "/management/secrets", labelKey: "admin.nav.item.secrets", icon: Lock },
      { href: "/management/feature-flags", labelKey: "admin.nav.item.featureFlags", icon: Flag },
      { href: "/management/profile-operations", labelKey: "admin.nav.item.profileOperations", icon: UserCog },
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

export function AdminNavigation({ idPrefix, pathname, t, onNavigate, className }: AdminNavigationProps) {
  return (
    <nav aria-label={t("admin.nav.label")} className={cn("space-y-4", className)}>
      <div className="space-y-1">
        {ADMIN_NAV_FIXED_ITEMS.map((item) => (
          <NavigationLink key={item.href} item={item} pathname={pathname} t={t} onNavigate={onNavigate} />
        ))}
      </div>

      {ADMIN_NAV_GROUPS.map((group) => {
        const headingId = `${idPrefix}-${group.id}-heading`;
        return (
          <section key={group.id} role="group" aria-labelledby={headingId}>
            <h2 id={headingId} className="mb-1 px-3 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              {t(group.labelKey)}
            </h2>
            <div className="space-y-1">
              {group.items.map((item) => (
                <NavigationLink key={item.href} item={item} pathname={pathname} t={t} onNavigate={onNavigate} />
              ))}
            </div>
          </section>
        );
      })}
    </nav>
  );
}
