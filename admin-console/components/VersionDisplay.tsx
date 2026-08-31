"use client";

import { useEffect, useState } from "react";
import { useI18n } from "@/lib/i18n";

interface VersionInfo {
  installedVersion: string;
  latestVersion: string | null;
  updateAvailable: boolean;
}

// Fallback product version — single source of truth is admin-console/package.json (0.1.2)
// Used before API resolves so UI never shows placeholder {version}
const FALLBACK_VERSION =
  (typeof process !== "undefined" && (process.env.OAOS_VERSION || process.env.NEXT_PUBLIC_OAOS_VERSION)?.trim().replace(/^v/i, "")) || "0.1.2";

export function VersionDisplay() {
  const { t } = useI18n();
  const [info, setInfo] = useState<VersionInfo | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    const id = setTimeout(() => controller.abort(), 4000);
    fetch("/version", { signal: controller.signal, cache: "no-store" })
      .then((r) => (r.ok ? r.json() : null))
      .then((data: VersionInfo | null) => {
        if (data && data.installedVersion) setInfo(data);
      })
      .catch(() => {
        // GitHub unavailable — show only installed version, never crash
      })
      .finally(() => clearTimeout(id));
    return () => {
      clearTimeout(id);
      controller.abort();
    };
  }, []);

  const installed = info?.installedVersion ?? FALLBACK_VERSION;
  // t("header.version") is "Open Agent OS v{version}" — interpolate with real product version
  const baseText = t("header.version", { version: installed });

  // Only when GitHub confirmed a higher semver release/tag; otherwise show only installed
  // Planned 0.1.2 is NOT shown as latest until it is actually released on GitHub
  // No "업데이트 가능" text — format strictly `Open Agent OS v0.1.2 -> vX.Y.Z` with latest in red
  if (info?.updateAvailable && info.latestVersion) {
    return (
      <div className="space-y-0.5">
        <p className="text-[11px] leading-4 text-muted-foreground">{t("header.copyright")}</p>
        <p className="text-[11px] text-muted-foreground/70">
          {baseText}
          <span className="ml-1 text-red-600 dark:text-red-400" aria-label={`latest ${info.latestVersion}`}>
            -&gt; v{info.latestVersion}
          </span>
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-0.5">
      <p className="text-[11px] leading-4 text-muted-foreground">{t("header.copyright")}</p>
      <p className="text-[11px] text-muted-foreground/70">{baseText}</p>
    </div>
  );
}
