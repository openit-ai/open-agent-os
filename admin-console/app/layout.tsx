import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Open Agent OS — Admin Console",
  description: "Open Agent OS Admin Console",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="ko">
      <body className="min-h-screen bg-background text-foreground">{children}</body>
    </html>
  );
}
