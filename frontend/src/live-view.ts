/**
 * Live practice view coordinator. Fetches /segments/{id}/live and
 * /games/{id}/live-summary in parallel, renders the route bar, segment summary,
 * and episode graph. Runs a 1s setInterval that re-renders only the route bar
 * with an updated nowSeconds — keeps Practice-saved-rate + session elapsed ticking
 * without re-fetching. Per-SSE-push callers re-invoke loadAndRenderLiveView,
 * which cancels the old tick and starts a fresh one.
 */
import { fetchJSON } from "./api";
import { renderRouteBar, type RouteBarData } from "./route-bar";
import { renderSegmentSummary } from "./segment-summary";
import { renderEpisodeGraph } from "./episode-graph";
import { renderRunGraph } from "./run-graph";
import type { LiveSegmentView, RouteSummary } from "./types";

// 1s matches human time perception for a wall-clock display and matches the
// D-Live spec's "ticks live" cadence. Anything faster spams renders without
// visible benefit; slower feels stale on Practice saved/rate.
const TICK_INTERVAL_MS = 1000;

export interface LiveViewHosts {
  routeBar: HTMLElement;
  runGraph: HTMLElement;
  segmentSummary: HTMLElement;
  graph: HTMLElement;
}

export interface LiveViewLoadOptions {
  segmentId: string;
  gameId: string;
  segmentName: string;
  title: string;
  hosts: LiveViewHosts;
}

// Module-level singleton: the practice card is a single-instance UI surface.
// Same pattern as improvement-view.ts's `_host`. destroyLiveView() must be
// called before re-loading into a different host set, and is called by
// model.ts:updatePracticeCard when the user exits practice mode.
let _tickHandle: ReturnType<typeof setInterval> | null = null;
let _lastRouteData: RouteBarData | null = null;
let _lastHosts: LiveViewHosts | null = null;
// Monotonic render generation. SSE pushes trigger overlapping
// loadAndRenderLiveView calls; only the latest may apply its render + start a
// tick. Without this guard a stale in-flight call (e.g. a live render still
// fetching when the frozen stop render begins) resolves last and starts an
// elapsed-tick interval that the newer frozen call never clears (its top-of-
// function stopTick already ran) — the route bar then ticks forever showing a
// frozen summary. Stale calls bail after their fetch, so they can't orphan one.
let _renderGen = 0;

export async function loadAndRenderLiveView(opts: LiveViewLoadOptions): Promise<void> {
  // Cancel the old elapsed-tick but DO NOT blank the DOM: this runs on every
  // SSE push, and wiping innerHTML before the (awaited) fetch leaves all three
  // panels empty for the fetch latency, then refills them — the visible
  // "MAJOR flicker". Keeping the prior content lets each render below replace
  // its host atomically once data arrives (the same flicker-free path the 1s
  // tickRouteBar already uses). Only true teardown (destroyLiveView, on exit
  // from practice) blanks the DOM.
  const myGen = ++_renderGen;
  stopTick();
  _lastHosts = opts.hosts;

  const [live, summary] = await Promise.all([
    fetchJSON<LiveSegmentView>(`/api/segments/${encodeURIComponent(opts.segmentId)}/live`)
      .catch((e: unknown) => { renderError(opts.hosts.segmentSummary, "segment live", e); return null; }),
    fetchJSON<RouteSummary>(`/api/games/${encodeURIComponent(opts.gameId)}/live-summary`)
      .catch((e: unknown) => { renderError(opts.hosts.routeBar, "route summary", e); return null; }),
  ]);

  // A newer call started while we were fetching — it owns the render and tick
  // lifecycle now. Bail before rendering or starting an interval.
  if (myGen !== _renderGen) return;

  // Frozen sessions (clean-stopped) carry session_ended_at; pin the elapsed clock
  // to that instant and skip the 1s tick so the idle view stays static.
  let frozen = false;
  if (summary) {
    frozen = summary.session_ended_at != null;
    _lastRouteData = {
      title: opts.title, gameId: opts.gameId,
      routeSummary: summary,
      nowSeconds: frozen ? summary.session_ended_at! : Date.now() / 1000,
    };
    renderRouteBar(opts.hosts.routeBar, _lastRouteData);
    renderRunGraph(opts.hosts.runGraph, summary);
  }
  if (live) {
    renderSegmentSummary(opts.hosts.segmentSummary, { name: opts.segmentName, live });
    renderEpisodeGraph(opts.hosts.graph, live);
  }

  if (!frozen) {
    _tickHandle = setInterval(tickRouteBar, TICK_INTERVAL_MS);
  }
}

function tickRouteBar(): void {
  if (!_lastRouteData || !_lastHosts) return;
  _lastRouteData = { ..._lastRouteData, nowSeconds: Date.now() / 1000 };
  renderRouteBar(_lastHosts.routeBar, _lastRouteData);
}

/** Cancel the elapsed-tick timer without touching the DOM. Used by
 * loadAndRenderLiveView on every reload so the prior content survives the
 * in-flight fetch (no blank flash). destroyLiveView() is the DOM-blanking
 * teardown for leaving practice mode. */
function stopTick(): void {
  if (_tickHandle != null) { clearInterval(_tickHandle); _tickHandle = null; }
}

export function destroyLiveView(): void {
  stopTick();
  _lastRouteData = null;
  if (_lastHosts) {
    _lastHosts.routeBar.innerHTML = "";
    _lastHosts.runGraph.innerHTML = "";
    _lastHosts.segmentSummary.innerHTML = "";
    _lastHosts.graph.innerHTML = "";
    _lastHosts = null;
  }
}

function renderError(host: HTMLElement, what: string, err: unknown): void {
  host.innerHTML = `<div class="lv-error dim">${what} unavailable: ${String(err)}</div>`;
}
