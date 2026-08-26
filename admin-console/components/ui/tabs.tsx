"use client";
import * as React from "react";
import { cn } from "@/lib/utils";

interface TabsContextValue { value: string; onValueChange: (v: string) => void }
const TabsContext = React.createContext<TabsContextValue | null>(null);

export function Tabs({ defaultValue, value: controlled, onValueChange, className, children }: { defaultValue?: string; value?: string; onValueChange?: (v: string) => void; className?: string; children: React.ReactNode }) {
  const [internal, setInternal] = React.useState(defaultValue ?? "");
  const value = controlled ?? internal;
  const handle = (v: string) => { if (!controlled) setInternal(v); onValueChange?.(v); };
  return <TabsContext.Provider value={{ value, onValueChange: handle }}><div className={cn(className)}>{children}</div></TabsContext.Provider>;
}

export function TabsList({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("inline-flex h-9 items-center justify-center rounded-lg bg-muted p-1 text-muted-foreground", className)} {...props} />;
}

export function TabsTrigger({ value, children, className, ...props }: React.ButtonHTMLAttributes<HTMLButtonElement> & { value: string }) {
  const ctx = React.useContext(TabsContext);
  if (!ctx) throw new Error("TabsTrigger must be inside Tabs");
  const active = ctx.value === value;
  return (
    <button
      className={cn("inline-flex items-center justify-center whitespace-nowrap rounded-md px-3 py-1 text-sm font-medium ring-offset-background transition-all focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:pointer-events-none disabled:opacity-50", active && "bg-background text-foreground shadow", className)}
      data-state={active ? "active" : "inactive"}
      onClick={() => ctx.onValueChange(value)}
      {...props}
    >{children}</button>
  );
}

export function TabsContent({ value, children, className, ...props }: React.HTMLAttributes<HTMLDivElement> & { value: string }) {
  const ctx = React.useContext(TabsContext);
  if (!ctx) throw new Error("TabsContent must be inside Tabs");
  if (ctx.value !== value) return null;
  return <div className={cn("mt-2 ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2", className)} {...props}>{children}</div>;
}
