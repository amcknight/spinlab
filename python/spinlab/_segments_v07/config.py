"""Centralized model / inference constants.

All values that aren't derived from data live here, so they can be inspected and
tweaked in one place. Nothing in this file is a fudge factor; every constant has
a documented meaning.
"""
import numpy as np


# Game / segment constants
# ------------------------
# Time spent respawning after a death. Subtracted from observed wall-clock to
# recover the in-attempt death time tau.
RESPAWN_MS = 3500.0


# Priors (in log-space; the original-scale param is exp(theta_i) which is Lognormal)
# ----------------------------------------------------------------------------------
# Each prior is Lognormal(median=M, sigma=S), equivalent to Normal(log M, S) on
# the log-parameter. See segment_model.md "Prior specifications".

PRIOR_LOG_BPT_MEAN = np.log(25000.0)   # bpt prior median (ms) — "game-typical-time"
PRIOR_LOG_BPT_SD = 1.5                 # very wide

PRIOR_LOG_SLOP_FRAC_MEAN = np.log(0.1) # slop_frac prior median = 10% of bpt
PRIOR_LOG_SLOP_FRAC_SD = 1.5

PRIOR_LOG_SLOP_SPREAD_MEAN = np.log(0.2)  # CV of clean-time slop, prior median 20%
PRIOR_LOG_SLOP_SPREAD_SD = 1.0

PRIOR_LOG_HAZ_MEAN = np.log(1.0)       # hazard rate per bin, prior median 1.0
PRIOR_LOG_HAZ_SD = 1.5


# Quadrature
# ----------
# Number of Simpson's-rule points across the support of clean_dist when
# computing the died-attempt likelihood integral. The integrand is single-peaked
# near T = bpt + mean_slop with width ~ std_slop, so a small fixed grid is
# both faster and more predictable than adaptive quadrature.
QUAD_POINTS = 64
# Upper limit of integration, measured in std_slop units above bpt+mean_slop.
# 8 sigma puts essentially all of clean_dist's mass below the cutoff.
QUAD_UPPER_SIGMAS = 8.0


# Optimizer (Nelder-Mead for MAP)
# -------------------------------
# Nelder-Mead is gradient-free and tolerant of regions where the likelihood is
# undefined; L-BFGS-B fails here because its numerical gradient hits -inf.

NM_XATOL = 1e-3       # absolute tolerance on parameter values (log-space)
NM_FATOL = 1e-4       # absolute tolerance on objective value (negative log post)
NM_MAXITER = 2000     # hard cap; warm-started fits converge in ~50-200 iters

# Per-axis size of the initial simplex (log-space). When warm-starting from a
# previous MAP, scipy's default initial simplex is built from the magnitude of
# theta_init, which can be tiny in log-space (e.g. log(slop_frac) ~ -2.5) and
# collapse the simplex. A fixed log-space delta keeps each vertex a meaningful
# distance from the others (a delta of 0.3 corresponds to a ~35% change on the
# original scale).
NM_SIMPLEX_DELTA = 0.3

# Finite-difference step (log-space) for the end-of-session Hessian. Small enough
# to capture local curvature, large enough that f(theta+eps)-f(theta) isn't
# dominated by Nelder-Mead's NM_FATOL=1e-4 convergence noise.
HESSIAN_FD_EPS = 1e-2

# Number of samples drawn from the Laplace posterior for plotting bands and
# haz_k marginal histograms.
LAPLACE_N_SAMPLES = 2000

# When log_posterior returns -inf (e.g. bpt >= min(survived T) so the survived
# density is 0), we return this finite penalty instead. The simplex sees "very
# bad" rather than "undefined" and steers away. Large enough to dominate
# log-likelihoods (O(100) for 20-30 attempts), small enough to avoid overflow.
NLL_PENALTY = 1e10

# Hard constraint: bpt must be strictly less than min observed survived T.
# This is structural (T_clean = bpt + nonneg), not a fudge. We back the upper
# bound off the constraint by this amount in log-space so the optimizer cannot
# sit exactly at the boundary (where clean_dist_pdf(min_survived) is 0 and the
# likelihood degenerates to the penalty).
BPT_UPPER_BOUND_LOG_BACKOFF = 1e-3
