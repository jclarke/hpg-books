/**
 * Fetch wrapper for the hpgbooks JSON API. Same origin only. Mutations send
 * the session CSRF token in X-CSRF-Token; the token comes from /api/session.
 */

export class ApiError extends Error {
  status: number;
  retryAfterSeconds: number | null;
  constructor(message: string, status: number, retryAfterSeconds: number | null = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.retryAfterSeconds = retryAfterSeconds;
  }
}

let csrfToken = "";
let currentMode: "business" | "personal" = "business";

/** The mode the shell is in. Shared endpoints (session, search, audit) answer for it. */
export function setApiMode(mode: "business" | "personal"): void {
  currentMode = mode;
}

export function getApiMode(): "business" | "personal" {
  return currentMode;
}

const SHARED_PATHS = ["/session", "/config", "/login", "/logout", "/search", "/audit", "/review-count", "/settings", "/accounts/settings"];

/**
 * Every request names its mode. /personal/... is personal; business routes are
 * business; shared routes follow the shell's current mode. The server refuses
 * a route that does not belong to the mode it is given.
 */
export function modeForPath(path: string): "business" | "personal" {
  if (path.startsWith("/personal/") || path.startsWith("/export/personal/")) return "personal";
  const bare = path.split("?")[0];
  if (SHARED_PATHS.includes(bare) || /^\/accounts\/[^/]+\/settings$/.test(bare)) return currentMode;
  return "business";
}

function withMode(path: string, params: Params = {}): Params {
  if (params.mode) return params;
  return { ...params, mode: modeForPath(path) };
}

export function setCsrfToken(token: string): void {
  csrfToken = token;
}

export function getCsrfToken(): string {
  return csrfToken;
}

type Params = Record<string, string | number | boolean | null | undefined>;

export function buildQuery(params: Params = {}): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === null || value === undefined || value === "" || value === false) continue;
    search.set(key, value === true ? "1" : String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

async function parse<T>(response: Response): Promise<T> {
  let body: unknown = null;
  const type = response.headers.get("Content-Type") || "";
  if (type.includes("application/json")) {
    body = await response.json();
  }
  if (!response.ok) {
    const record = body && typeof body === "object" ? (body as { error?: unknown; retry_after_seconds?: unknown }) : {};
    const message = typeof record.error === "string" ? record.error : `Request failed (${response.status})`;
    const retryAfterSeconds = typeof record.retry_after_seconds === "number" ? record.retry_after_seconds : null;
    throw new ApiError(message, response.status, retryAfterSeconds);
  }
  return body as T;
}

export async function apiGet<T>(path: string, params?: Params, init?: { signal?: AbortSignal }): Promise<T> {
  const response = await fetch(`/api${path}${buildQuery(withMode(path, params))}`, {
    method: "GET",
    credentials: "same-origin",
    headers: { Accept: "application/json" },
    signal: init?.signal,
  });
  return parse<T>(response);
}

export async function apiPost<T>(path: string, body: unknown, params?: Params): Promise<T> {
  return send<T>("POST", path, body, params);
}

/** Pass `params.mode` for a route that serves both modes; the path alone reads as business. */
export async function apiPatch<T>(path: string, body: unknown, params?: Params): Promise<T> {
  return send<T>("PATCH", path, body, params);
}

/** Pass `params.mode` for a route that serves both modes; the path alone reads as business. */
export async function apiDelete<T>(path: string, params?: Params): Promise<T> {
  return send<T>("DELETE", path, undefined, params);
}

async function send<T>(method: "POST" | "PATCH" | "DELETE", path: string, body: unknown, params?: Params): Promise<T> {
  if (!csrfToken) {
    // The session query normally sets this first; fetch it if a mutation races ahead.
    const session = await apiGet<{ csrf_token: string }>("/session");
    csrfToken = session.csrf_token;
  }
  const response = await fetch(`/api${path}${buildQuery(withMode(path, params))}`, {
    method,
    credentials: "same-origin",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
      "X-CSRF-Token": csrfToken,
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  return parse<T>(response);
}

/** Download link. Personal exports name their mode; business exports keep their old URLs (no mode means business). */
export function exportUrl(path: string, params: Params): string {
  const mode = path.startsWith("/export/personal/") ? { ...params, mode: "personal" } : params;
  return `${path}${buildQuery(mode)}`;
}
