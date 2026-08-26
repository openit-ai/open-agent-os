"use client";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { LayoutDashboard, Server, Users, Shield, ClipboardCheck, ScrollText, LogOut } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { clearToken, getToken } from "@/lib/api";

const navItems = [
  { href: "/", label: "Dashboard", icon: LayoutDashboard },
  { href: "/infra", label: "Infra 관리", icon: Server },
  { href: "/users", label: "Users / Agents", icon: Users },
  { href: "/policy", label: "Policy", icon: Shield },
  { href: "/approvals", label: "Approvals", icon: ClipboardCheck },
  { href: "/audit", label: "Audit", icon: ScrollText },
];

export default function DashboardLayout({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const [email, setEmail] = useState<string | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);

  useEffect(() => {
    const token = getToken();
    if (!token) { router.replace("/login"); return; }
    try {
      const payload = JSON.parse(atob(token.split(".")[1] ?? ""));
      setEmail(payload.sub ?? payload.email ?? "admin");
    } catch { setEmail("admin"); }
  }, [router]);

  function handleLogout() {
    clearToken();
    router.replace("/login");
  }

  return (
    <div className="flex min-h-screen">
      {/* Sidebar desktop */}
      <aside className="hidden w-60 shrink-0 border-r bg-card md:flex md:flex-col">
        <div className="flex h-14 items-center border-b px-4">
          <span className="text-sm font-semibold">Open Agent OS</span>
          <span className="ml-2 rounded bg-secondary px-2 py-0.5 text-xs">Admin</span>
        </div>
        <nav className="flex-1 space-y-1 p-3">
          {navItems.map((item) => {
            const active = pathname === item.href;
            return (
              <Link key={item.href} href={item.href} className={cn("flex items-center gap-2 rounded-md px-3 py-2 text-sm transition-colors", active ? "bg-primary text-primary-foreground" : "hover:bg-accent")}>
                <item.icon className="h-4 w-4" />{item.label}
              </Link>
            );
          })}
        </nav>
      </aside>

      <div className="flex flex-1 flex-col">
        {/* Header */}
        <header className="flex h-14 items-center justify-between border-b bg-card px-4">
          <div className="flex items-center gap-2">
            <Button variant="ghost" size="icon" className="md:hidden" onClick={() => setDrawerOpen(!drawerOpen)} aria-label="메뉴">☰</Button>
            <span className="text-sm font-medium md:hidden">Open Agent OS</span>
          </div>
          <div className="flex items-center gap-3">
            <span className="text-sm text-muted-foreground hidden sm:inline">{email ?? ""}</span>
            <span className="flex h-8 w-8 items-center justify-center rounded-full bg-primary text-xs font-medium text-primary-foreground">{(email?.[0] ?? "A").toUpperCase()}</span>
            <Button variant="ghost" size="sm" onClick={handleLogout}><LogOut className="mr-1 h-4 w-4" />로그아웃</Button>
          </div>
        </header>

        {/* Mobile drawer */}
        {drawerOpen && (
          <div className="border-b bg-card p-3 md:hidden">
            <nav className="space-y-1">
              {navItems.map((item) => {
                const active = pathname === item.href;
                return (
                  <Link key={item.href} href={item.href} onClick={() => setDrawerOpen(false)} className={cn("flex items-center gap-2 rounded-md px-3 py-2 text-sm", active ? "bg-primary text-primary-foreground" : "hover:bg-accent")}>
                    <item.icon className="h-4 w-4" />{item.label}
                  </Link>
                );
              })}
            </nav>
          </div>
        )}

        <main className="flex-1 bg-muted/20 p-4 md:p-6">{children}</main>
      </div>
    </div>
  );
}
