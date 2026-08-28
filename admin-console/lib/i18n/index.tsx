"use client";

import React, { createContext, useContext, useEffect, useState, useCallback, useMemo } from "react";
import en from "./en.json";
import ko from "./ko.json";

// Extensible dictionary: add new languages by importing JSON and extending `dictionaries`
const dictionaries: Record<string, Record<string, unknown>> = { en, ko };

export type Lang = "en" | "ko" | (string & {});
export const LANG_STORAGE_KEY = "oaos_lang";
export const SUPPORTED_LANGS: { code: Lang; labelKey: string }[] = [
  { code: "en", labelKey: "header.english" },
  { code: "ko", labelKey: "header.korean" },
];

function getNested(obj: Record<string, unknown>, path: string): unknown {
  const parts = path.split(".");
  let cur: unknown = obj;
  for (const p of parts) {
    if (cur == null || typeof cur !== "object") return undefined;
    cur = (cur as Record<string, unknown>)[p];
  }
  return cur;
}

function interpolate(template: string, vars?: Record<string, string | number>): string {
  if (!vars) return template;
  return template.replace(/\{(\w+)\}/g, (_, k) => (vars[k] != null ? String(vars[k]) : `{${k}}`));
}

interface I18nContextValue {
  lang: Lang;
  setLang: (l: Lang) => void;
  t: (key: string, vars?: Record<string, string | number>) => string;
}

const I18nContext = createContext<I18nContextValue | null>(null);

export function I18nProvider({ children }: { children: React.ReactNode }) {
  const [lang, setLangState] = useState<Lang>("en");

  useEffect(() => {
    try {
      const saved = localStorage.getItem(LANG_STORAGE_KEY) as Lang | null;
      if (saved && dictionaries[saved]) {
        setLangState(saved);
        if (typeof document !== "undefined") document.documentElement.lang = saved;
      } else {
        if (typeof document !== "undefined") document.documentElement.lang = "en";
      }
    } catch {
      /* ignore */
    }
  }, []);

  const setLang = useCallback((l: Lang) => {
    setLangState(l);
    try {
      localStorage.setItem(LANG_STORAGE_KEY, l);
    } catch {
      /* ignore */
    }
    if (typeof document !== "undefined") document.documentElement.lang = l;
  }, []);

  const t = useCallback(
    (key: string, vars?: Record<string, string | number>): string => {
      const dict = dictionaries[lang] ?? dictionaries.en;
      const fallback = dictionaries.en;
      let val = getNested(dict as Record<string, unknown>, key);
      if (typeof val !== "string") val = getNested(fallback as Record<string, unknown>, key);
      if (typeof val !== "string") return key;
      return interpolate(val, vars);
    },
    [lang]
  );

  const value = useMemo(() => ({ lang, setLang, t }), [lang, setLang, t]);

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n(): I18nContextValue {
  const ctx = useContext(I18nContext);
  if (!ctx) throw new Error("useI18n must be used within I18nProvider");
  return ctx;
}

// Optional helper for non-hook contexts (e.g. server fallback)
export function createTranslator(lang: Lang) {
  return (key: string, vars?: Record<string, string | number>) => {
    const dict = dictionaries[lang] ?? dictionaries.en;
    const fallback = dictionaries.en;
    let val = getNested(dict as Record<string, unknown>, key);
    if (typeof val !== "string") val = getNested(fallback as Record<string, unknown>, key);
    if (typeof val !== "string") return key;
    return interpolate(val, vars);
  };
}
