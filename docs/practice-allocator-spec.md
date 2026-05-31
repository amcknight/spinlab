# Speedrun Practice Allocator — Model Spec

A Monte Carlo model that decides **which segment to practice next**, given a reset
policy and a target time. All skill and improvement modeling is sealed inside the
segment sampler; everything else is plain simulation and arithmetic.

> **v0 status (2026-05-30):** The per-segment sampler (§0) has been substantially
> redesigned during brainstorming and is now specified at
> [`docs/superpowers/specs/2026-05-30-em-suite-sampler-design.md`](superpowers/specs/2026-05-30-em-suite-sampler-design.md).
> v0 builds only §0 + a live matrix view of the sampler's predictions. §1–§5 below
> remain valid future scope but are not built in v0.

---

## 0. Segment sampler — the only thing behind the wall

For each segment, a black box that returns a random **time** for that segment. Ask it for:

- `sample(0)` — a draw at current skill.
- `sample(1)` — a draw after **one more attempt** of practice on that segment.

One attempt is the natural unit, because that's what the sampler can produce directly.
Everything else — deaths, recency-weighting, how an attempt reshapes the distribution —
stays inside. The outer model only ever sees times.

> *Optional `sample(k)`*: a draw after `k` attempts of practice. Only needed if you want a
> single simulation to also model the skill you'd gain **over the course of that simulated
> session** (§2 runs improving you as they accumulate). Nice-to-have, harder to build, skip
> unless cheap.

## 1. One simulated run — needs §0 + the reset policy

Play one attempt: segment by segment, draw `sample(0)`, accumulate time, and after each
segment apply the reset policy — if behind its boundary, abort. The run ends as either
*aborted at segment k* or *finished, total time T*.

## 2. Many runs → statistics — needs §1, repeated

Run §1 thousands of times. Count:

- `q` — fraction that finish under the target `T*`.
- `τ̄` — average real time per attempt (aborts short, finishers long).
- `w_i` — *(optional)* average time spent in each segment.

## 3. Objective — needs §2, one formula

Pick what you're maximizing; both use the same `q` and `τ̄`, so swap freely:

- **WR per hour** = `q / τ̄`
- **P(≥1 PB this session)** = `1 − (1 − q)^(H / τ̄)`, for remaining session length `H`

## 4. Value of practicing a segment — needs §3, run twice

1. Compute the objective as-is (**baseline**).
2. For each segment `i`, recompute it with segment `i` drawing from `sample(1)` instead of
   `sample(0)`, everyone else unchanged.
3. **Value of practicing `i`** = new objective − baseline.

Use **common random numbers** (same draws in both passes) so the difference is the
improvement, not Monte Carlo noise.

To rank fairly by time spent, divide each value by the seconds that practice unit costs —
the segment's expected sample length — giving **value per second of practice**.

## 5. Decision — needs §4

Practice the segment with the highest value per second (or softmax over them). Then
practice/run, collect the new data, let the samplers update themselves, and **re-solve
from §1**.

Re-solving makes diminishing returns and "free practice from running" handle themselves:
once a segment improves — whether from deliberate practice or just from being played in
runs — its §4 value drops on the next pass, so you move on without forecasting anything.

### Practice vs. run, from the same machinery

Practicing costs session time (4h00m00s → 3h59m21s), which lowers `H` and feeds back into
§3:

- **Value of practicing** = objective at *(improved segment, H − practice cost)*.
- **Value of running** = objective at *(current skill, full H)*.

Whichever is higher tells you whether to practice or run. This naturally shuts practice off
near the end of a session, when there's no longer time for it to pay off.

---

## What you do *not* need

No derivatives. No mean/std trends. No fitted distributions. Just `sample(0)` and
`sample(1)` (optionally `sample(k)`) — which you can build and improve entirely on its own,
independent of everything above.
