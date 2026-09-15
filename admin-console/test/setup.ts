import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

/**
 * Node 22+ exposes `globalThis.localStorage` as an accessor that is `undefined`
 * unless the process runs with `--localstorage-file`. The jsdom environment used
 * by vitest does not always replace it, so pages that read the auth token throw
 * `Cannot read properties of undefined (reading 'getItem')`.
 *
 * Provide a minimal in-memory Storage so component tests behave like a browser.
 */
class MemoryStorage implements Storage {
  private store = new Map<string, string>();

  get length(): number {
    return this.store.size;
  }

  clear(): void {
    this.store.clear();
  }

  getItem(key: string): string | null {
    return this.store.has(key) ? (this.store.get(key) as string) : null;
  }

  key(index: number): string | null {
    return Array.from(this.store.keys())[index] ?? null;
  }

  removeItem(key: string): void {
    this.store.delete(key);
  }

  setItem(key: string, value: string): void {
    this.store.set(key, String(value));
  }
}

function ensureStorage(name: "localStorage" | "sessionStorage"): void {
  const current = (globalThis as Record<string, unknown>)[name];
  if (current && typeof (current as Storage).getItem === "function") return;
  const storage = new MemoryStorage();
  Object.defineProperty(globalThis, name, {
    value: storage,
    configurable: true,
    writable: true,
  });
  if (typeof window !== "undefined" && window !== (globalThis as unknown)) {
    Object.defineProperty(window, name, {
      value: storage,
      configurable: true,
      writable: true,
    });
  }
}

ensureStorage("localStorage");
ensureStorage("sessionStorage");

afterEach(() => {
  cleanup();
  localStorage.clear();
  sessionStorage.clear();
});
