"""Public API for the V07 segment model. Single import surface for downstream
Python consumers.

For the v1 SpinLab integration, use the JSON-payload surface — the three
contract helpers below produce dicts matching `external_docs/api_contract.md`.

    import segments_v07 as sv

    sv.prewarm_buckets()                                     # one-time JIT warm

    payload = sv.fit_segment(attempts, segment_id='w1-2-castle')
    payload = sv.refit_segment(attempts, segment_id='w1-2-castle',
                                prev_result=payload)         # streaming refit
    pool    = sv.fit_pool([{'segment_id': ..., 'attempts': [...]}, ...])

The lower-level math API (`find_map`, `laplace_posterior`, `log_posterior`,
etc.) is what the helpers above compose. Use it directly only for tooling
that needs intermediate quantities — e.g. the inspector, calibration probes,
or research scripts in `tmp/`. See README "Public API surface" for the full
list; the contract surface is what SpinLab depends on.

Theta layout (length 10, log-space) is constants below. Curve evaluation:
    log_theta(n) = log_theta_inf + (log_theta_1 - log_theta_inf) * 2^(-(n-1)/halflife)

Everything imported here is verified by the test suite in tests/.
"""

# ----- Model constants -----
from learning_model_v07 import (
    N_PARAMS,
    IDX_LOG_BPT,
    IDX_LOG_SF_INF, IDX_LOG_SSP_INF, IDX_LOG_ALPHA_INF,
    IDX_LOG_SF_1,   IDX_LOG_SSP_1,   IDX_LOG_ALPHA_1,
    IDX_LOG_HALFLIFE_SF, IDX_LOG_HALFLIFE_SSP, IDX_LOG_HALFLIFE_ALPHA,
)

# ----- Pure-math eval (JAX, for callers that need to compute curves) -----
from learning_model_v07_jax import (
    learning_curve_at_n,
    log_prior,
    log_likelihood,
    log_posterior,
    initial_theta,
    data_to_arrays,
    bucket_size,
    pad_to,
    halflife_sigma,
)

# Prior sampling lives in the numpy reference module (use numpy_reference.sample_prior).

# ----- Fit + posterior -----
from fit_jax import (
    warm_init_from_data,
    prewarm_buckets,
    find_map,
    laplace_posterior,
    sample_laplace,
)

# ----- Multi-segment empirical-Bayes pool (HYPER-lite, all 3 halflives) -----
from fit_eb_pool import (
    Pool,                # NamedTuple with 6 fields: log_halflife_{sf,ssp,alpha}_{mean,sigma}
    fit_eb_pool,         # one-step sigma + iterated mean, returns (pool, maps, history)
    fit_independent,    # baseline: no pooling
)

# ----- Numpy reference (kept for cross-validation and offline tools) -----
import learning_model_v07 as numpy_reference

# ----- v1 JSON-payload serializer (the SpinLab handoff surface) -----
from api import (
    SCHEMA,
    fit_segment,
    refit_segment,
    fit_pool,
)

__all__ = [
    # constants
    'N_PARAMS',
    'IDX_LOG_BPT',
    'IDX_LOG_SF_INF',  'IDX_LOG_SSP_INF',  'IDX_LOG_ALPHA_INF',
    'IDX_LOG_SF_1',    'IDX_LOG_SSP_1',    'IDX_LOG_ALPHA_1',
    'IDX_LOG_HALFLIFE_SF', 'IDX_LOG_HALFLIFE_SSP', 'IDX_LOG_HALFLIFE_ALPHA',
    # eval
    'learning_curve_at_n',
    'log_prior', 'log_likelihood', 'log_posterior',
    'initial_theta', 'data_to_arrays',
    'halflife_sigma',
    'bucket_size', 'pad_to',
    # fit
    'warm_init_from_data', 'prewarm_buckets', 'find_map',
    'laplace_posterior', 'sample_laplace',
    # EB pool
    'Pool', 'fit_eb_pool', 'fit_independent',
    # v1 JSON-payload serializer
    'SCHEMA', 'fit_segment', 'refit_segment', 'fit_pool',
    # reference (numpy: sample_prior, log_prior, log_likelihood etc.)
    'numpy_reference',
]
