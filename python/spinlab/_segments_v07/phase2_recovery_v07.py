"""Phase 2 recovery -- V07 parameterization (halflife form, with joint prior).

Generates data with KNOWN learning curves in (theta_inf, theta_1, halflife)
form, fits learning_model_v07, reports recovery quality, and gates on the V07
pass criteria (external_docs/learning_curves_session_findings.md):

    1. Prior 95% on M_clear at n=1 < 1 hour            (prior predictive)
    2. Asymptote MAP within 30% of truth at N=500       (sf_inf, ssp_inf,
                                                          alpha_inf, bpt)
    3. Cold-start MAP under 30 s at N=500
    4. No multi-seed stuck-at-init failures

Pass criteria 2-4 are computed across `--seeds` independent draws.

Usage:
    python phase2_recovery_v07.py                       # 3 seeds at N=500
    python phase2_recovery_v07.py --seeds 5 --ns 150 500
"""
import argparse
import time

import numpy as np
from scipy.optimize import minimize

import config
import learning_model_v07 as lm07
import model
from generate_synthetic_learning_v07 import TRUTH_DEFAULTS, generate, curve
from inference import _initial_simplex, _bounds


N_PRIOR_SAMPLES = 5000      # for the prior-predictive M_clear check
MAX_FIT_SECONDS_AT_N500 = 30.0
ASYMPTOTE_REL_TOL = 0.30


def truth_to_theta_v07(truth):
    return np.array([
        np.log(truth['bpt']),
        np.log(truth['sf_inf']),
        np.log(truth['ssp_inf']),
        np.log(truth['alpha_inf']),
        np.log(truth['sf_1']),
        np.log(truth['ssp_1']),
        np.log(truth['alpha_1']),
        np.log(truth['sf_halflife']),
        np.log(truth['ssp_halflife']),
        np.log(truth['alpha_halflife']),
    ])


def _nll_v07(theta, data, hazard):
    lp = lm07.log_posterior(theta, data, hazard)
    if not np.isfinite(lp):
        return config.NLL_PENALTY
    return -lp


def find_map_v07(data, hazard, theta_init=None):
    if theta_init is None:
        theta_init = lm07.initial_theta(hazard)
    theta_init = theta_init.copy()

    bounds = _bounds(data, len(theta_init))
    bpt_upper = bounds[0][1]
    if theta_init[0] >= bpt_upper:
        theta_init[0] = bpt_upper - config.NM_SIMPLEX_DELTA

    result = minimize(
        _nll_v07,
        theta_init,
        args=(data, hazard),
        method='Nelder-Mead',
        bounds=bounds,
        options={
            'xatol': config.NM_XATOL,
            'fatol': config.NM_FATOL,
            'maxiter': config.NM_MAXITER * 3,    # 10-d, give it room
            'adaptive': True,
            'initial_simplex': _initial_simplex(theta_init),
        },
    )
    return result.x, result


def prior_predictive_m_clear_at_1(hazard, n_samples=N_PRIOR_SAMPLES, seed=0):
    """Sample theta from the JOINT prior (uses lm07.sample_prior, which respects
    the joint P(halflife | A) tightening). Returns M_clear at n=1 in ms."""
    rng = np.random.default_rng(seed)
    samples = lm07.sample_prior(n_samples, rng=rng)
    out = np.empty(n_samples)
    for i, th in enumerate(samples):
        try:
            out[i] = lm07.m_clear_at_n(th, hazard, n=1)
        except (FloatingPointError, ValueError):
            out[i] = np.nan
    return out


def run_one(n_attempts, seed, truth, hazard):
    data = generate(n_attempts, truth, seed=seed)
    n_died = sum(1 for o, _ in data if o == 'died')

    theta_init = lm07.initial_theta(hazard)
    t0 = time.perf_counter()
    theta_map, result = find_map_v07(data, hazard)
    dt = time.perf_counter() - t0

    # "Stuck at init" -- MAP is within NM_SIMPLEX_DELTA of init on every axis,
    # meaning the optimizer didn't move. NM_XATOL=1e-3 is the inner tolerance;
    # SIMPLEX_DELTA=0.3 is the step size. If max-axis movement is below half
    # the simplex step, that's stuck behavior on a 10-d fit.
    moved = np.max(np.abs(theta_map - theta_init))
    stuck = moved < config.NM_SIMPLEX_DELTA / 2

    return {
        'n_attempts': n_attempts,
        'seed': seed,
        'n_died': n_died,
        'theta_map': theta_map,
        'dt': dt,
        'nit': result.nit,
        'converged': bool(result.success),
        'stuck': stuck,
        'max_axis_move': float(moved),
    }


def report_prior_predictive(hazard):
    print(f"=== Pass criterion 1: prior-predictive M_clear at n=1 ===")
    mc = prior_predictive_m_clear_at_1(hazard) / 1000.0   # to seconds
    finite = mc[np.isfinite(mc)]
    n_inf_or_nan = len(mc) - len(finite)
    pct = np.percentile(finite, [50, 75, 95, 97.5, 99])
    pct_hours = pct / 3600.0
    print(f"  samples: {len(mc)} ({n_inf_or_nan} non-finite dropped)")
    print(f"  percentile  50    75    95    97.5  99   (hours)")
    print(f"              {pct_hours[0]:.3f} {pct_hours[1]:.3f} {pct_hours[2]:.3f} "
          f"{pct_hours[3]:.3f} {pct_hours[4]:.3f}")
    upper_95 = pct_hours[3]   # 97.5th percentile = upper end of central 95%
    ok = upper_95 < 1.0
    print(f"  upper 95% of prior predictive M_clear(n=1): {upper_95:.3f} hours "
          f"(target < 1.0)  {'PASS' if ok else 'FAIL'}\n")
    return ok


def report_recovery(results, truth, hazard):
    theta_truth = truth_to_theta_v07(truth)
    asymp_idx = [lm07.IDX_LOG_BPT, lm07.IDX_LOG_SF_INF,
                 lm07.IDX_LOG_SSP_INF, lm07.IDX_LOG_ALPHA_INF]
    asymp_labels = ['bpt (ms)', 'sf_inf', 'ssp_inf', 'alpha_inf']

    ns = sorted({r['n_attempts'] for r in results})
    all_ok_30pct = True
    all_ok_speed = True
    all_ok_unstuck = True

    for n in ns:
        rs = [r for r in results if r['n_attempts'] == n]
        print(f"=== N={n} ({len(rs)} seeds) ===")
        mats = np.array([r['theta_map'] for r in rs])           # log-space
        mean_log = mats.mean(axis=0)
        std_log = mats.std(axis=0)

        print(f"  {'param':>14}  {'truth(nat)':>12}  {'MAP mean(nat)':>14}  "
              f"{'MAP std(log)':>13}  {'rel err':>9}")
        for ax, label in zip(asymp_idx, asymp_labels):
            truth_nat = float(np.exp(theta_truth[ax]))
            mean_nat = float(np.exp(mean_log[ax]))
            rel = (mean_nat - truth_nat) / truth_nat
            print(f"  {label:>14}  {truth_nat:>12.4f}  {mean_nat:>14.4f}  "
                  f"{std_log[ax]:>13.4f}  {rel:>+9.2%}")

        # Pass criterion 2: each seed's MAP asymptote within 30% of truth at N=500.
        if n == 500:
            n500_rel_errs = np.array([
                np.abs(np.exp(r['theta_map'][asymp_idx])
                       - np.exp(theta_truth[asymp_idx]))
                / np.exp(theta_truth[asymp_idx])
                for r in rs
            ])
            worst_per_seed = n500_rel_errs.max(axis=1)
            n_pass = int(np.sum(worst_per_seed <= ASYMPTOTE_REL_TOL))
            all_ok_30pct = (n_pass == len(rs))
            print(f"\n  asymptote within 30% of truth: {n_pass}/{len(rs)} seeds "
                  f"{'PASS' if all_ok_30pct else 'FAIL'}")

        # Pass criterion 3: cold-start MAP under 30s at N=500.
        times = [r['dt'] for r in rs]
        max_t = max(times)
        if n == 500:
            speed_ok = max_t < MAX_FIT_SECONDS_AT_N500
            all_ok_speed = speed_ok
            print(f"  fit times: " + ", ".join(f"{t:.1f}s" for t in times)
                  + f"   max {max_t:.1f}s   "
                  + ('PASS (<30s)' if speed_ok else 'FAIL (>=30s)'))
        else:
            print(f"  fit times: " + ", ".join(f"{t:.1f}s" for t in times)
                  + f"   max {max_t:.1f}s   (not gated at N={n})")

        # Pass criterion 4: no seeds stuck at init.
        stuck_seeds = [r['seed'] for r in rs if r['stuck']]
        moves = [r['max_axis_move'] for r in rs]
        unstuck_ok = (len(stuck_seeds) == 0)
        if not unstuck_ok:
            all_ok_unstuck = False
        print(f"  max axis moves: " + ", ".join(f"{m:.3f}" for m in moves)
              + f"   stuck seeds: {stuck_seeds or 'none'}   "
              + ('PASS' if unstuck_ok else 'FAIL'))

        # M_clear at anchor n (info, not gated -- still useful)
        anchors = sorted({1, n // 4, n // 2, n})
        print(f"\n  M_clear (s) by attempt n:")
        print(f"  {'n':>4}  {'truth':>10}  " + "  ".join(
            f"{'seed' + str(r['seed']):>10}" for r in rs))
        for n_eval in anchors:
            log_sf = curve(np.log(truth['sf_inf']),
                           np.log(truth['sf_1']),  truth['sf_halflife'],  n_eval)
            log_ssp = curve(np.log(truth['ssp_inf']),
                            np.log(truth['ssp_1']), truth['ssp_halflife'], n_eval)
            log_a = curve(np.log(truth['alpha_inf']),
                          np.log(truth['alpha_1']), truth['alpha_halflife'], n_eval)
            theta_eff = np.array([np.log(truth['bpt']), log_sf, log_ssp, log_a])
            truth_mc = model.m_clear(theta_eff, hazard) / 1000.0
            row = f"  {n_eval:>4}  {truth_mc:>10.2f}  "
            for r in rs:
                fit_mc = lm07.m_clear_at_n(r['theta_map'], hazard, n_eval) / 1000.0
                row += f"  {fit_mc:>10.2f}"
            print(row)
        print()

    return all_ok_30pct, all_ok_speed, all_ok_unstuck


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--ns', type=int, nargs='+', default=[500])
    ap.add_argument('--seeds', type=int, default=3)
    args = ap.parse_args()

    truth = dict(TRUTH_DEFAULTS)
    hazard = lm07.haz1()

    print(f"V07 recovery sweep")
    print(f"  N in {args.ns}, seeds in {list(range(args.seeds))}")
    print(f"  hazard: haz1 (K=1 piecewise); generator: beta-mixture timing\n")

    pass1 = report_prior_predictive(hazard)

    results = []
    for n in args.ns:
        for seed in range(args.seeds):
            print(f"[N={n}, seed={seed}] generating + fitting...", flush=True)
            r = run_one(n, seed, truth, hazard)
            print(f"  -> {r['nit']} iters in {r['dt']:.1f}s, "
                  f"converged={r['converged']}, stuck={r['stuck']}, "
                  f"max_move={r['max_axis_move']:.3f}, {r['n_died']} died")
            results.append(r)
    print()

    pass2, pass3, pass4 = report_recovery(results, truth, hazard)

    print(f"=" * 60)
    print(f"V07 pass criteria:")
    print(f"  1. prior-predictive M_clear(n=1) upper 95% < 1h:  "
          f"{'PASS' if pass1 else 'FAIL'}")
    print(f"  2. asymptote MAP within 30% of truth at N=500:    "
          f"{'PASS' if pass2 else 'FAIL'}")
    print(f"  3. cold-start MAP under 30 s at N=500:            "
          f"{'PASS' if pass3 else 'FAIL'}")
    print(f"  4. no multi-seed stuck-at-init failures:          "
          f"{'PASS' if pass4 else 'FAIL'}")
    all_ok = pass1 and pass2 and pass3 and pass4
    print(f"\n  OVERALL: {'PASS' if all_ok else 'FAIL'}")


if __name__ == '__main__':
    main()
