# Online Density Estimation for Survival Data with Adaptive Complexity

A spec for a streaming 1D density estimator on `[0, 1]` for survival/death data, with automatic complexity selection across different numbers of mixture components.

## Goal

Maintain a live estimate of where deaths occur in `[0, 1]` for "attempts" that travel through the interval. The estimator should:

- Update online (one new observation at a time, target 60 fps redraw rate)
- Handle multimodal structure without committing to a fixed number of modes
- Respect bounded support `[0, 1]` (no probability mass outside)
- Handle censored / partial observations (trial was alive at time `T`, fate after that unknown)
- Have a small, finite parameter vector that can plug into a larger model
- Output a CDF or cumulative hazard curve for display

## Approach summary

Run `K_max` beta mixture models in parallel, with `K = 1, 2, ..., K_max` components. Each updates via streaming EM. For display, blend the models using AIC (or BIC) weights, which gives "let the data pick the complexity" behavior without needing specialized Bayesian inference machinery.

`K_max = 10` is plenty for problems with up to ~8 expected modes.

## Data model

### Observations

Each trial produces one of:

1. **Death event** at time `t ∈ [0, 1]`: the trial died at `t`. Terminal.
2. **Censored survival** at time `T`: the trial was alive at `T`, fate after unknown. Updatable.
3. **Update to a previous censored observation**: a trial previously known to be alive at time `T_old` is now known to either be alive at `T_new > T_old`, or to have died at some `t > T_old`.

Maintain a `trials` collection where each trial's status is either:
- `("died", t)` — terminal
- `("alive", T)` — current latest-known-alive time, may update

### Density model: beta mixture

For a model with `K` components:

```
f(t) = sum_{k=1..K} w_k * Beta(t | α_k, β_k)
```

with weights `w_k ≥ 0`, `sum_k w_k = 1`.

Parameterize each component as `(μ_k, κ_k)` where `μ_k ∈ (0, 1)` is the mean and `κ_k > 0` is the concentration:

```
α_k = μ_k * κ_k
β_k = (1 - μ_k) * κ_k
```

This parameterization is more natural for optimization and interpretation than direct `(α, β)`.

CDF:
```
F(t) = sum_{k=1..K} w_k * I_t(α_k, β_k)
```

where `I_t(α, β)` is the regularized incomplete beta function (`scipy.special.betainc`).

Survival: `S(t) = 1 - F(t)`.

### Likelihood

Each trial contributes to the log-likelihood based on its current status:

- `("died", t)`: contributes `log f(t)`
- `("alive", T)`: contributes `log S(T)`

Total log-likelihood is the sum across trials.

## Inference: online EM

### Per-model state

For each `K ∈ {1, ..., K_max}`, maintain:

- Component parameters: `{(μ_k, κ_k)}` for `k = 1..K`
- Component weights: `{w_k}` for `k = 1..K`
- Sufficient statistics: per-component `N_k`, `M_k`, `V_k` (see below)
- Running log-likelihood `ℓ`
- Observation count `n`

Initialize bumps evenly across `[0, 1]` with moderate concentration (e.g. `κ_k = 10`), equal weights `w_k = 1/K`.

### Sufficient statistics

For each component `k`:
- `N_k` — total responsibility weight
- `M_k` — weighted sum of `t` values
- `V_k` — weighted sum of `t^2` values

Then method-of-moments parameters:
- `μ_k = M_k / N_k`
- `σ_k^2 = V_k / N_k - μ_k^2`
- `κ_k = μ_k (1 - μ_k) / σ_k^2 - 1` (clamp ≥ 2)
- `w_k = N_k / sum_j N_j`

Method of moments is used in the M-step instead of MLE because it has a closed form, avoiding a numerical solver in the inner loop. Slightly less statistically efficient than MLE, much faster.

Optional exponential forgetting: `N_k ← λ * N_k + new_contribution` with `λ ∈ (0, 1]`. Set `λ = 1` for fully stationary data.

### Update on a death event at time t

For each model in parallel:

1. **E-step**:
   ```
   for k in 1..K:
       p_k = w_k * Beta(t | α_k, β_k)
   γ_k = p_k / sum_j p_j
   ```

2. **Sufficient stat update**:
   ```
   N_k += γ_k
   M_k += γ_k * t
   V_k += γ_k * t^2
   ```

3. **M-step** (method of moments):
   ```
   w_k = N_k / sum_j N_j
   μ_k = M_k / N_k
   σ²_k = V_k / N_k - μ_k²
   κ_k = max(2, μ_k (1 - μ_k) / σ²_k - 1)
   μ_k = clip(μ_k, 1e-3, 1 - 1e-3)
   ```

4. **Log-likelihood update**:
   ```
   ℓ += log f(t)   # using updated parameters
   n += 1
   ```

### Update on a censored observation at time T

Same structure, but using survival functions:

1. **E-step**:
   ```
   for k in 1..K:
       p_k = w_k * S_k(T)   # S_k(T) = 1 - betainc(α_k, β_k, T)
   γ_k = p_k / sum_j p_j
   ```

2. **Sufficient stat update** uses truncated conditional moments:
   ```
   E_t = E[t | t > T, component k]
   E_t2 = E[t² | t > T, component k]
   N_k += γ_k
   M_k += γ_k * E_t
   V_k += γ_k * E_t2
   ```
   These can be computed via numerical quadrature on `[T, 1]` against the component's beta density, or via closed-form recurrences. Quadrature with 32 points is fast enough and simple.

3. **M-step**: same formulas.

4. **Log-likelihood update**:
   ```
   ℓ += log S(T)
   n += 1
   ```

### Status updates

When a trial's status changes:

**Append-the-increment** (preferred): Track each trial's last-known-alive time. When the status updates from `("alive", T_old)` to `("alive", T_new)`, treat this as new evidence "did not die in `[T_old, T_new]`". The likelihood contribution of this increment is `log [S(T_new) / S(T_old)]`. Compute responsibilities and truncated moments over the interval `[T_old, T_new]` rather than `[T, 1]`.

When the update is `("alive", T_old)` → `("died", t)` with `t > T_old`: treat as "did not die in `[T_old, t]`" followed by a death at `t`. Two contributions, summed.

This is mathematically equivalent to replace-and-recompute, but avoids needing to undo old contributions. A periodic resync (every N hours, say) can be added to prevent numerical drift.

## Model averaging across K

After each update — or every M updates if computation matters — compute scores:

### AIC
```
score_K^AIC = -2 * ℓ_K + 2 * d_K
```

### BIC
```
score_K^BIC = -2 * ℓ_K + d_K * log(n)
```

where `d_K = 3K - 1` is the number of free parameters (each component contributes `μ_k`, `κ_k`; weights add `K - 1` free parameters).

### Weight computation (with log-sum-exp)

```python
scores = [score_K for K in range(1, K_max + 1)]
min_s = min(scores)
shifted = [s - min_s for s in scores]
unnormalized = [exp(-s / 2) for s in shifted]
total = sum(unnormalized)
weights = [u / total for u in unnormalized]
```

### AIC vs BIC

- **AIC**: smaller penalty, more responsive, sharper curves. Default for live display.
- **BIC**: larger penalty, more conservative, smoother curves. Use if AIC blend feels jittery.

### Effective K

```
K_eff = sum_K w_K * K
```

Useful diagnostic for how complex the data is being judged.

## Outputs

Blended density at `t`:
```
f(t) = sum_K w_K * f_K(t)
```

Derived quantities:
- **CDF**: `F(t) = sum_K w_K * F_K(t)`
- **Survival**: `S(t) = 1 - F(t)`
- **Hazard**: `h(t) = f(t) / S(t)`
- **Cumulative hazard**: `Λ(t) = -log S(t)`

For a live "where do attempts fail" display, `Λ(t)` is usually the best curve to show — its slope shows the local death rate at each `t`, uncorrected for survival selection bias.

## Suggested code structure

```python
import numpy as np
from scipy.stats import beta as scipy_beta
from scipy.special import betainc
from scipy.integrate import fixed_quad

class BetaMixture:
    def __init__(self, K, lambda_forget=1.0):
        self.K = K
        self.lambda_forget = lambda_forget
        # init parameters: μ evenly spaced, κ=10, equal weights
        self.mu = np.linspace(0.1, 0.9, K) if K > 1 else np.array([0.5])
        self.kappa = np.full(K, 10.0)
        self.w = np.full(K, 1.0 / K)
        # sufficient stats
        self.N = np.zeros(K)
        self.M = np.zeros(K)
        self.V = np.zeros(K)
        self.ll = 0.0
        self.n = 0
    
    def _alpha_beta(self):
        return self.mu * self.kappa, (1 - self.mu) * self.kappa
    
    def pdf_components(self, t):
        a, b = self._alpha_beta()
        return scipy_beta.pdf(t, a, b)
    
    def cdf_components(self, t):
        a, b = self._alpha_beta()
        return betainc(a, b, t)
    
    def density(self, t):
        return np.sum(self.w * self.pdf_components(t))
    
    def cdf(self, t):
        return np.sum(self.w * self.cdf_components(t))
    
    def update_death(self, t):
        p = self.w * self.pdf_components(t)
        gamma = p / p.sum()
        # forgetting
        self.N *= self.lambda_forget
        self.M *= self.lambda_forget
        self.V *= self.lambda_forget
        # update
        self.N += gamma
        self.M += gamma * t
        self.V += gamma * t * t
        self._m_step()
        self.ll += np.log(self.density(t) + 1e-300)
        self.n += 1
    
    def update_censored(self, T):
        a, b = self._alpha_beta()
        S_k = 1.0 - betainc(a, b, T)
        p = self.w * S_k
        gamma = p / (p.sum() + 1e-300)
        # truncated moments via quadrature on [T, 1]
        E_t, E_t2 = self._truncated_moments(T, 1.0)
        self.N *= self.lambda_forget
        self.M *= self.lambda_forget
        self.V *= self.lambda_forget
        self.N += gamma
        self.M += gamma * E_t
        self.V += gamma * E_t2
        self._m_step()
        self.ll += np.log(1.0 - self.cdf(T) + 1e-300)
        self.n += 1
    
    def update_increment(self, T_old, T_new):
        # "alive in [T_old, T_new]" — analogous to update_censored but on the interval
        ...
    
    def _truncated_moments(self, a_lim, b_lim):
        # for each component k, compute E[t | t in [a_lim, b_lim], k] etc.
        # via fixed_quad of t * pdf_k(t) / (CDF_k(b_lim) - CDF_k(a_lim))
        ...
    
    def _m_step(self):
        total = self.N.sum()
        self.w = self.N / total
        self.mu = np.clip(self.M / self.N, 1e-3, 1 - 1e-3)
        var = self.V / self.N - self.mu ** 2
        var = np.maximum(var, 1e-6)
        self.kappa = np.maximum(2.0, self.mu * (1 - self.mu) / var - 1)


class MixtureEnsemble:
    def __init__(self, K_max=10, criterion='aic', lambda_forget=1.0):
        self.models = [BetaMixture(K, lambda_forget) for K in range(1, K_max + 1)]
        self.criterion = criterion
    
    def update_death(self, t):
        for m in self.models:
            m.update_death(t)
    
    def update_censored(self, T):
        for m in self.models:
            m.update_censored(T)
    
    def weights(self):
        scores = []
        for K, m in enumerate(self.models, start=1):
            d = 3 * K - 1
            if self.criterion == 'aic':
                scores.append(-2 * m.ll + 2 * d)
            else:  # bic
                scores.append(-2 * m.ll + d * np.log(max(m.n, 1)))
        scores = np.array(scores)
        scores -= scores.min()
        unn = np.exp(-scores / 2)
        return unn / unn.sum()
    
    def density(self, t):
        w = self.weights()
        return sum(wk * m.density(t) for wk, m in zip(w, self.models))
    
    def cdf(self, t):
        w = self.weights()
        return sum(wk * m.cdf(t) for wk, m in zip(w, self.models))
    
    def cumulative_hazard(self, t):
        return -np.log(1.0 - self.cdf(t) + 1e-300)
```

## Implementation notes

### Libraries

- `numpy` for arrays
- `scipy.stats.beta` for beta PDF
- `scipy.special.betainc` for the CDF (regularized incomplete beta)
- `scipy.integrate.fixed_quad` for truncated moment integrals (32 points is fine)

PyMC is **not needed** for this approach. PyMC would be appropriate for a fully Bayesian alternative — proper marginal likelihoods via MCMC or VI, full posteriors over parameters — but it's far too slow for streaming updates at 60 fps. Consider PyMC only for occasional offline validation runs.

### Numerical stability

- All likelihood operations in log space
- Clamp `κ_k ≥ 2` to prevent narrow-collapse pathologies
- Clamp `μ_k ∈ [1e-3, 1 - 1e-3]`
- Use `log(x + 1e-300)` to guard against zero-density evaluations
- For weight computation, always shift scores by the minimum before exponentiating

### Performance targets

- Per-update cost: `O(K_max² )` ≈ sub-millisecond for `K_max = 10`
- Per-frame redraw (200 grid points): a few milliseconds
- Total at 60 fps (16 ms budget): comfortable

### When PyMC would actually help

Switch to PyMC (or Stan, NumPyro) if you need:
- Full posterior over parameters, not point estimates
- Proper marginal likelihoods rather than AIC/BIC approximations
- Hierarchical structure (e.g. multiple groups of trials)
- WAIC or LOO for model comparison (better-founded than AIC/BIC for mixtures)

Expect inference to take seconds-to-minutes per fit, not milliseconds.

## Testing

### Synthetic data

Generate data from a known beta mixture, verify recovery:

1. **Single component** (`K_true = 1`): verify `K_eff ≈ 1` after sufficient data.
2. **Well-separated modes** (`K_true = 3`, distinct peaks): verify correct K wins, parameters recovered.
3. **Close modes** (`K_true = 3`, overlapping): harder; verify graceful behavior.
4. **Censored data**: generate true deaths, then apply censoring at various `T`. Verify hazard function is recovered correctly even though raw density is biased toward earlier times.
5. **Status updates**: simulate trials reporting alive multiple times before dying. Verify increment-based updates and replace-based updates produce the same final fit.

### Diagnostics

Plot over the data stream:
- Model weights `w_K` over time (should stabilize)
- `K_eff` over time
- Log-likelihood per model
- Density and cumulative hazard curves
