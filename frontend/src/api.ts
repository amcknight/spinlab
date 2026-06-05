import type { AppState } from "./types";

const TOAST_TIMEOUT_MS = 8000;
const FALLBACK_POLL_MS = 5000;

// Bootstrap state-fetch retry. In dev the Vite dev server (the page's origin)
// comes up before the FastAPI backend binds its port, so the very first
// `/api/state` is proxied to a not-yet-listening backend and Vite returns a
// 500. Retry silently until the backend answers rather than greeting the user
// with an "Internal Server Error" toast on first launch.
// 20 × 250ms = 5s, comfortably covering the gap between Vite-ready and uvicorn
// bound (import + prewarm is typically ~1-3s after Vite starts serving).
const BOOTSTRAP_RETRY_ATTEMPTS = 20;
const BOOTSTRAP_RETRY_DELAY_MS = 250;

const sleep = (ms: number): Promise<void> =>
  new Promise((resolve) => setTimeout(resolve, ms));

// Click-to-SSE perf tracking. perfMarkLastAction stamps when a mode/lifecycle
// POST returned 2xx; connectSSE measures the gap to the next SSE message and
// logs it as the "post→SSE" latency for that action. Window for correlation:
// if no SSE arrives within PERF_SSE_WINDOW_MS, the mark is dropped (the next
// SSE was for an unrelated state change).
const PERF_SSE_WINDOW_MS = 5000;
let perfLastActionUrl: string | null = null;
let perfLastActionAt: number | null = null;

export function formatClientError(err: unknown): string {
  if (err instanceof Error) return err.message;
  if (typeof err === "string") return err;
  try {
    return JSON.stringify(err);
  } catch {
    return String(err);
  }
}

let toastTimer: ReturnType<typeof setTimeout> | null = null;

function showToast(msg: string): void {
  let el = document.getElementById("toast");
  if (!el) {
    el = document.createElement("div");
    el.id = "toast";
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.classList.add("visible");
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(
    () => el!.classList.remove("visible"),
    TOAST_TIMEOUT_MS,
  );
}

export async function fetchJSON<T = unknown>(
  url: string,
  opts: RequestInit = {},
): Promise<T | null> {
  try {
    const res = await fetch(url, opts);
    if (!res.ok) {
      let detail = res.statusText;
      try {
        detail = (await res.json()).detail || detail;
      } catch (_) {
        // no JSON body
      }
      showToast(url + ": " + detail);
      return null;
    }
    return (await res.json()) as T;
  } catch (e) {
    showToast("Request failed: " + ((e as Error).message || url));
    return null;
  }
}

/**
 * Fetch `/api/state` with silent retry, for the page bootstrap. Returns the
 * state once the backend answers 2xx, or null after `attempts` tries. Unlike
 * `fetchJSON` it shows no toast on transient failure — a cold backend during
 * launch is expected, not an error to surface. Callers open the SSE stream
 * AFTER this resolves so SSE doesn't also race the cold proxy.
 */
export async function fetchStateWithRetry(
  attempts: number = BOOTSTRAP_RETRY_ATTEMPTS,
  delayMs: number = BOOTSTRAP_RETRY_DELAY_MS,
): Promise<AppState | null> {
  for (let i = 0; i < attempts; i++) {
    try {
      const res = await fetch("/api/state");
      if (res.ok) return (await res.json()) as AppState;
    } catch (_) {
      // Backend not reachable yet during startup — fall through to retry.
    }
    if (i < attempts - 1) await sleep(delayMs);
  }
  return null;
}

export async function postJSON<T = unknown>(
  url: string,
  body: unknown = null,
): Promise<T | null> {
  const opts: RequestInit = { method: "POST" };
  if (body !== null) {
    opts.headers = { "Content-Type": "application/json" };
    opts.body = JSON.stringify(body);
  }
  const t0 = performance.now();
  const result = await fetchJSON<T>(url, opts);
  const rtt = performance.now() - t0;
  console.log(`[perf] POST ${url} rtt_ms=${rtt.toFixed(1)} ok=${result !== null}`);
  if (result !== null) {
    perfLastActionUrl = url;
    perfLastActionAt = performance.now();
  }
  return result;
}

export function connectSSE(onMessage: (data: AppState) => void): EventSource {
  const es = new EventSource("/api/events");
  es.onmessage = (e) => {
    if (perfLastActionAt !== null) {
      const gap = performance.now() - perfLastActionAt;
      if (gap <= PERF_SSE_WINDOW_MS) {
        console.log(`[perf] SSE after ${perfLastActionUrl} gap_ms=${gap.toFixed(1)}`);
      }
      perfLastActionUrl = null;
      perfLastActionAt = null;
    }
    try {
      const data: AppState = JSON.parse(e.data);
      onMessage(data);
    } catch (err) {
      console.warn("SSE: malformed payload, dropping:", formatClientError(err));
    }
  };
  es.onerror = () => {
    if (es.readyState === EventSource.CLOSED) {
      startFallbackPoll(onMessage);
    }
  };
  return es;
}

let fallbackInterval: ReturnType<typeof setInterval> | null = null;

function startFallbackPoll(onMessage: (data: AppState) => void): void {
  if (fallbackInterval) return;
  fallbackInterval = setInterval(async () => {
    const data = await fetchJSON<AppState>("/api/state");
    if (data) onMessage(data);
  }, FALLBACK_POLL_MS);
}
