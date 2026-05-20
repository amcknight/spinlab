"""Survival/hazard model interface and implementations.

A HazardModel owns the parameterization of the hazard function h(s) on s in [0,1]
and the priors on those parameters. Everything outside this module treats the
hazard as a black box: it exposes a parameter count, an `unpack` from a slice of
the theta vector to internal params, and methods to evaluate h(s) and the
cumulative hazard H(s) = integral_0^s h(u) du.

To plug in a new survival model (e.g. a Beta mixture, monotone spline, ...):
    1. Subclass HazardModel.
    2. Set n_params and implement unpack / h / cum / log_prior /
       initial_theta_slice.
    3. Build it at the entry-point in run.py (or wherever) and pass it through.
The likelihood, MAP fit, Laplace covariance, and evidence code don't change.
"""
import math

import numpy as np
from scipy.stats import beta as beta_dist, norm
from scipy.special import betainc

import config


class HazardModel:
    """Interface. Subclasses must set n_params and override the methods below."""
    n_params: int

    def unpack(self, theta_slice):
        """log-space slice of theta -> internal params (model's own choice)."""
        raise NotImplementedError

    def h(self, s, params):
        """Hazard rate at s in [0,1]. Vectorized over s."""
        raise NotImplementedError

    def cum(self, s, params):
        """Cumulative hazard from 0 to s. Vectorized over s."""
        raise NotImplementedError

    def log_prior(self, theta_slice):
        """Log prior on the log-space slice of theta belonging to this model."""
        raise NotImplementedError

    def initial_theta_slice(self):
        """Length-n_params log-space initial point (typically the prior means)."""
        raise NotImplementedError

    def log_evidence_correction(self):
        """Additive constant to add to the Laplace log-evidence for this model.

        Default 0. Mixture models that admit label-switching modes override this
        to add log(K!) so the Laplace approximation (which captures one of K!
        equivalent modes) is multiplied by the correct number of permutations.
        """
        return 0.0


class PiecewiseConstantHazard(HazardModel):
    """Original model: K bins on [0,1], one hazard rate per bin.

    theta layout (within this model's slice): [log(haz_1), ..., log(haz_K)].
    """

    def __init__(self, K, bin_edges):
        if K < 1:
            raise ValueError(f"K must be >= 1, got {K}")
        if len(bin_edges) != K + 1:
            raise ValueError(f"bin_edges must have length K+1={K+1}, got {len(bin_edges)}")
        self.K = K
        self.bin_edges = np.asarray(bin_edges, dtype=float)
        self.n_params = K

    def unpack(self, theta_slice):
        return np.exp(np.asarray(theta_slice, dtype=float))

    def h(self, s, haz):
        s = np.asarray(s, dtype=float)
        idx = np.searchsorted(self.bin_edges[1:], s, side='left')
        idx = np.clip(idx, 0, self.K - 1)
        out = np.asarray(haz)[idx]
        out = np.where((s < 0.0) | (s > 1.0), 0.0, out)
        return out if out.ndim else float(out)

    def cum(self, s, haz):
        s = np.asarray(s, dtype=float)
        s_c = np.clip(s, 0.0, 1.0)
        total = np.zeros_like(s_c)
        for k in range(self.K):
            lo, hi = self.bin_edges[k], self.bin_edges[k + 1]
            total = total + haz[k] * np.maximum(0.0, np.minimum(s_c, hi) - lo)
        return total if total.ndim else float(total)

    def log_prior(self, theta_slice):
        return float(norm.logpdf(
            theta_slice, config.PRIOR_LOG_HAZ_MEAN, config.PRIOR_LOG_HAZ_SD
        ).sum())

    def initial_theta_slice(self):
        return np.full(self.K, config.PRIOR_LOG_HAZ_MEAN)


class BetaMixtureHazard(HazardModel):
    """Death density on [0,1] as a scaled mixture of K beta components.

    f(s)   = alpha * sum_k w_k * Beta(s | a_k, b_k)        (scaled density)
    F(s)   = alpha * sum_k w_k * I_s(a_k, b_k)             (CDF; F(1) = alpha)
    S(s)   = 1 - F(s)                                      (survival; S(1) = 1-alpha)
    h(s)   = f(s) / S(s)                                   (hazard rate)
    cum(s) = -log S(s)                                     (cumulative hazard)

    Each component is parameterized as (mu_k, kappa_k) with a_k = mu_k * kappa_k
    and b_k = (1 - mu_k) * kappa_k. The scale alpha in (0, 1) is the overall
    P(die in an attempt); 1 - alpha is the probability of surviving s=1.

    theta_slice layout (length 3*K, all log/logit unconstrained):
        [0]            : logit(alpha)
        [1 : 1+K]      : logit(mu_k)         for k = 1..K
        [1+K : 1+2K]   : log(kappa_k)        for k = 1..K
        [1+2K : 3K]    : eta_k               for k = 1..K-1   (softmax logits;
                                                                eta_K := 0 reference)

    Label switching: components are interchangeable, so the posterior has up
    to K! equivalent modes in the free parameterization. We DO NOT add log(K!)
    to the Laplace evidence because that correction is only exact when the
    modes are well-separated; with sparse data, components routinely collapse
    to identical params (single mode), and the K! correction then over-credits
    the model. Concretely, at K=3 on 1 data point the components all snap to
    μ=0.59 and the raw evidence is correctly worse than K=1, but +log(3!)
    flips the comparison. Raw Laplace evidence is the safer default; the worst
    case is an under-credit of well-separated mixtures by at most log(K!) nats
    (≤5 for K≤5), which is small relative to the per-attempt evidence
    contribution for any sizeable dataset.
    """

    # Prior hyperparameters for the mixture parts (set here, not in config.py,
    # to keep the beta-mixture model self-contained while we evaluate it).
    PRIOR_LOGIT_ALPHA_MEAN = 0.0           # alpha prior median = 0.5
    PRIOR_LOGIT_ALPHA_SD = 1.0             # 68% in (~0.27, ~0.73)
    PRIOR_LOGIT_MU_MEAN = 0.0              # mu_k prior median = 0.5
    PRIOR_LOGIT_MU_SD = 1.5                # 68% in (~0.18, ~0.82)
    PRIOR_LOG_KAPPA_MEAN = math.log(15.0)  # kappa_k prior median = 15
    PRIOR_LOG_KAPPA_SD = 1.0                # 68% in (5.5, 41); 95% lower
                                            # bound ~2 keeps mass out of the
                                            # boundary-spike regime (kappa<1)
                                            # which would produce divergent
                                            # Beta densities at s=0 or s=1.
    PRIOR_ETA_MEAN = 0.0                   # equal-weight reference
    PRIOR_ETA_SD = 1.5                     # broad; allows w ratios e^(+/-1.5)

    def __init__(self, K):
        if K < 1:
            raise ValueError(f"K must be >= 1, got {K}")
        self.K = K
        self.n_params = 3 * K

    def unpack(self, theta_slice):
        """theta_slice -> (alpha, mu[K], kappa[K], w[K])."""
        K = self.K
        t = np.asarray(theta_slice, dtype=float)
        alpha = float(_sigmoid(t[0]))
        mu = _sigmoid(t[1:1 + K])
        kappa = np.exp(t[1 + K:1 + 2 * K])
        if K == 1:
            w = np.array([1.0])
        else:
            eta_free = t[1 + 2 * K:3 * K]                # length K-1
            eta = np.concatenate([eta_free, [0.0]])      # eta_K = 0 reference
            eta = eta - eta.max()                        # numerical stability
            ex = np.exp(eta)
            w = ex / ex.sum()
        return alpha, mu, kappa, w

    @staticmethod
    def _ab(mu, kappa):
        return mu * kappa, (1.0 - mu) * kappa

    def _F(self, s, alpha, mu, kappa, w):
        """Scaled CDF F(s) = alpha * sum_k w_k * I_s(a_k, b_k). Vectorized over s."""
        a, b = self._ab(mu, kappa)
        s = np.asarray(s, dtype=float)
        s_c = np.clip(s, 0.0, 1.0)
        if s.ndim == 0:
            comp = betainc(a, b, float(s_c))             # shape (K,)
            return float(alpha * np.sum(w * comp))
        comp = betainc(a[None, :], b[None, :], s_c[:, None])  # (n, K)
        return alpha * np.sum(w[None, :] * comp, axis=1)

    def _f(self, s, alpha, mu, kappa, w):
        """Scaled density f(s). Vectorized over s."""
        a, b = self._ab(mu, kappa)
        s = np.asarray(s, dtype=float)
        if s.ndim == 0:
            comp = beta_dist.pdf(float(s), a, b)
            return float(alpha * np.sum(w * comp))
        comp = beta_dist.pdf(s[:, None], a[None, :], b[None, :])
        return alpha * np.sum(w[None, :] * comp, axis=1)

    def h(self, s, params):
        alpha, mu, kappa, w = params
        f_s = self._f(s, alpha, mu, kappa, w)
        F_s = self._F(s, alpha, mu, kappa, w)
        S_s = 1.0 - F_s
        out = f_s / S_s
        s_arr = np.asarray(s, dtype=float)
        if s_arr.ndim:
            out = np.where((s_arr < 0.0) | (s_arr > 1.0), 0.0, out)
        elif s_arr < 0.0 or s_arr > 1.0:
            out = 0.0
        return out

    def cum(self, s, params):
        alpha, mu, kappa, w = params
        F_s = self._F(s, alpha, mu, kappa, w)
        return -np.log(1.0 - F_s)

    def log_prior(self, theta_slice):
        K = self.K
        t = np.asarray(theta_slice, dtype=float)
        lp = float(norm.logpdf(t[0], self.PRIOR_LOGIT_ALPHA_MEAN,
                               self.PRIOR_LOGIT_ALPHA_SD))
        lp += float(norm.logpdf(t[1:1 + K], self.PRIOR_LOGIT_MU_MEAN,
                                self.PRIOR_LOGIT_MU_SD).sum())
        lp += float(norm.logpdf(t[1 + K:1 + 2 * K], self.PRIOR_LOG_KAPPA_MEAN,
                                self.PRIOR_LOG_KAPPA_SD).sum())
        if K > 1:
            lp += float(norm.logpdf(t[1 + 2 * K:3 * K], self.PRIOR_ETA_MEAN,
                                    self.PRIOR_ETA_SD).sum())
        return lp

    def initial_theta_slice(self):
        """Spread mu evenly across (0,1), all kappa at prior median, equal weights."""
        K = self.K
        slice_ = np.empty(3 * K)
        slice_[0] = self.PRIOR_LOGIT_ALPHA_MEAN
        if K == 1:
            mu_init = np.array([0.5])
        else:
            mu_init = np.linspace(0.15, 0.85, K)
        slice_[1:1 + K] = np.log(mu_init / (1.0 - mu_init))   # logit
        slice_[1 + K:1 + 2 * K] = self.PRIOR_LOG_KAPPA_MEAN
        if K > 1:
            slice_[1 + 2 * K:3 * K] = 0.0
        return slice_

    def log_evidence_correction(self):
        """No correction. log(K!) would be exact only with well-separated
        components; collapsed modes (common at low N) get over-credited. See
        class docstring."""
        return 0.0


def _sigmoid(x):
    return 1.0 / (1.0 + np.exp(-x))
