import type { Metadata } from "next";
import "./globals.css";
import { I18nProvider } from "@/lib/i18n";

export const metadata: Metadata = {
  title: "Open Agent OS — Admin Console",
  description: "Open Agent OS Admin Console",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen bg-background text-foreground"><I18nProvider>{children}</I18nProvider></body>
    </html>
  );
}
