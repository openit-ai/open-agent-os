export const SETUP_DEFERRED_SESSION_KEY = "oaos_setup_deferred";

export function deferSetupForSession() {
  try {
    window.sessionStorage.setItem(SETUP_DEFERRED_SESSION_KEY, "true");
  } catch {
    // Session storage can be unavailable in restricted browser contexts.
  }
}

export function clearSetupDeferral() {
  try {
    window.sessionStorage.removeItem(SETUP_DEFERRED_SESSION_KEY);
  } catch {
    // Session storage can be unavailable in restricted browser contexts.
  }
}

export function isSetupDeferredForSession() {
  try {
    return window.sessionStorage.getItem(SETUP_DEFERRED_SESSION_KEY) === "true";
  } catch {
    return false;
  }
}
