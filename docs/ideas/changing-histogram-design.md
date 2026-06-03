# Changing histogram — design notes

A small concept doc for a live-updating distribution view. This captures *intent and design decisions only* — no implementation. The actual rendering will be rebuilt to fit an existing program, so how it's drawn is left open.

## What it is

A histogram of a distribution that refreshes periodically (a new sample lands every few seconds), holding still between updates. The point is to make *change over time* legible — how the shape shifts, sharpens, or drifts as new data arrives.

## Core decisions

**Classic bars are the default view.** The boxy step histogram is the primary, preferred representation. A bar histogram is a step function, and the view should stay honest about that.

**Keep few histograms on screen at once.** The richest version layered many past snapshots, but the cleaner default is to show the current histogram plus *one* comparison — not a deep stack. The multi-snapshot fading history is worth keeping as an *optional* mode, not the baseline. Let the viewer choose what's shown rather than displaying everything by default.

**Line variants are optional extras.** Alongside bars, the same data can render as a straight frequency polygon or a smoothed curve. Useful for comparison, but secondary to the classic bars and best treated as toggleable, not always-on.

## History and comparison

**Fading-to-grey trail (optional).** When enabled, older snapshots sit behind the current one and fade toward grey, newest drawn on top. Reads as a "wake" showing where the distribution has been.

**Diffs are pairwise.** Coloring change only makes sense between *two* things, so the design colors the current snapshot against a single chosen reference — never all pairs at once. The grey trail (if on) carries the long-run trend; the colored diff carries the latest delta. These are two separate jobs.

**Reference is selectable.** Compare the current snapshot to: the previous one (frame-to-frame jitter), the first one (drift from baseline), or the running average (deviation from the typical shape — quietest when converged, which is a nice "we've stabilized" signal).

**Color meaning.** Bins that grew vs. the reference read one color, bins that shrank read another. Teal/coral was chosen over red/green to stay on-palette and avoid colorblind issues; sign is also implied by which side of the line the fill sits on.

## Scale and updates

**One shared, stable y-axis.** The axis should scale to fit the tallest *visible* layer, not just the current snapshot. Normalizing to the current sample alone hides sharpening entirely (the current bar always fills the same height). With a shared scale, a sharpening peak visibly climbs. Tradeoff to decide later: an adaptive scale occasionally resizes the whole panel; a fixed scale never moves but can clip an unusually tall snapshot.

**Static between refreshes, with a gentle morph.** The view is still until a new sample arrives; the transition into the new shape can animate softly. A manual "refresh now" trigger is valuable for testing without waiting for the timer.

## Suggested display options

- Which views are visible (bars / straight / smoothed).
- History trail on/off, and how many snapshots / how fast it fades.
- Diff reference (off / previous / first / average).
- Update interval, and a manual trigger.
- Data source (synthetic modes were only for prototyping — real binned data plugs in here).

## Decisions left open

- Adaptive vs. fixed y-axis scale.
- Whether shape-fitted (curve-hugging) diff fills are worth it — only matters if a line view becomes primary; bars stay rectangular regardless.
- Smoothed interpolation can slightly overshoot below zero in the tails next to a tall-then-empty bin; clamp if a line view is used.

## Out of scope

Rendering technique, data structures, and framework choices — all deferred to the host program.
