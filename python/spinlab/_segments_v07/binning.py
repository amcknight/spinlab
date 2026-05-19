"""Bin-edge pickers for the piecewise-constant hazard model.

Each picker returns a numpy array of bin edges on [0, 1]:
    edges[0] = 0.0, edges[-1] = 1.0, strictly increasing, monotone.

K (the number of bins) equals len(edges) - 1. Some pickers take K as input
explicitly (uniform, quantile, kmeans_midpoints); others determine K from the
data plus a smoothness knob (kde_valleys). Bins narrower than MIN_BIN_WIDTH
are collapsed to avoid numerical degeneracy.

To swap pickers in a caller, just assign the chosen function to a local
variable and call it. There is no abstract interface beyond "(...) -> ndarray".
"""
import numpy as np

# Smallest bin width permitted; narrower bins are collapsed by `_finalize`.
MIN_BIN_WIDTH = 1e-4


def _finalize(interior_edges):
    """Wrap a list of interior edges with [0, 1] endpoints, enforce strict
    monotonicity and MIN_BIN_WIDTH spacing, and return as ndarray."""
    interior = sorted(float(e) for e in np.asarray(interior_edges).tolist())
    keep = []
    for e in interior:
        if not (0.0 < e < 1.0):
            continue
        if keep and e - keep[-1] < MIN_BIN_WIDTH:
            continue
        keep.append(e)
    return np.concatenate([[0.0], keep, [1.0]])


def uniform(K):
    """Equal-width bins on [0, 1]."""
    if K < 1:
        raise ValueError(f"K must be >= 1, got {K}")
    return np.linspace(0.0, 1.0, K + 1)


def quantile(s_stars, K):
    """Edges at the K-1 internal quantiles of observed s*.

    Warning: this puts edges *in* clusters (at the median of dense regions),
    splitting evidence across adjacent bins. Generally worse than uniform.
    Kept for sanity-check comparison.
    """
    if K <= 1 or len(s_stars) < 2:
        return uniform(K)
    qs = np.linspace(0.0, 1.0, K + 1)[1:-1]
    return _finalize(np.quantile(s_stars, qs))


def kde_valleys(s_stars, bandwidth=0.05, grid_points=500):
    """Edges at local minima of a Gaussian KDE of s*.

    K is determined by how many valleys the KDE has, which depends on the
    bandwidth. Smaller bandwidth → more (and noisier) valleys → larger K.
    """
    s_stars = np.asarray(s_stars, dtype=float)
    if len(s_stars) < 2:
        return uniform(1)
    grid = np.linspace(0.0, 1.0, grid_points)
    diffs = (grid[None, :] - s_stars[:, None]) / bandwidth
    kde = np.exp(-0.5 * diffs ** 2).sum(axis=0)
    valleys = [grid[i] for i in range(1, len(grid) - 1)
               if kde[i] < kde[i - 1] and kde[i] < kde[i + 1]]
    return _finalize(valleys) if valleys else uniform(1)


def _kmeans_1d(values, K, max_iters=100):
    values = np.sort(np.asarray(values, dtype=float))
    quantiles = np.linspace(0.5 / K, 1.0 - 0.5 / K, K)
    centers = np.quantile(values, quantiles)
    for _ in range(max_iters):
        assign = np.argmin(np.abs(values[:, None] - centers[None, :]), axis=1)
        new_centers = np.array([
            values[assign == k].mean() if np.any(assign == k) else centers[k]
            for k in range(K)
        ])
        new_centers = np.sort(new_centers)
        if np.allclose(new_centers, centers):
            break
        centers = new_centers
    return centers


def kmeans_midpoints(s_stars, K):
    """Edges at midpoints between consecutive 1D k-means cluster centers."""
    if K <= 1 or len(s_stars) < K:
        return uniform(K)
    centers = _kmeans_1d(np.asarray(s_stars, dtype=float), K)
    midpoints = (centers[:-1] + centers[1:]) / 2.0
    return _finalize(midpoints)


def manual(interior_edges):
    """Use explicitly given interior edges (excluding the 0 and 1 endpoints)."""
    return _finalize(interior_edges)
