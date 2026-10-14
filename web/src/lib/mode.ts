/**
 * Business / Personal mode. The path decides the mode (/personal/... is
 * personal), ?mode= in the URL wins on load, and the last mode is remembered
 * in localStorage under hpgbooks.mode. Only the mode flag is stored there.
 */

export type Mode = "business" | "personal";
export const MODE_KEY = "hpgbooks.mode";
/** compat: the key before the rename; read when MODE_KEY is unset. */
export const LEGACY_MODE_KEY = "hpbooks.mode";
export const PERSONAL_ROOT = "/personal";

export function isMode(value: unknown): value is Mode {
  return value === "business" || value === "personal";
}

/** The modes this install has (features.business / features.personal), business first. */
export function enabledModes(features: { business?: boolean; personal?: boolean }): Mode[] {
  const modes: Mode[] = [];
  if (features.business !== false) modes.push("business");
  if (features.personal !== false) modes.push("personal");
  return modes.length ? modes : ["business"];
}

/** `mode` when this install has it, else the mode it does have. */
export function allowedMode(mode: Mode, features: { business?: boolean; personal?: boolean }): Mode {
  const modes = enabledModes(features);
  return modes.includes(mode) ? mode : modes[0];
}

export function modeFromPath(pathname: string): Mode {
  return pathname === PERSONAL_ROOT || pathname.startsWith(`${PERSONAL_ROOT}/`) ? "personal" : "business";
}

export function readStoredMode(): Mode | null {
  try {
    const value = window.localStorage.getItem(MODE_KEY) ?? window.localStorage.getItem(LEGACY_MODE_KEY);
    return isMode(value) ? value : null;
  } catch {
    return null;
  }
}

export function storeMode(mode: Mode): void {
  try {
    window.localStorage.setItem(MODE_KEY, mode);
  } catch {
    /* storage unavailable */
  }
}

/** Business routes that have a personal page of the same purpose, and back. */
const PAIRS: [string, string][] = [
  ["/", "/personal"],
  ["/transactions", "/personal/transactions"],
  ["/accounts", "/personal/accounts"],
  ["/review", "/personal/review"],
  ["/calendar", "/personal/bills"],
  ["/rules", "/personal/categories"],
  ["/settings", "/personal/settings"],
];

/** Where to go when switching to `target`: the matching page, else that mode's dashboard. */
export function counterpartPath(pathname: string, target: Mode): string {
  if (modeFromPath(pathname) === target) return pathname;
  for (const [business, personal] of PAIRS) {
    if (target === "personal" && pathname === business) return personal;
    if (target === "business" && pathname === personal) return business;
  }
  return target === "personal" ? PERSONAL_ROOT : "/";
}

/** Query string for the other mode: the date range carries over; the business filter does not. */
export function counterpartSearch(search: string, target: Mode): string {
  const current = new URLSearchParams(search);
  const next = new URLSearchParams();
  for (const key of ["start", "end"]) {
    const value = current.get(key);
    if (value) next.set(key, value);
  }
  if (target === "personal") next.set("mode", "personal");
  const text = next.toString();
  return text ? `?${text}` : "";
}
