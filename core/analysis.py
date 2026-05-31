"""Read-only analytics for arcticdb-viewer.

Every function here is pure and side-effect free — it takes a DataFrame
(already read from ArcticDB) and returns plain Python dicts/lists ready for
JSON or template rendering. Nothing in this module can mutate stored data,
which is exactly what you want when inspecting production market data.

The feature set is adapted from dtale (describe, distribution, correlation,
data-quality, outliers) plus finance-specific returns/risk statistics.
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

# Periods-per-year lookup for annualising returns, keyed by the median
# spacing of a DatetimeIndex (in days).
_TRADING_DAYS = 252
_CALENDAR_DAYS = 365


def _clean_float(v: Any) -> float | None:
    """Convert to a JSON-safe float (NaN/Inf -> None)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _numeric_columns(df: pd.DataFrame) -> list[str]:
    return [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]


def _spearman(a: pd.Series, b: pd.Series) -> float:
    """Spearman correlation without a scipy dependency (Pearson of ranks)."""
    return float(a.rank().corr(b.rank()))


# ── 1. Describe (per-column summary statistics) ──

def describe_frame(df: pd.DataFrame) -> list[dict[str, Any]]:
    """Per-column summary stats — adapted from dtale's describe view.

    Returns one row per column with dtype, counts, and (for numerics)
    distribution statistics including skew and kurtosis.
    """
    n = len(df)
    rows: list[dict[str, Any]] = []
    for col in df.columns:
        s = df[col]
        missing = int(s.isna().sum())
        rec: dict[str, Any] = {
            "column": str(col),
            "dtype": str(s.dtype),
            "count": int(n - missing),
            "missing": missing,
            "missing_pct": round(missing / n * 100, 2) if n else 0.0,
            "distinct": int(s.nunique(dropna=True)),
        }
        if pd.api.types.is_numeric_dtype(s):
            sd = s.dropna()
            rec.update({
                "kind": "numeric",
                "mean": _clean_float(sd.mean()),
                "std": _clean_float(sd.std()),
                "min": _clean_float(sd.min()),
                "q25": _clean_float(sd.quantile(0.25)) if len(sd) else None,
                "median": _clean_float(sd.median()) if len(sd) else None,
                "q75": _clean_float(sd.quantile(0.75)) if len(sd) else None,
                "max": _clean_float(sd.max()),
                "skew": _clean_float(sd.skew()) if len(sd) > 2 else None,
                "kurtosis": _clean_float(sd.kurtosis()) if len(sd) > 3 else None,
                "zeros": int((sd == 0).sum()),
            })
        elif pd.api.types.is_datetime64_any_dtype(s):
            sd = s.dropna()
            rec.update({
                "kind": "datetime",
                "min": str(sd.min()) if len(sd) else None,
                "max": str(sd.max()) if len(sd) else None,
            })
        else:
            sd = s.dropna()
            top = sd.value_counts().head(1)
            rec.update({
                "kind": "categorical",
                "top": str(top.index[0]) if len(top) else None,
                "top_freq": int(top.iloc[0]) if len(top) else 0,
            })
        rows.append(rec)
    return rows


# ── 2. Column distribution (histogram + value counts + outliers) ──

def column_histogram(s: pd.Series, bins: int = 30) -> dict[str, Any]:
    """numpy histogram for a numeric column."""
    sd = pd.to_numeric(s, errors="coerce").dropna()
    if len(sd) == 0:
        return {"bins": [], "counts": [], "labels": []}
    counts, edges = np.histogram(sd.values, bins=min(bins, max(1, sd.nunique())))
    labels = [
        f"{_fmt_num(edges[i])} – {_fmt_num(edges[i + 1])}"
        for i in range(len(edges) - 1)
    ]
    return {
        "counts": [int(c) for c in counts],
        "edges": [_clean_float(e) for e in edges],
        "labels": labels,
    }


def value_counts(s: pd.Series, top: int = 25) -> dict[str, Any]:
    """Top-N value counts with percentages (dtale value-counts view)."""
    vc = s.value_counts(dropna=False).head(top)
    total = len(s)
    return {
        "values": [("∅ (missing)" if pd.isna(i) else str(i)) for i in vc.index],
        "counts": [int(c) for c in vc.values],
        "pcts": [round(int(c) / total * 100, 2) if total else 0.0 for c in vc.values],
    }


def outliers_iqr(s: pd.Series, k: float = 1.5) -> dict[str, Any] | None:
    """IQR-based outlier detection (Tukey fences) — dtale outlier view.

    For OHLCV data this flags suspect ticks (fat-finger prints, bad fills).
    """
    sd = pd.to_numeric(s, errors="coerce").dropna()
    if len(sd) < 4:
        return None
    q1 = float(sd.quantile(0.25))
    q3 = float(sd.quantile(0.75))
    iqr = q3 - q1
    lower = q1 - k * iqr
    upper = q3 + k * iqr
    mask = (sd < lower) | (sd > upper)
    n_out = int(mask.sum())
    return {
        "q1": _clean_float(q1),
        "q3": _clean_float(q3),
        "iqr": _clean_float(iqr),
        "lower": _clean_float(lower),
        "upper": _clean_float(upper),
        "count": n_out,
        "pct": round(n_out / len(sd) * 100, 3),
    }


def column_analysis(df: pd.DataFrame, col: str, bins: int = 30) -> dict[str, Any]:
    """Combined per-column report: stats + histogram/value-counts + outliers."""
    if col == "__index__":
        s = df.index.to_series()
        s.name = df.index.name or "index"
    elif col not in df.columns:
        raise KeyError(col)
    else:
        s = df[col]

    numeric = pd.api.types.is_numeric_dtype(s)
    out: dict[str, Any] = {
        "column": str(s.name if s.name is not None else col),
        "dtype": str(s.dtype),
        "numeric": numeric,
        "count": int(s.notna().sum()),
        "missing": int(s.isna().sum()),
        "distinct": int(s.nunique(dropna=True)),
    }
    if numeric:
        out["histogram"] = column_histogram(s, bins)
        out["outliers"] = outliers_iqr(s)
        sd = pd.to_numeric(s, errors="coerce").dropna()
        out["stats"] = {
            "mean": _clean_float(sd.mean()),
            "std": _clean_float(sd.std()),
            "min": _clean_float(sd.min()),
            "median": _clean_float(sd.median()) if len(sd) else None,
            "max": _clean_float(sd.max()),
        }
    else:
        out["value_counts"] = value_counts(s)
    return out


# ── 3. Correlation matrix ──

def correlation_matrix(df: pd.DataFrame, method: str = "pearson") -> dict[str, Any]:
    """Pairwise correlation of numeric columns (dtale correlations view)."""
    if method not in ("pearson", "spearman", "kendall"):
        method = "pearson"
    num = df[_numeric_columns(df)]
    # Drop columns that are entirely NaN or constant (corr is undefined).
    num = num.loc[:, num.nunique(dropna=True) > 1]
    if num.shape[1] < 2:
        return {"columns": [], "matrix": [], "method": method}
    if method == "spearman":
        corr = num.rank().corr()              # Pearson of ranks — scipy-free
    elif method == "kendall":
        try:
            corr = num.corr(method="kendall")  # needs scipy; fall back if absent
        except Exception:
            corr = num.rank().corr()
            method = "spearman"
    else:
        corr = num.corr()
    cols = [str(c) for c in corr.columns]
    matrix = [[_clean_float(v) for v in row] for row in corr.values]
    return {"columns": cols, "matrix": matrix, "method": method}


# ── 4. Data-quality report ──

def quality_report(df: pd.DataFrame) -> dict[str, Any]:
    """Whole-frame data-quality summary (missing / duplicate / constant)."""
    n = len(df)
    per_col = []
    constant_cols = []
    for col in df.columns:
        s = df[col]
        miss = int(s.isna().sum())
        distinct = int(s.nunique(dropna=True))
        if distinct <= 1:
            constant_cols.append(str(col))
        per_col.append({
            "column": str(col),
            "dtype": str(s.dtype),
            "missing": miss,
            "missing_pct": round(miss / n * 100, 2) if n else 0.0,
            "distinct": distinct,
        })
    try:
        dup_rows = int(df.duplicated().sum())
    except TypeError:
        dup_rows = 0  # unhashable cell types
    dup_index = int(df.index.duplicated().sum())
    total_cells = n * max(1, df.shape[1])
    total_missing = int(df.isna().sum().sum())
    return {
        "rows": n,
        "columns": df.shape[1],
        "total_missing": total_missing,
        "total_missing_pct": round(total_missing / total_cells * 100, 2) if total_cells else 0.0,
        "duplicate_rows": dup_rows,
        "duplicate_index": dup_index,
        "constant_columns": constant_cols,
        "memory_mb": round(df.memory_usage(deep=True).sum() / 1024 / 1024, 2),
        "per_column": per_col,
        "timeseries": timeseries_gaps(df),
    }


def timeseries_gaps(df: pd.DataFrame) -> dict[str, Any] | None:
    """Detect calendar gaps / duplicate timestamps / sort order on the index.

    Works on a DatetimeIndex or the first level of a MultiIndex. Critical for
    market data: a hole in the series often means a missed download.
    """
    idx = df.index
    if isinstance(idx, pd.MultiIndex):
        level0 = idx.get_level_values(0)
        if not pd.api.types.is_datetime64_any_dtype(level0):
            return None
        dates = pd.DatetimeIndex(level0).unique().sort_values()
    elif isinstance(idx, pd.DatetimeIndex):
        dates = idx
    else:
        return None

    if len(dates) < 3:
        return None

    monotonic = bool(pd.Series(dates).is_monotonic_increasing)
    dup = int(pd.Series(dates).duplicated().sum())
    uniq = dates.unique().sort_values()
    deltas = uniq.to_series().diff().dropna()
    if deltas.empty:
        return None
    median_delta = deltas.median()
    # Count gaps materially larger than the typical spacing (weekends for
    # daily data are expected; flag anything > ~3x median spacing).
    threshold = median_delta * 3
    big_gaps = deltas[deltas > threshold]
    gap_list = [
        {"after": str(uniq[i]), "gap_days": round(d.total_seconds() / 86400, 1)}
        for i, d in zip(big_gaps.index.map(lambda x: uniq.get_loc(x) - 1), big_gaps)
    ][:20]
    return {
        "start": str(uniq.min()),
        "end": str(uniq.max()),
        "periods": int(len(uniq)),
        "median_spacing_days": round(median_delta.total_seconds() / 86400, 3),
        "monotonic_increasing": monotonic,
        "duplicate_timestamps": dup,
        "large_gaps": int(len(big_gaps)),
        "gap_samples": gap_list,
    }


# ── 5. Returns / risk summary (finance-specific) ──

def _periods_per_year(index: pd.Index) -> float:
    if isinstance(index, pd.MultiIndex):
        index = pd.DatetimeIndex(index.get_level_values(0).unique())
    if not isinstance(index, pd.DatetimeIndex) or len(index) < 3:
        return _TRADING_DAYS
    deltas = pd.Series(index.sort_values()).diff().dropna()
    if deltas.empty:
        return _TRADING_DAYS
    median_days = deltas.median().total_seconds() / 86400
    if median_days <= 0:
        return _TRADING_DAYS
    if median_days < 1.5:
        return _TRADING_DAYS          # daily
    if median_days < 4:
        return _TRADING_DAYS / 3      # ~few-day
    if median_days < 10:
        return 52.0                   # weekly
    if median_days < 45:
        return 12.0                   # monthly
    if median_days < 130:
        return 4.0                    # quarterly
    return 1.0                        # yearly


def returns_stats(df: pd.DataFrame, col: str) -> dict[str, Any]:
    """Risk/return summary for a price column.

    Computes simple returns, then CAGR, annualised volatility, Sharpe
    (rf=0), max drawdown, and hit-rate — the numbers a futures/equity
    desk looks at first. Read-only: derived entirely from the price series.
    """
    if col not in df.columns:
        raise KeyError(col)
    price = pd.to_numeric(df[col], errors="coerce").dropna()
    if len(price) < 3:
        return {"error": "Not enough data points for return statistics."}

    ppy = _periods_per_year(df.index)
    rets = price.pct_change().dropna()
    if rets.empty:
        return {"error": "Could not compute returns (constant or invalid series)."}

    total_return = float(price.iloc[-1] / price.iloc[0] - 1.0)
    n_periods = len(rets)
    years = n_periods / ppy if ppy else 0
    cagr = float((price.iloc[-1] / price.iloc[0]) ** (1 / years) - 1) if years > 0 and price.iloc[0] > 0 else None
    vol = float(rets.std() * math.sqrt(ppy))
    mean_ann = float(rets.mean() * ppy)
    sharpe = float(mean_ann / vol) if vol > 0 else None

    # Max drawdown on the cumulative curve.
    curve = (1 + rets).cumprod()
    running_max = curve.cummax()
    drawdown = curve / running_max - 1.0
    max_dd = float(drawdown.min())

    wins = int((rets > 0).sum())
    return {
        "column": str(col),
        "observations": int(n_periods),
        "periods_per_year": round(ppy, 1),
        "total_return": _clean_float(total_return),
        "cagr": _clean_float(cagr),
        "ann_volatility": _clean_float(vol),
        "ann_return": _clean_float(mean_ann),
        "sharpe": _clean_float(sharpe),
        "max_drawdown": _clean_float(max_dd),
        "best_period": _clean_float(rets.max()),
        "worst_period": _clean_float(rets.min()),
        "hit_rate": round(wins / n_periods * 100, 2),
        "skew": _clean_float(rets.skew()) if n_periods > 2 else None,
        "kurtosis": _clean_float(rets.kurtosis()) if n_periods > 3 else None,
    }


# ── 6. Signal analysis (information coefficient & quantile buckets) ──

def signal_analysis(
    df: pd.DataFrame,
    signal_col: str,
    price_col: str,
    horizons: tuple[int, ...] = (1, 5, 10, 20),
    bucket_horizon: int = 1,
    buckets: int = 5,
) -> dict[str, Any]:
    """Evaluate a predictive signal against forward returns — read-only.

    Computes the Information Coefficient (rank/Spearman and Pearson
    correlation between the signal and forward returns) across several
    horizons, and the mean forward return per signal quantile bucket. This
    answers "does my signal actually predict returns?" without ever writing
    to the data.
    """
    if signal_col not in df.columns:
        raise KeyError(signal_col)
    if price_col not in df.columns:
        raise KeyError(price_col)
    if isinstance(df.index, pd.MultiIndex):
        return {"error": "Signal analysis expects a single-index series. "
                         "For futures, chart a continuous series first."}

    sig = pd.to_numeric(df[signal_col], errors="coerce")
    price = pd.to_numeric(df[price_col], errors="coerce")
    if sig.notna().sum() < 10 or price.notna().sum() < 10:
        return {"error": "Not enough numeric data in the chosen columns."}

    # IC across horizons.
    ic_rows = []
    for h in horizons:
        fwd = price.shift(-h) / price - 1.0
        pair = pd.concat([sig, fwd], axis=1, keys=["s", "f"]).dropna()
        if len(pair) < 10:
            ic_rows.append({"horizon": h, "n": len(pair), "ic_spearman": None,
                            "ic_pearson": None, "mean_fwd": None})
            continue
        ic_s = _spearman(pair["s"], pair["f"])
        ic_p = pair["s"].corr(pair["f"])  # Pearson (scipy-free)
        ic_rows.append({
            "horizon": h,
            "n": int(len(pair)),
            "ic_spearman": _clean_float(ic_s),
            "ic_pearson": _clean_float(ic_p),
            "mean_fwd": _clean_float(pair["f"].mean()),
        })

    # Quantile buckets at the chosen horizon.
    fwd = price.shift(-bucket_horizon) / price - 1.0
    pair = pd.concat([sig, fwd], axis=1, keys=["s", "f"]).dropna()
    bucket_data: dict[str, Any] = {"horizon": bucket_horizon, "labels": [],
                                   "mean_fwd": [], "counts": [], "monotonic": None}
    if len(pair) >= buckets * 2 and pair["s"].nunique() >= buckets:
        try:
            qs = pd.qcut(pair["s"], buckets, labels=False, duplicates="drop")
            grp = pair.groupby(qs)["f"]
            means = grp.mean()
            counts = grp.size()
            n_b = len(means)
            bucket_data["labels"] = [f"Q{i + 1}" for i in range(n_b)]
            bucket_data["mean_fwd"] = [_clean_float(v) for v in means.values]
            bucket_data["counts"] = [int(c) for c in counts.values]
            vals = list(means.values)
            inc = all(vals[i] <= vals[i + 1] for i in range(len(vals) - 1))
            dec = all(vals[i] >= vals[i + 1] for i in range(len(vals) - 1))
            bucket_data["monotonic"] = inc or dec
            # Long-short spread: top minus bottom bucket.
            bucket_data["long_short"] = _clean_float(vals[-1] - vals[0]) if vals else None
        except (ValueError, IndexError):
            pass

    primary = ic_rows[0] if ic_rows else {}
    return {
        "signal": str(signal_col),
        "price": str(price_col),
        "ic": ic_rows,
        "buckets": bucket_data,
        "primary_ic_spearman": primary.get("ic_spearman"),
    }


# ── helpers ──

def _fmt_num(v: float) -> str:
    av = abs(v)
    if av != 0 and (av < 0.001 or av >= 1e7):
        return f"{v:.2e}"
    if av >= 100:
        return f"{v:,.1f}"
    return f"{v:.3f}"
