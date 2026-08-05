/**
 * API base URL.
 *
 * Empty string = same-origin relative URLs. Works in production (FastAPI
 * serves the SPA) and in dev (vite proxies /api and /ws to :8000).
 */
export const API = "";

/** Build a WebSocket URL for the current origin. */
export function wsUrl(path: string): string {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${location.host}${path}`;
}
