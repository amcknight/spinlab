import type { AppState } from "./types";

const TOAST_TIMEOUT_MS = 8000;
const FALLBACK_POLL_MS = 5000;

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

export async function postJSON<T = unknown>(
  url: string,
  body: unknown = null,
): Promise<T | null> {
  const opts: RequestInit = { method: "POST" };
  if (body !== null) {
    opts.headers = { "Content-Type": "application/json" };
    opts.body = JSON.stringify(body);
  }
  return fetchJSON<T>(url, opts);
}

export function connectSSE(onMessage: (data: AppState) => void): EventSource {
  const es = new EventSource("/api/events");
  es.onmessage = (e) => {
    try {
      const data: AppState = JSON.parse(e.data);
      onMessage(data);
    } catch (_) {
      // malformed SSE payload
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
