"""Market-factor diagnostics: covariance, beta, and PM-style risk attribution.

Joins strategy daily returns to SPX / IXIC / RUT and computes:
  - sample + shrunk covariance / correlation
  - single- and multi-factor OLS betas (incl. orthogonalized factors)
  - rolling beta/corr, upside/downside beta, capture ratios
  - PCA, tracking error / IR, hedge ratios, VIX-regime splits, tail co-movement
"""
from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

TRADING_DAYS = 252
SERIES_KEYS = ("strategy", "spx", "ixic", "rut")
SERIES_LABELS = {
    "strategy": "Strategy",
    "spx": "SPX",
    "ixic": "IXIC",
    "rut": "RUT",
}


@dataclass
class ReturnPanel:
    dates: List[str]
    strategy: np.ndarray
    spx: np.ndarray
    ixic: np.ndarray
    rut: np.ndarray
    vix_open: Optional[np.ndarray] = None

    def as_matrix(self, keys: Sequence[str] = SERIES_KEYS) -> np.ndarray:
        cols = []
        for key in keys:
            cols.append(getattr(self, key))
        return np.column_stack(cols)

    @property
    def n(self) -> int:
        return len(self.dates)


def strategy_returns_from_daily(
    rows: Sequence[dict],
    account_equity: float,
) -> Dict[str, float]:
    """Compounded path returns: r_t = net_pnl_t / equity_{t-1}."""
    equity = float(account_equity)
    out: Dict[str, float] = {}
    for row in rows:
        day = str(row.get("date", ""))[:10]
        if not day:
            continue
        pnl = float(row.get("net_pnl") or 0.0)
        if not math.isfinite(pnl):
            pnl = 0.0
        ret = pnl / equity if equity else 0.0
        out[day] = ret
        equity += pnl
    return out


def load_daily_summary_csv(path: Path) -> List[dict]:
    if not path.exists():
        raise FileNotFoundError(path)
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def build_return_panel(
    strategy_rets: Dict[str, float],
    index_rets: Dict[str, Dict[str, float]],
    *,
    vix_open_by_date: Optional[Dict[str, float]] = None,
) -> ReturnPanel:
    """Inner-join strategy dates that have all three index returns."""
    dates = sorted(
        d
        for d in strategy_rets
        if d in index_rets.get("spx", {})
        and d in index_rets.get("ixic", {})
        and d in index_rets.get("rut", {})
    )
    if not dates:
        raise ValueError("no overlapping dates between strategy and index returns")
    vix = None
    if vix_open_by_date is not None:
        vix = np.array([float(vix_open_by_date.get(d) or np.nan) for d in dates], dtype=float)
    return ReturnPanel(
        dates=dates,
        strategy=np.array([strategy_rets[d] for d in dates], dtype=float),
        spx=np.array([index_rets["spx"][d] for d in dates], dtype=float),
        ixic=np.array([index_rets["ixic"][d] for d in dates], dtype=float),
        rut=np.array([index_rets["rut"][d] for d in dates], dtype=float),
        vix_open=vix,
    )


def _cov_matrix(X: np.ndarray) -> np.ndarray:
    """Population covariance (divide by n), matching portfolio_metrics style."""
    if X.ndim != 2 or X.shape[0] < 2:
        k = X.shape[1] if X.ndim == 2 else 0
        return np.zeros((k, k))
    centered = X - X.mean(axis=0, keepdims=True)
    return (centered.T @ centered) / X.shape[0]


def _corr_from_cov(cov: np.ndarray) -> np.ndarray:
    d = np.sqrt(np.clip(np.diag(cov), 0.0, None))
    denom = np.outer(d, d)
    with np.errstate(divide="ignore", invalid="ignore"):
        corr = np.where(denom > 0, cov / denom, 0.0)
    np.fill_diagonal(corr, 1.0)
    return corr


def matrix_to_labeled(mat: np.ndarray, keys: Sequence[str] = SERIES_KEYS) -> Dict[str, Dict[str, float]]:
    out: Dict[str, Dict[str, float]] = {}
    for i, ki in enumerate(keys):
        out[ki] = {}
        for j, kj in enumerate(keys):
            out[ki][kj] = round(float(mat[i, j]), 8)
    return out


def ledoit_wolf_constant_corr(X: np.ndarray) -> Tuple[np.ndarray, float]:
    """Shrink sample cov toward constant-correlation target (Ledoit–Wolf style)."""
    n, k = X.shape
    if n < 3 or k < 2:
        return _cov_matrix(X), 1.0
    sample = _cov_matrix(X)
    variances = np.diag(sample).copy()
    std = np.sqrt(np.clip(variances, 1e-18, None))
    corr = _corr_from_cov(sample)
    # Average pairwise correlation (off-diagonal)
    mask = ~np.eye(k, dtype=bool)
    avg_corr = float(corr[mask].mean()) if mask.any() else 0.0
    target = np.outer(std, std) * avg_corr
    np.fill_diagonal(target, variances)

    centered = X - X.mean(axis=0, keepdims=True)
    # Frobenius intensity of estimation error (simplified LW intensity)
    pi_hat = 0.0
    for t in range(n):
        xt = centered[t : t + 1].T
        m = xt @ xt.T - sample
        pi_hat += float(np.sum(m * m))
    pi_hat /= n
    gamma = float(np.sum((sample - target) ** 2))
    kappa = pi_hat / gamma if gamma > 1e-18 else 1.0
    shrink = max(0.0, min(1.0, kappa / n))
    shrunk = shrink * target + (1.0 - shrink) * sample
    return shrunk, shrink


def ols_single(y: np.ndarray, x: np.ndarray) -> dict:
    """Simple OLS: y = a + b x + e."""
    n = len(y)
    if n < 3:
        return {"alpha": 0.0, "beta": 0.0, "r2": 0.0, "resid_vol": 0.0, "n": n, "se_beta": 0.0}
    x_c = x - x.mean()
    y_c = y - y.mean()
    var_x = float(np.dot(x_c, x_c))
    if var_x < 1e-18:
        return {"alpha": float(y.mean()), "beta": 0.0, "r2": 0.0, "resid_vol": float(np.std(y)), "n": n, "se_beta": 0.0}
    beta = float(np.dot(x_c, y_c) / var_x)
    alpha = float(y.mean() - beta * x.mean())
    fitted = alpha + beta * x
    resid = y - fitted
    ss_res = float(np.dot(resid, resid))
    ss_tot = float(np.dot(y_c, y_c))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 1e-18 else 0.0
    dof = max(n - 2, 1)
    sigma2 = ss_res / dof
    se_beta = math.sqrt(sigma2 / var_x) if var_x > 0 else 0.0
    return {
        "alpha": round(alpha, 8),
        "alpha_ann": round(alpha * TRADING_DAYS, 6),
        "beta": round(beta, 6),
        "r2": round(r2, 6),
        "resid_vol": round(float(np.std(resid)), 8),
        "resid_vol_ann": round(float(np.std(resid)) * math.sqrt(TRADING_DAYS), 6),
        "n": n,
        "se_beta": round(se_beta, 6),
    }


def ols_multi(y: np.ndarray, X: np.ndarray, names: Sequence[str]) -> dict:
    """Multiple OLS with intercept. X is n×k (no intercept column)."""
    n, k = X.shape
    if n < k + 2:
        return {"available": False, "reason": f"need n>={k+2}, have {n}"}
    ones = np.ones((n, 1))
    Z = np.hstack([ones, X])
    try:
        coef, _, rank, _ = np.linalg.lstsq(Z, y, rcond=None)
    except np.linalg.LinAlgError as exc:
        return {"available": False, "reason": str(exc)}
    fitted = Z @ coef
    resid = y - fitted
    ss_res = float(np.dot(resid, resid))
    y_c = y - y.mean()
    ss_tot = float(np.dot(y_c, y_c))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 1e-18 else 0.0
    # Condition number of design (ex-intercept factors)
    try:
        cond = float(np.linalg.cond(X))
    except np.linalg.LinAlgError:
        cond = float("inf")
    # VIF for each factor
    vifs: Dict[str, float] = {}
    for j, name in enumerate(names):
        others = [i for i in range(k) if i != j]
        if not others:
            vifs[name] = 1.0
            continue
        xj = X[:, j]
        Xo = X[:, others]
        Zo = np.hstack([np.ones((n, 1)), Xo])
        try:
            cj, _, _, _ = np.linalg.lstsq(Zo, xj, rcond=None)
            rj = xj - Zo @ cj
            ss_r = float(np.dot(rj, rj))
            ss_t = float(np.dot(xj - xj.mean(), xj - xj.mean()))
            r2_j = 1.0 - ss_r / ss_t if ss_t > 1e-18 else 0.0
            vifs[name] = round(1.0 / (1.0 - r2_j), 4) if r2_j < 0.999999 else 9999.0
        except np.linalg.LinAlgError:
            vifs[name] = float("nan")
    betas = {names[j]: round(float(coef[j + 1]), 6) for j in range(k)}
    return {
        "available": True,
        "alpha": round(float(coef[0]), 8),
        "alpha_ann": round(float(coef[0]) * TRADING_DAYS, 6),
        "betas": betas,
        "r2": round(r2, 6),
        "resid_vol_ann": round(float(np.std(resid)) * math.sqrt(TRADING_DAYS), 6),
        "condition_number": round(cond, 2),
        "vif": vifs,
        "n": n,
        "rank": int(rank),
    }


def residualize(y: np.ndarray, x: np.ndarray) -> np.ndarray:
    fit = ols_single(y, x)
    return y - (fit["alpha"] + fit["beta"] * x)


def _safe_corr(a: np.ndarray, b: np.ndarray) -> float:
    if len(a) < 2 or np.std(a) < 1e-18 or np.std(b) < 1e-18:
        return 0.0
    with np.errstate(divide="ignore", invalid="ignore"):
        c = np.corrcoef(a, b)[0, 1]
    return float(c) if np.isfinite(c) else 0.0


def partial_correlation(a: np.ndarray, b: np.ndarray, control: np.ndarray) -> float:
    ra = residualize(a, control)
    rb = residualize(b, control)
    return _safe_corr(ra, rb)


def rolling_beta_corr(
    y: np.ndarray,
    x: np.ndarray,
    dates: Sequence[str],
    window: int,
) -> List[dict]:
    out: List[dict] = []
    if len(y) < window:
        return out
    for i in range(window - 1, len(y)):
        ys = y[i - window + 1 : i + 1]
        xs = x[i - window + 1 : i + 1]
        fit = ols_single(ys, xs)
        corr = _safe_corr(ys, xs)
        out.append(
            {
                "date": dates[i],
                "beta": fit["beta"],
                "corr": round(corr, 6),
                "r2": fit["r2"],
            }
        )
    return out


def upside_downside_beta(y: np.ndarray, x: np.ndarray) -> dict:
    up = x > 0
    down = x < 0
    return {
        "upside": ols_single(y[up], x[up]) if up.sum() >= 3 else None,
        "downside": ols_single(y[down], x[down]) if down.sum() >= 3 else None,
        "n_up": int(up.sum()),
        "n_down": int(down.sum()),
    }


def capture_ratios(y: np.ndarray, x: np.ndarray) -> dict:
    up = x > 0
    down = x < 0
    up_cap = float(y[up].mean() / x[up].mean()) if up.any() and abs(x[up].mean()) > 1e-12 else None
    down_cap = float(y[down].mean() / x[down].mean()) if down.any() and abs(x[down].mean()) > 1e-12 else None
    return {
        "up_capture": round(up_cap, 4) if up_cap is not None else None,
        "down_capture": round(down_cap, 4) if down_cap is not None else None,
    }


def tracking_error_ir(y: np.ndarray, bench: np.ndarray) -> dict:
    active = y - bench
    te = float(np.std(active))
    te_ann = te * math.sqrt(TRADING_DAYS)
    ir = (float(active.mean()) / te) * math.sqrt(TRADING_DAYS) if te > 1e-18 else 0.0
    return {
        "tracking_error_ann": round(te_ann, 6),
        "information_ratio": round(ir, 4),
        "active_mean_ann": round(float(active.mean()) * TRADING_DAYS, 6),
    }


def hedge_ratio(y: np.ndarray, bench: np.ndarray) -> float:
    var_b = float(np.var(bench))
    if var_b < 1e-18:
        return 0.0
    return float(np.cov(y, bench, bias=True)[0, 1] / var_b)


def pca_panel(X: np.ndarray, names: Sequence[str]) -> dict:
    """PCA on standardized columns."""
    n, k = X.shape
    if n < 2:
        return {"available": False}
    std = X.std(axis=0)
    std = np.where(std < 1e-18, 1.0, std)
    Z = (X - X.mean(axis=0)) / std
    cov = _cov_matrix(Z)
    eigvals, eigvecs = np.linalg.eigh(cov)
    order = np.argsort(eigvals)[::-1]
    eigvals = eigvals[order]
    eigvecs = eigvecs[:, order]
    total = float(eigvals.sum()) if eigvals.sum() > 0 else 1.0
    components = []
    for i in range(k):
        loadings = {names[j]: round(float(eigvecs[j, i]), 6) for j in range(k)}
        components.append(
            {
                "pc": i + 1,
                "eigenvalue": round(float(eigvals[i]), 6),
                "variance_pct": round(100.0 * float(eigvals[i]) / total, 2),
                "loadings": loadings,
            }
        )
    return {"available": True, "components": components}


def mctr_sleeve_example(
    cov: np.ndarray,
    names: Sequence[str],
    weights: Sequence[float],
) -> dict:
    """Marginal contribution to risk for an illustrative multi-asset book."""
    w = np.array(weights, dtype=float)
    if abs(w.sum() - 1.0) > 1e-6:
        w = w / w.sum()
    port_var = float(w @ cov @ w)
    port_vol = math.sqrt(max(port_var, 0.0))
    if port_vol < 1e-18:
        return {"available": False, "reason": "zero portfolio vol"}
    mctr = (cov @ w) / port_vol
    cctr = w * mctr
    return {
        "available": True,
        "weights": {names[i]: round(float(w[i]), 4) for i in range(len(names))},
        "portfolio_vol_ann": round(port_vol * math.sqrt(TRADING_DAYS), 6),
        "mctr": {names[i]: round(float(mctr[i]) * math.sqrt(TRADING_DAYS), 6) for i in range(len(names))},
        "cctr_pct": {names[i]: round(100.0 * float(cctr[i]) / port_vol, 2) for i in range(len(names))},
        "note": "Illustrative sleeve: 50% SPX / 20% IXIC / 15% RUT / 15% strategy",
    }


def vix_regime_buckets(vix: float) -> str:
    if not math.isfinite(vix):
        return "unknown"
    if vix < 15.0:
        return "lt15"
    if vix < 25.0:
        return "15_25"
    if vix <= 35.0:
        return "25_35"
    return "gt35"


def regime_conditional(panel: ReturnPanel) -> List[dict]:
    if panel.vix_open is None:
        return []
    rows = []
    for bucket in ("lt15", "15_25", "25_35", "gt35"):
        mask = np.array([vix_regime_buckets(float(v)) == bucket for v in panel.vix_open])
        n = int(mask.sum())
        if n < 10:
            rows.append({"regime": bucket, "n": n, "available": False})
            continue
        y = panel.strategy[mask]
        spx = panel.spx[mask]
        fit = ols_single(y, spx)
        corr = _safe_corr(y, spx)
        rows.append(
            {
                "regime": bucket,
                "n": n,
                "available": True,
                "beta_spx": fit["beta"],
                "corr_spx": round(corr, 4),
                "r2_spx": fit["r2"],
                "strategy_vol_ann": round(float(np.std(y)) * math.sqrt(TRADING_DAYS), 4),
            }
        )
    return rows


def tail_comovement(y: np.ndarray, x: np.ndarray, pct: float = 0.05) -> dict:
    n = len(x)
    k = max(int(math.floor(n * pct)), 5)
    order = np.argsort(x)[:k]
    ys, xs = y[order], x[order]
    corr = _safe_corr(ys, xs)
    return {
        "pct": pct,
        "n_tail": int(k),
        "corr_worst_spx_days": round(corr, 4),
        "mean_strategy_on_tail": round(float(ys.mean()), 6),
        "mean_spx_on_tail": round(float(xs.mean()), 6),
    }


def era_split(panel: ReturnPanel, split_date: str = "2022-05-02") -> dict:
    """Pre/post SPXW daily-expiry era (approx May 2022)."""
    pre_mask = np.array([d < split_date for d in panel.dates])
    post_mask = ~pre_mask
    out = {}
    for label, mask in (("pre_daily_expiry", pre_mask), ("post_daily_expiry", post_mask)):
        n = int(mask.sum())
        if n < 30:
            out[label] = {"n": n, "available": False}
            continue
        fit = ols_single(panel.strategy[mask], panel.spx[mask])
        out[label] = {"n": n, "available": True, **{k: fit[k] for k in ("beta", "alpha_ann", "r2")}}
    return out


def downsample_rolling(series: List[dict], max_points: int = 180) -> List[dict]:
    if len(series) <= max_points:
        return series
    step = max(1, len(series) // max_points)
    sampled = series[::step]
    if sampled[-1] is not series[-1]:
        sampled.append(series[-1])
    return sampled


def run_full_analysis(panel: ReturnPanel) -> dict:
    X = panel.as_matrix()
    cov = _cov_matrix(X)
    corr = _corr_from_cov(cov)
    shrunk, shrink = ledoit_wolf_constant_corr(X)
    shrunk_corr = _corr_from_cov(shrunk)

    single = {
        "spx": ols_single(panel.strategy, panel.spx),
        "ixic": ols_single(panel.strategy, panel.ixic),
        "rut": ols_single(panel.strategy, panel.rut),
    }
    multi = ols_multi(
        panel.strategy,
        np.column_stack([panel.spx, panel.ixic, panel.rut]),
        ["spx", "ixic", "rut"],
    )
    # Orthogonalize IXIC and RUT on SPX
    ixic_orth = residualize(panel.ixic, panel.spx)
    rut_orth = residualize(panel.rut, panel.spx)
    ortho = ols_multi(
        panel.strategy,
        np.column_stack([panel.spx, ixic_orth, rut_orth]),
        ["spx", "ixic_resid", "rut_resid"],
    )

    partial = {
        "strategy_ixic_given_spx": round(partial_correlation(panel.strategy, panel.ixic, panel.spx), 4),
        "strategy_rut_given_spx": round(partial_correlation(panel.strategy, panel.rut, panel.spx), 4),
    }

    rolling = {}
    for window in (63, 126, 252):
        rolling[str(window)] = {
            bench: downsample_rolling(rolling_beta_corr(panel.strategy, getattr(panel, bench), panel.dates, window))
            for bench in ("spx", "ixic", "rut")
        }

    up_down = {b: upside_downside_beta(panel.strategy, getattr(panel, b)) for b in ("spx", "ixic", "rut")}
    capture = {b: capture_ratios(panel.strategy, getattr(panel, b)) for b in ("spx", "ixic", "rut")}

    hedges = {b: round(hedge_ratio(panel.strategy, getattr(panel, b)), 6) for b in ("spx", "ixic", "rut")}
    te_ir = tracking_error_ir(panel.strategy, panel.spx)

    # Illustrative book: SPX 50 / IXIC 20 / RUT 15 / strategy 15 — reorder cov to match
    # cov is strategy, spx, ixic, rut — rebuild for spx, ixic, rut, strategy
    order = [1, 2, 3, 0]
    cov_book = cov[np.ix_(order, order)]
    mctr = mctr_sleeve_example(cov_book, ["spx", "ixic", "rut", "strategy"], [0.50, 0.20, 0.15, 0.15])

    return {
        "n_days": panel.n,
        "date_start": panel.dates[0],
        "date_end": panel.dates[-1],
        "labels": SERIES_LABELS,
        "covariance": matrix_to_labeled(cov),
        "correlation": matrix_to_labeled(corr),
        "covariance_shrunk": matrix_to_labeled(shrunk),
        "correlation_shrunk": matrix_to_labeled(shrunk_corr),
        "shrinkage_intensity": round(shrink, 4),
        "single_factor": single,
        "multi_factor": multi,
        "orthogonal_factor": ortho,
        "partial_correlation": partial,
        "rolling": rolling,
        "upside_downside": up_down,
        "capture": capture,
        "hedge_ratios": hedges,
        "tracking_vs_spx": te_ir,
        "pca": pca_panel(X, SERIES_KEYS),
        "mctr_example": mctr,
        "vix_regimes": regime_conditional(panel),
        "tail_comovement_spx": tail_comovement(panel.strategy, panel.spx),
        "era_split": era_split(panel),
        "headline": {
            "beta_spx": single["spx"]["beta"],
            "r2_spx": single["spx"]["r2"],
            "alpha_ann_spx": single["spx"]["alpha_ann"],
            "beta_ixic": single["ixic"]["beta"],
            "beta_rut": single["rut"]["beta"],
            "corr_spx": round(float(corr[0, 1]), 4),
            "corr_ixic": round(float(corr[0, 2]), 4),
            "corr_rut": round(float(corr[0, 3]), 4),
            "te_ann_vs_spx": te_ir["tracking_error_ann"],
            "ir_vs_spx": te_ir["information_ratio"],
        },
    }
