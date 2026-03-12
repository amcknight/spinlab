# Speedrun Practice Optimizer — Model Specification

## Overview

For each of N levels (indexed i = 1..N), we maintain a Kalman filter that
tracks two hidden states: the runner's current expected time on that level,
and the rate at which that expected time is changing (the "drift" or learning
rate). From these, we derive a single priority score per level representing
the expected improvement per second of practice time spent.

---

## Per-Level State

Each level i has a state vector and covariance matrix:

```
x_i = [ μ_i ]    state vector (2x1)
      [ d_i ]

P_i = [ P_μμ  P_μd ]    state covariance (2x2)
      [ P_dμ  P_dd ]
```

Where:
- **μ_i** — estimated current expected time (seconds) for level i
- **d_i** — estimated drift: change in expected time per run (seconds/run).
  Negative means improving. Positive means getting worse.
- **P_μμ** — variance (uncertainty) of μ estimate
- **P_dd** — variance (uncertainty) of d estimate
- **P_μd, P_dμ** — covariance between μ and d estimates

Additionally, each level has two noise parameters:

- **Q_i** — process noise matrix (2x2), how much the true state is
  expected to jitter between runs
- **R_i** — observation noise variance (scalar), how much a single
  run's time deviates from the true expected time due to execution
  variance and RNG

---

## Model Equations

### System Model (what we believe happens between runs)

The transition model says: between run n-1 and run n, the true expected
time shifts by the current drift, and both states may jitter slightly.

```
State transition matrix:

F = [ 1  1 ]
    [ 0  1 ]

Meaning:
  μ_new = μ_old + d_old    (expected time shifts by drift)
  d_new = d_old             (drift persists, changed only by process noise)
```

### Observation Model (what we see when we do a run)

When we complete a run, we observe a single time y_n. This is a noisy
observation of the true expected time:

```
Observation matrix:

H = [ 1  0 ]

Meaning:
  y_n = μ_n + noise,   noise ~ Normal(0, R_i)
```

We do not directly observe the drift. We infer it from the pattern of
observations over time.

### Predict Step (before each new observation)

```
x_predicted = F @ x_i           # propagate state
P_predicted = F @ P_i @ F^T + Q_i   # propagate covariance, add process noise
```

Expanded:
```
μ_predicted = μ_i + d_i
d_predicted = d_i

P_predicted = [ P_μμ + 2*P_μd + P_dd + q_μ     P_μd + P_dd + q_μd ]
              [ P_μd + P_dd + q_μd               P_dd + q_d         ]
```

Where q_μ, q_d, q_μd are entries of the Q_i matrix.

### Update Step (after observing run time y_n)

```
innovation:       z = y_n - H @ x_predicted = y_n - μ_predicted
innovation var:   S = H @ P_predicted @ H^T + R_i = P_predicted[0,0] + R_i
Kalman gain:      K = P_predicted @ H^T / S     # (2x1 vector)
state update:     x_i = x_predicted + K * z
covariance update: P_i = (I - K @ H) @ P_predicted
```

Expanded:
```
K = [ P_predicted[0,0] / S ]     # gain for μ
    [ P_predicted[1,0] / S ]     # gain for d

μ_i = μ_predicted + K[0] * (y_n - μ_predicted)
d_i = d_predicted + K[1] * (y_n - μ_predicted)
```

Intuition:
- If the observed time is faster than predicted (z < 0), both μ and d
  get pulled downward. The Kalman gain determines how much.
- K[1] is the critical term: it controls how much a single observation
  shifts the drift estimate. When P_dd is large (uncertain about drift),
  new observations have more influence on d.

---

## Initialization

### First run on a level:

```
μ_i = y_1          (first observation is our best guess)
d_i = d_prior       (prior drift, see below)
P_i = [ R_prior    0       ]
      [ 0          P_d0    ]
```

Where:
- **d_prior** — prior expected drift for a new level. Use the mean drift
  across all levels that have sufficient data (hierarchical prior). If no
  other data exists, use a mildly negative value like -0.5 (assuming some
  improvement is expected).
- **R_prior** — prior observation noise. Use the mean R across other levels,
  or a reasonable default (e.g., 25.0, meaning ±5s std dev).
- **P_d0** — prior uncertainty on drift. Should be large enough to let the
  filter learn quickly. Suggested: 1.0 (meaning "drift could plausibly be
  anywhere from -2 to +2 s/run").

### After 2+ runs:

Filter runs normally via predict/update cycle.

---

## Noise Parameter Estimation

### Observation noise R_i

After accumulating N_i >= 5 runs on level i, estimate R from the innovation
sequence. The innovation variance should equal S = P_predicted[0,0] + R_i.
We can estimate R_i by:

```
R_i = sample_variance(innovations) - mean(P_predicted[0,0])
R_i = max(R_i, R_floor)    # floor to prevent degeneracy, e.g. R_floor = 1.0
```

Simpler bootstrap approach: R_i ≈ variance of residuals (y_n - μ_n) over
recent runs. This is slightly biased but works well in practice.

### Process noise Q_i

Q controls how much we expect the true state to change between runs.

```
Q_i = [ q_μ    q_μd ]
      [ q_μd   q_d  ]

Suggested structure:
  q_μ  = small (e.g. 0.1) — true mean jitters slightly between runs
  q_d  = small (e.g. 0.01) — drift changes slowly
  q_μd = 0 (simplification: assume μ and d jitter independently)
```

If q_d is too large, the filter will overfit to noise in the drift.
If q_d is too small, the filter will be slow to detect when the runner
starts or stops improving.

Tuning approach: start with the defaults above. If the filter seems
sluggish (takes many runs to detect obvious improvement), increase q_d.
If drift estimates are noisy and jumpy, decrease q_d.

### Hierarchical sharing across levels

When a new level has < 5 runs, borrow noise parameters from the population:

```
R_i = mean(R_j for all j with N_j >= 10)
Q_i = mean(Q_j for all j with N_j >= 10)
d_prior = mean(d_j for all j with N_j >= 10)
```

As level i accumulates data, transition to level-specific estimates.
A simple blending:

```
weight = min(1, N_i / 20)
R_i = weight * R_i_local + (1 - weight) * R_population
```

---

## Derived Quantities

### Marginal return (the core priority signal)

```
m_i = -d_i / μ_i
```

Units: dimensionless (seconds saved per run, divided by seconds per run).
Interpretation: fraction of run time recovered per practice run.
Higher is better. Negative means you're getting worse — deprioritize.

### Drift confidence interval

```
d_i ± z_α * sqrt(P_dd)

e.g. 95% CI: d_i ± 1.96 * sqrt(P_dd)
```

If this interval contains zero, we are not confident the runner is
improving (or declining) on this level.

### Predicted expected time after k more runs

```
μ_i(k) = μ_i + k * d_i
```

Note: this is a linear extrapolation. It will be wrong over large k
because the true learning curve decelerates. Treat as valid for modest
k (say, k < 30 runs). For longer projections, acknowledge increasing
uncertainty.

### Predicted gold after k more runs

Each of the k future runs is drawn from Normal(μ_i(j), R_i) for
j = 1..k. The expected minimum of k such draws is approximately:

```
predicted_gold_i(k) = min(current_gold_i,  μ_i(k) - σ_i * C(k))

where:
  σ_i = sqrt(R_i)
  C(k) ≈ sqrt(2 * ln(k)) - (ln(ln(k)) + ln(4π)) / (2 * sqrt(2 * ln(k)))
       (Gumbel approximation for expected min of k normal draws)
       For small k, use exact values: C(1)=0, C(2)=0.56, C(5)=1.16,
       C(10)=1.54, C(20)=1.87, C(50)=2.25, C(100)=2.51
```

This accounts for both continued improvement (via d_i shifting μ down)
and lucky draws (via the order statistic).

### Sum of best (SOB) prediction

```
predicted_SOB(k_1, ..., k_N) = sum_i predicted_gold_i(k_i)
```

Where k_i is the number of practice runs allocated to level i.

---

## What the Allocator Receives

The model outputs, per level, after each run:

| Field         | Type    | Description                                    |
|---------------|---------|------------------------------------------------|
| μ_i           | float   | Current expected time (seconds)                |
| d_i           | float   | Current drift (seconds/run, negative = improving) |
| P_dd_i        | float   | Variance of drift estimate                     |
| R_i           | float   | Observation noise variance                     |
| gold_i        | float   | Best observed time                             |
| m_i           | float   | Marginal return: -d_i / μ_i                   |
| n_i           | int     | Total runs completed                           |

The allocator (greedy, softmax, bandit, etc.) uses m_i as the primary
signal. It may also use P_dd_i to express preference for levels where
the signal is confident, or to drive exploration toward levels where
the drift is uncertain.

---

## Implementation Notes

1. All matrix operations are 2x2. No libraries needed. Expand everything
   into scalar arithmetic if desired.

2. The filter processes runs sequentially. For each new observation:
   predict, update, optionally re-estimate R_i every ~10 runs.

3. Store the full state (x_i, P_i, R_i, Q_i, gold_i, n_i) per level.
   This is ~10 floats per level. For 10 levels: 100 floats total.

4. Gold is tracked separately as min(all observed times). It is not
   a Kalman output — just a running minimum.

5. The hierarchy (shared priors) is optional but recommended. Without
   it, new levels with 1-3 runs will have very uncertain drift
   estimates, which is correct but may lead to noisy priority scores.
