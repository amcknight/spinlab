"""End-of-data comparison across beta-mixture K values (and vs piecewise).

**v2-scope — parked.** Beta-K mixtures are not in the v1 API surface (see
V1_ESSENCE.md). This script is preserved as the starting point for any v2
beta2 work inside SpinLab. v1 porters can ignore it. Default fixtures
`test_data.tsv` (real data N=22) and `test_synth.tsv` (K=3-truth synth)
at the repo root exist solely to support this script.

Fits each model once on the full dataset, computes Laplace log-evidence with
the log(K!) correction for mixtures, then renders a figure that shows:

    - death density f(s) on [0, 1]
    - survival S(s) on [0, 1]
    - hazard h(s) on [0, 1]
    - survival S(t) in wall-clock seconds
    - per-component decomposition for the highest-weight beta K
    - log-evidence bars across K

Each curve panel shows individual-K point estimates (thin) and the K-blended
posterior with 50% and 90% bands drawn from Laplace samples (model-averaged).

Usage:
    python beta_compare.py                                # default: K=1..5
    python beta_compare.py --data test_synth.tsv --K_values 1 2 3
    python beta_compare.py --piecewise_K 0                # skip piecewise ref
"""
import argparse
import csv
import math
import time

import numpy as np
import matplotlib.pyplot as plt

import binning
import config
from hazard import PiecewiseConstantHazard, BetaMixtureHazard
from model import unpack, lognormal_params, slop_moments, m_clear
from inference import find_map, laplace_cov, log_evidence_laplace


DEFAULT_DATA = 'test_data.tsv'
DEFAULT_SAVE = 'pic/beta_compare.png'
S_GRID = np.linspace(1e-4, 1.0 - 1e-4, 400)
N_BAND_SAMPLES = 400              # Laplace samples for posterior bands (total)
BAND_SEED = 1


# -----------------------
# data + fitting
# -----------------------

def load_data(path):
    with open(path) as f:
        reader = csv.DictReader(f, delimiter='\t')
        return [(r['outcome'].strip(), float(r['time_ms'])) for r in reader]


def fit_one(data, hazard, label):
    t0 = time.perf_counter()
    theta_map, _ = find_map(data, hazard)
    map_dt = time.perf_counter() - t0

    t0 = time.perf_counter()
    cov, ok = laplace_cov(theta_map, data, hazard)
    hess_dt = time.perf_counter() - t0
    raw_ev = log_evidence_laplace(theta_map, cov, data, hazard)
    correction = hazard.log_evidence_correction()
    log_ev = raw_ev + correction if math.isfinite(raw_ev) else float('-inf')

    print(f"  {label:>18}: log_ev = {log_ev:>8.2f}  "
          f"(raw {raw_ev:>8.2f} + corr {correction:+.2f})  "
          f"map {map_dt:>5.1f}s  hess {hess_dt:>5.1f}s  pd={ok}")
    return {
        'label': label, 'hazard': hazard, 'theta_map': theta_map,
        'cov': cov, 'log_ev': log_ev, 'raw_ev': raw_ev,
        'correction': correction, 'pd': ok,
    }


# -----------------------
# curve evaluators
# -----------------------

def density_on_s(hazard, theta):
    """Total death density f(s) = h(s) * S(s). Works for any HazardModel."""
    _, _, _, hp = unpack(theta, hazard)
    return hazard.h(S_GRID, hp) * np.exp(-hazard.cum(S_GRID, hp))


def survival_on_s(hazard, theta):
    _, _, _, hp = unpack(theta, hazard)
    return np.exp(-hazard.cum(S_GRID, hp))


def hazard_on_s(hazard, theta):
    _, _, _, hp = unpack(theta, hazard)
    return hazard.h(S_GRID, hp)


def mean_clean_ms(hazard, theta):
    """E[T_clean] = bpt + E[slop] at the given parameter vector."""
    bpt, sf, ssp, _ = unpack(theta, hazard)
    mu_log, sigma_log = lognormal_params(sf, ssp, bpt)
    mean_slop, _ = slop_moments(mu_log, sigma_log)
    return bpt + mean_slop


def s_star_point_estimates(data, theta, hazard):
    mc = mean_clean_ms(hazard, theta)
    return np.array([(t - config.RESPAWN_MS) / mc
                     for o, t in data if o == 'died'])


# -----------------------
# model averaging
# -----------------------

def model_weights(fits):
    evs = np.array([f['log_ev'] for f in fits])
    evs = evs - np.max(evs)
    w = np.exp(evs)
    return w / w.sum()


def blended_samples(beta_fits, weights, n_total, seed=BAND_SEED):
    """Draw n_total samples from the K-weighted mixture of Laplace posteriors.

    Returns (sample_thetas, fit_idx_per_sample). Each sample is a theta vector
    matching the parameter structure of beta_fits[fit_idx]. Samples from fits
    whose Hessian failed (cov is None) are skipped.
    """
    rng = np.random.default_rng(seed)
    counts = rng.multinomial(n_total, weights)
    sample_arrs, idx_arrs = [], []
    for i, (fit, n_i) in enumerate(zip(beta_fits, counts)):
        if n_i == 0 or fit['cov'] is None:
            continue
        s = rng.multivariate_normal(fit['theta_map'], fit['cov'], size=n_i)
        sample_arrs.append(s)
        idx_arrs.append(np.full(n_i, i))
    if not sample_arrs:
        return None, None
    # samples have different dims per K, so keep them in a list-of-rows form:
    flat_samples = [row for arr in sample_arrs for row in arr]
    flat_idx = np.concatenate(idx_arrs)
    return flat_samples, flat_idx


def blended_curves(beta_fits, sample_rows, idx_per_sample, curve_fn):
    """Stack curves over a list of (sample, fit_idx) pairs.

    curve_fn(hazard, theta) -> array of length len(S_GRID).
    Returns shape (n_samples, len(S_GRID)).
    """
    n = len(sample_rows)
    out = np.empty((n, len(S_GRID)))
    for i, theta in enumerate(sample_rows):
        fit = beta_fits[idx_per_sample[i]]
        out[i] = curve_fn(fit['hazard'], theta)
    return out


def quantile_bands(curves):
    """50% and 90% bands plus median. Returns (lo90, lo50, med, hi50, hi90)."""
    lo90, lo50, med, hi50, hi90 = np.quantile(
        curves, [0.05, 0.25, 0.5, 0.75, 0.95], axis=0
    )
    return lo90, lo50, med, hi50, hi90


# -----------------------
# plot helpers
# -----------------------

def _draw_curve_panel(ax, x, curves_per_fit, point_labels, blend_x, blend_curves,
                       extra_curves=None, hist=None, xlabel='', ylabel='',
                       title='', ylim=None, legend_loc='best'):
    """Common pattern: faint point-estimate lines per K, thick blended median
    with 50% / 90% bands, optional extra reference curves (e.g. piecewise) as
    dashed lines, optional histogram, axis labels."""
    # faint individual K's
    for (xs, ys), lab in zip(curves_per_fit, point_labels):
        ax.plot(xs, ys, lw=1.0, alpha=0.45, label=lab)
    # extras (piecewise reference, truth, etc.)
    if extra_curves:
        for (xs, ys, color, ls, label) in extra_curves:
            ax.plot(xs, ys, lw=1.5, color=color, ls=ls, label=label)
    # bands + median for blended
    if blend_curves is not None:
        lo90, lo50, med, hi50, hi90 = quantile_bands(blend_curves)
        ax.fill_between(blend_x, lo90, hi90, color='C3', alpha=0.15,
                        label='90% CI (blended)')
        ax.fill_between(blend_x, lo50, hi50, color='C3', alpha=0.30,
                        label='50% CI (blended)')
        ax.plot(blend_x, med, lw=2.2, color='C3', label='blended median')
    # histogram
    if hist is not None:
        h_data, h_kwargs = hist
        ax.hist(h_data, **h_kwargs)
    if ylim is not None:
        ax.set_ylim(*ylim)
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.legend(fontsize=8, loc=legend_loc)
    ax.grid(alpha=0.3)


def _decomposition_for_K(fit, ax, samples_for_this_K=None):
    """For a single beta-mixture fit, plot each weighted component w_k * f_k(s)
    and the total f(s). Helps see which bumps the mixture discovered."""
    hazard = fit['hazard']
    if not isinstance(hazard, BetaMixtureHazard):
        ax.text(0.5, 0.5, 'no decomposition for non-mixture model',
                ha='center', va='center', transform=ax.transAxes)
        return
    K = hazard.K
    alpha, mu, kappa, w = unpack(fit['theta_map'], hazard)[3]
    a, b = mu * kappa, (1.0 - mu) * kappa
    from scipy.stats import beta as beta_dist
    total = np.zeros_like(S_GRID)
    for k in range(K):
        comp = alpha * w[k] * beta_dist.pdf(S_GRID, a[k], b[k])
        total += comp
        ax.plot(S_GRID, comp, lw=1.5, color=f'C{k+2}',
                label=f'w_{k+1}={w[k]:.2f}, mu={mu[k]:.2f}, kap={kappa[k]:.1f}')
    ax.plot(S_GRID, total, lw=2.5, color='k',
            label=f'total (alpha={alpha:.2f})')
    ax.set_xlabel('s')
    ax.set_ylabel('density (weighted)')
    ax.set_title(f'Decomposition of {fit["label"]}')
    ax.legend(fontsize=8)
    ax.grid(alpha=0.3)


# -----------------------
# main
# -----------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--data', default=DEFAULT_DATA,
                    help='path to tsv with outcome / time_ms columns')
    ap.add_argument('--K_values', type=int, nargs='+', default=[1, 2, 3, 4, 5])
    ap.add_argument('--piecewise_K', type=int, default=3,
                    help='K for piecewise reference (set to 0 to skip)')
    ap.add_argument('--save', default=DEFAULT_SAVE)
    args = ap.parse_args()

    data = load_data(args.data)
    n_died = sum(1 for o, _ in data if o == 'died')
    print(f"Loaded {len(data)} attempts from {args.data} "
          f"({n_died} died, {len(data) - n_died} survived).")

    beta_fits = []
    for K in args.K_values:
        beta_fits.append(fit_one(data, BetaMixtureHazard(K), f'beta K={K}'))
    piece_fit = None
    if args.piecewise_K > 0:
        pK = args.piecewise_K
        piece_fit = fit_one(
            data, PiecewiseConstantHazard(pK, binning.uniform(pK)),
            f'piecewise K={pK}',
        )

    # Reference fit chooses mean_clean for the wall-clock x-axis and the s*
    # histogram. Piecewise if available, else the best-weighted beta.
    ref_fit = piece_fit if piece_fit is not None else beta_fits[0]
    ref_mc_ms = mean_clean_ms(ref_fit['hazard'], ref_fit['theta_map'])
    ref_mc_s = ref_mc_ms / 1000.0
    s_stars = s_star_point_estimates(data, ref_fit['theta_map'], ref_fit['hazard'])

    weights = model_weights(beta_fits)
    print("\nLaplace-blend weights across beta variants:")
    for f, w in zip(beta_fits, weights):
        print(f"  {f['label']:>18}  w = {w:.3f}")
    k_array = np.array([f['hazard'].K for f in beta_fits])
    k_eff = float(np.sum(weights * k_array))
    print(f"  K_eff = {k_eff:.2f}")

    # Expected total wall-clock time to first survival (M_clear) per model.
    # This depends on bpt, slop, alpha (P die), and where deaths cluster --
    # so it's a useful headline summary that exposes how much the structural
    # choice of model matters in user-facing units.
    print("\nM_clear (expected wall-clock seconds to first clear from scratch):")
    m_per_K = []
    for f in beta_fits:
        m = m_clear(f['theta_map'], f['hazard']) / 1000.0
        m_per_K.append(m)
        print(f"  {f['label']:>18}  M_clear = {m:>7.2f} s")
    if piece_fit is not None:
        m_piece = m_clear(piece_fit['theta_map'], piece_fit['hazard']) / 1000.0
        print(f"  {piece_fit['label']:>18}  M_clear = {m_piece:>7.2f} s")
    # Blended M_clear: zero-weight NaN contributions are ignored (a
    # mis-specified model -- e.g. K=1 on data with 3 distinct peaks -- can
    # produce NaN due to numerical integration over a poorly-fit density;
    # since it carries no weight in the blend, treat its M_clear as missing).
    m_arr = np.array(m_per_K)
    finite_mask = np.isfinite(m_arr)
    if not finite_mask.all():
        print(f"  (note: {(~finite_mask).sum()} model(s) gave non-finite "
              f"M_clear, excluded from blend)")
    if finite_mask.any():
        w_used = weights[finite_mask] / weights[finite_mask].sum()
        m_blend = float(np.sum(w_used * m_arr[finite_mask]))
        print(f"  {'beta BLENDED':>18}  M_clear = {m_blend:>7.2f} s  "
              f"(weighted by Laplace evidence)")

    # Blended sample set (used for posterior bands on every curve panel)
    print(f"\nDrawing {N_BAND_SAMPLES} blended Laplace samples for bands...")
    sample_rows, idx_per_sample = blended_samples(beta_fits, weights, N_BAND_SAMPLES)
    if sample_rows is None:
        print("  No usable Laplace covariances; bands will be skipped.")
        f_curves = S_curves = h_curves = Swc_curves = None
    else:
        f_curves = blended_curves(beta_fits, sample_rows, idx_per_sample,
                                  density_on_s)
        S_curves = blended_curves(beta_fits, sample_rows, idx_per_sample,
                                  survival_on_s)
        h_curves = blended_curves(beta_fits, sample_rows, idx_per_sample,
                                  hazard_on_s)
        # wall-clock S(t): each sample's S(s) plotted on its own time axis would
        # be inconsistent; we use a shared x-axis = S_GRID * ref_mc_ms.
        Swc_curves = S_curves

    # Per-K point-estimate curves
    f_per_K = [(S_GRID, density_on_s(f['hazard'], f['theta_map']))
               for f in beta_fits]
    S_per_K = [(S_GRID, survival_on_s(f['hazard'], f['theta_map']))
               for f in beta_fits]
    h_per_K = [(S_GRID, hazard_on_s(f['hazard'], f['theta_map']))
               for f in beta_fits]
    t_axis_s = S_GRID * ref_mc_s
    Swc_per_K = [(t_axis_s, survival_on_s(f['hazard'], f['theta_map']))
                 for f in beta_fits]
    labels = [f['label'] for f in beta_fits]

    # Piecewise reference curves
    piece_extra_f = piece_extra_S = piece_extra_h = piece_extra_Swc = None
    if piece_fit is not None:
        plab = piece_fit['label']
        piece_extra_f = [(S_GRID, density_on_s(piece_fit['hazard'], piece_fit['theta_map']),
                          'k', '--', plab)]
        piece_extra_S = [(S_GRID, survival_on_s(piece_fit['hazard'], piece_fit['theta_map']),
                          'k', '--', plab)]
        piece_extra_h = [(S_GRID, hazard_on_s(piece_fit['hazard'], piece_fit['theta_map']),
                          'k', '--', plab)]
        piece_extra_Swc = [(t_axis_s,
                            survival_on_s(piece_fit['hazard'], piece_fit['theta_map']),
                            'k', '--', plab)]

    # ------ figure ------
    fig, axes = plt.subplots(3, 2, figsize=(13, 13))
    ax_f, ax_S, ax_h, ax_Swc, ax_dec, ax_ev = axes.flat

    # (0,0) f(s) on [0,1]
    _draw_curve_panel(
        ax_f, S_GRID, f_per_K, labels, S_GRID, f_curves,
        extra_curves=piece_extra_f,
        hist=(s_stars,
              dict(bins=20, range=(0, 1), density=True, color='gray',
                   alpha=0.25, label=f's* hist (n={len(s_stars)})')),
        xlabel='s (fractional position)', ylabel='density',
        title='Death density f(s)',
        legend_loc='upper right',
    )

    # (0,1) S(s) on [0,1]
    _draw_curve_panel(
        ax_S, S_GRID, S_per_K, labels, S_GRID, S_curves,
        extra_curves=piece_extra_S,
        xlabel='s', ylabel='P(alive at s)',
        title='Survival in s-coords',
        ylim=(0, 1.02), legend_loc='upper right',
    )

    # (1,0) h(s) on [0,1]
    _draw_curve_panel(
        ax_h, S_GRID, h_per_K, labels, S_GRID, h_curves,
        extra_curves=piece_extra_h,
        xlabel='s', ylabel='hazard',
        title='Hazard h(s)',
        legend_loc='upper right',
    )

    # (1,1) S(t) wall-clock
    death_taus_s = [(t - config.RESPAWN_MS) / 1000.0
                    for o, t in data if o == 'died']
    _draw_curve_panel(
        ax_Swc, t_axis_s, Swc_per_K, labels, t_axis_s, Swc_curves,
        extra_curves=piece_extra_Swc,
        xlabel=f'time into attempt (s); axis uses {ref_fit["label"]} mean_clean = {ref_mc_s:.1f}s',
        ylabel='P(still alive)',
        title='Survival in wall-clock time',
        ylim=(0, 1.02), legend_loc='upper right',
    )
    for tau in death_taus_s:
        ax_Swc.axvline(tau, color='k', alpha=0.15, lw=0.5)

    # (2,0) Per-component decomposition of the highest-weight beta fit
    focus_idx = int(np.argmax(weights))
    _decomposition_for_K(beta_fits[focus_idx], ax_dec)

    # (2,1) Evidence bars
    xs = np.arange(len(beta_fits))
    bar_colors = [f'C{i}' for i in range(len(beta_fits))]
    evs_arr = [f['log_ev'] for f in beta_fits]
    bars = ax_ev.bar(xs, evs_arr, color=bar_colors, alpha=0.7)
    for b, w in zip(bars, weights):
        ax_ev.text(b.get_x() + b.get_width() / 2, b.get_height(),
                   f'w={w:.2f}', ha='center', va='bottom', fontsize=9)
    if piece_fit is not None:
        ax_ev.axhline(piece_fit['log_ev'], color='k', ls='--', lw=1.0,
                      label=f"{piece_fit['label']}  ev={piece_fit['log_ev']:.1f}")
        ax_ev.legend(fontsize=8)
    ax_ev.set_xticks(xs)
    ax_ev.set_xticklabels(labels, rotation=0, fontsize=9)
    ax_ev.set_ylabel('Laplace log-evidence (with log K! corr)')
    ax_ev.set_title(f'Model evidence; K_eff = {k_eff:.2f}')
    ax_ev.grid(alpha=0.3, axis='y')

    fig.suptitle(f'Beta mixture sweep on {args.data}; N={len(data)}, '
                 f'{n_died} died, focus K = {beta_fits[focus_idx]["label"]}')
    fig.tight_layout()
    fig.savefig(args.save, dpi=110)
    print(f"\nSaved figure to {args.save}")


if __name__ == '__main__':
    main()
