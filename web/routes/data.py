import io
import json
import re

import pandas as pd
from fastapi import APIRouter, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse

from core import operations as ops
from web.app import templates

router = APIRouter()

DEFAULT_PAGE_SIZE = 50


# ── Value parsing ──

def _parse_timestamp(val: str) -> pd.Timestamp:
    """Parse a date string. ISO format (YYYY-MM-DD) detected automatically,
    otherwise dayfirst=True for European DD.MM.YYYY / DD/MM/YYYY convention."""
    val = val.strip()
    if re.match(r'^\d{4}-\d{2}', val):
        return pd.Timestamp(val)
    return pd.to_datetime(val, dayfirst=True)


def _parse_value(val: str) -> float | str:
    """Parse filter value, supporting 10^6, 2.5e3, 1_000_000."""
    val = val.strip()
    match = re.match(r'^([+-]?[\d.]+)\s*\^\s*([+-]?[\d.]+)$', val)
    if match:
        return float(match.group(1)) ** float(match.group(2))
    cleaned = val.replace('_', '')
    try:
        return float(cleaned)
    except (ValueError, TypeError):
        return val


# ── Single filter ──

def _apply_single_filter(df: pd.DataFrame, col: str, op: str, val: str) -> pd.DataFrame:
    is_index = col == "__index__"

    # Check for MultiIndex level filters (__index_0__, __index_1__)
    mi_level = None
    if col.startswith("__index_") and col.endswith("__") and isinstance(df.index, pd.MultiIndex):
        try:
            mi_level = int(col[len("__index_"):-len("__")])
        except ValueError:
            pass

    if is_index or mi_level is not None:
        # For MultiIndex, __index__ filters on level 0 (typically date)
        if isinstance(df.index, pd.MultiIndex):
            level = mi_level if mi_level is not None else 0
            level_values = df.index.get_level_values(level)
            if pd.api.types.is_datetime64_any_dtype(level_values):
                try:
                    if op == "between":
                        parts = [p.strip() for p in val.split(",")]
                        if len(parts) == 2:
                            mask = (level_values >= _parse_timestamp(parts[0])) & (level_values <= _parse_timestamp(parts[1]))
                            return df[mask]
                        return df
                    parsed_ts = _parse_timestamp(val)
                    ops_map = {"eq": "__eq__", "neq": "__ne__", "gt": "__gt__", "gte": "__ge__", "lt": "__lt__", "lte": "__le__"}
                    if op in ops_map:
                        series = pd.Series(level_values, index=df.index)
                        return df[getattr(series, ops_map[op])(parsed_ts)]
                    return df
                except Exception:
                    pass
            # Fall through to string comparison on that level
            series = pd.Series(level_values.astype(str), index=df.index)
        else:
            series = df.index.to_series()
            if isinstance(df.index, pd.DatetimeIndex):
                try:
                    if op == "between":
                        parts = [p.strip() for p in val.split(",")]
                        if len(parts) == 2:
                            return df[(series >= _parse_timestamp(parts[0])) & (series <= _parse_timestamp(parts[1]))]
                        return df
                    parsed_ts = _parse_timestamp(val)
                    ops_map = {"eq": "__eq__", "neq": "__ne__", "gt": "__gt__", "gte": "__ge__", "lt": "__lt__", "lte": "__le__"}
                    if op in ops_map:
                        return df[getattr(series, ops_map[op])(parsed_ts)]
                    return df
                except Exception:
                    pass
            series = series.astype(str)
    else:
        if col not in df.columns:
            return df
        series = df[col]

    parsed = _parse_value(val)

    # Numeric
    if isinstance(parsed, float) and not is_index and pd.api.types.is_numeric_dtype(series):
        if op == "between":
            parts = [p.strip() for p in val.split(",")]
            if len(parts) == 2:
                lo, hi = _parse_value(parts[0]), _parse_value(parts[1])
                if isinstance(lo, float) and isinstance(hi, float):
                    return df[(series >= lo) & (series <= hi)]
            return df
        ops_map = {"eq": "__eq__", "neq": "__ne__", "gt": "__gt__", "gte": "__ge__", "lt": "__lt__", "lte": "__le__"}
        if op in ops_map:
            return df[getattr(series, ops_map[op])(parsed)]
        return df

    # Datetime columns (e.g. expiry) — try to parse val as a date
    if not is_index and pd.api.types.is_datetime64_any_dtype(series):
        try:
            if op == "between":
                parts = [p.strip() for p in val.split(",")]
                if len(parts) == 2:
                    lo_ts = _parse_timestamp(parts[0])
                    hi_ts = _parse_timestamp(parts[1])
                    return df[(series >= lo_ts) & (series <= hi_ts)]
                return df
            parsed_ts = _parse_timestamp(val)
            ops_map = {"eq": "__eq__", "neq": "__ne__", "gt": "__gt__", "gte": "__ge__", "lt": "__lt__", "lte": "__le__"}
            if op in ops_map:
                return df[getattr(series, ops_map[op])(parsed_ts)]
        except Exception:
            pass

    # String
    str_series = series.astype(str)
    val_str = str(parsed) if isinstance(parsed, float) else val
    str_ops = {
        "eq": lambda s, v: s == v,
        "neq": lambda s, v: s != v,
        "gt": lambda s, v: s > v,
        "gte": lambda s, v: s >= v,
        "lt": lambda s, v: s < v,
        "lte": lambda s, v: s <= v,
        "contains": lambda s, v: s.str.contains(v, case=False, na=False),
        "startswith": lambda s, v: s.str.startswith(v, na=False),
        "endswith": lambda s, v: s.str.endswith(v, na=False),
    }
    if op == "regex":
        try:
            return df[str_series.str.contains(val_str, case=False, na=False, regex=True)]
        except re.error:
            return df
    if op in str_ops:
        return df[str_ops[op](str_series, val_str)]
    return df


# ── Query pipeline ──

def _execute_query(df: pd.DataFrame, query: dict) -> tuple[pd.DataFrame, list[str]]:
    """Execute query pipeline: filter → group → sort → limit → columns.
    Returns (transformed_df, display_columns)."""
    # 1. Filters
    for f in query.get("filters", []):
        col, op, val = f.get("col", ""), f.get("op", ""), f.get("val", "")
        if col and op and val:
            df = _apply_single_filter(df, col, op, val)

    # 2. Deduplicate (unique by column, keep first or last)
    dedup = query.get("deduplicate")
    if dedup and dedup.get("col"):
        col = dedup["col"]
        keep = dedup.get("keep", "last")  # "first" or "last"
        if col == "__index__":
            df = df[~df.index.duplicated(keep=keep)]
        elif col in df.columns:
            df = df.drop_duplicates(subset=[col], keep=keep)

    # 3. Group by & aggregate
    gb = query.get("group_by")
    if gb and gb.get("col"):
        col = gb["col"]
        agg = gb.get("agg", "last")
        if col == "__index__":
            # Group by index — reset index first so it becomes a column
            idx_name = df.index.name or "index"
            df = df.reset_index()
            col = idx_name
        if col in df.columns:
            numeric_cols = df.select_dtypes(include="number").columns.tolist()
            if agg in ("last", "first"):
                df = getattr(df.groupby(col, sort=False), agg)()
            elif agg == "count":
                df = df.groupby(col, sort=False).size().reset_index(name="count")
            elif agg in ("sum", "mean", "min", "max", "median", "std"):
                if numeric_cols:
                    grouped = df.groupby(col, sort=False)
                    df = getattr(grouped[numeric_cols], agg)()
                else:
                    df = df.groupby(col, sort=False).first()
            df = df.reset_index()

    # 4. Sort
    sort = query.get("sort")
    if sort and sort.get("col"):
        ascending = sort.get("dir", "asc") != "desc"
        if sort["col"] == "__index__":
            df = df.sort_index(ascending=ascending)
        elif sort["col"] in df.columns:
            df = df.sort_values(by=sort["col"], ascending=ascending, na_position="last")

    # 5. Limit
    limit = query.get("limit")
    if limit and limit.get("n"):
        n = int(limit["n"])
        if limit.get("mode", "first") == "last":
            df = df.tail(n)
        else:
            df = df.head(n)

    # 6. Column select
    display_cols = list(df.columns)
    sel_cols = query.get("columns")
    if sel_cols:
        valid = [c for c in sel_cols if c in df.columns]
        if valid:
            df = df[valid]
            display_cols = valid

    return df, display_cols


def _parse_query(query_str: str) -> dict:
    if not query_str:
        return {}
    try:
        return json.loads(query_str)
    except (json.JSONDecodeError, TypeError):
        return {}


# ── Routes ──

@router.get("/libraries/{lib}/symbols/{sym:path}", response_class=HTMLResponse)
async def view_symbol(request: Request, lib: str, sym: str):
    try:
        desc = ops.get_description(lib, sym)
    except Exception as e:
        return HTMLResponse(f"<p class='text-danger'>Error: {e}</p>", status_code=404)

    cols_lower = {c.lower() for c in desc["columns"]}
    has_ohlc = all(k in cols_lower for k in ("open", "high", "low", "close"))
    has_volume = "volume" in cols_lower

    # Detect MultiIndex for contract charting
    is_multiindex = False
    index_names = []
    try:
        sample = ops.read_data(lib, sym, row_range=(0, 5))
        is_multiindex = _detect_multiindex_contracts(sample)
        if isinstance(sample.index, pd.MultiIndex):
            index_names = [n or f"level_{i}" for i, n in enumerate(sample.index.names)]
    except Exception:
        pass

    return templates.TemplateResponse("data_view.html", {
        "request": request,
        "library": lib,
        "symbol": sym,
        "description": desc,
        "page_size": DEFAULT_PAGE_SIZE,
        "has_ohlc": has_ohlc,
        "has_volume": has_volume,
        "is_multiindex": is_multiindex,
        "index_names": index_names,
    })


@router.get("/api/data/{lib}/{sym:path}", response_class=HTMLResponse)
async def get_data_table(
    request: Request,
    lib: str,
    sym: str,
    page: int = 0,
    page_size: int = DEFAULT_PAGE_SIZE,
    query: str = "",
):
    try:
        desc = ops.get_description(lib, sym)
        total_rows = desc["rows"]
        all_columns = desc["columns"]
        q = _parse_query(query)
        has_query = bool(q)

        if has_query:
            df = ops.read_data(lib, sym)
            df, display_cols = _execute_query(df, q)
            total_rows = len(df)

            # Paginate after query
            start = page * page_size
            df_page = df.iloc[start:start + page_size]
        else:
            row_range = (page * page_size, (page + 1) * page_size)
            df_page = ops.read_data(lib, sym, row_range=row_range)
            display_cols = all_columns

        total_pages = max(1, (total_rows + page_size - 1) // page_size)
        sort_info = q.get("sort", {})

        return templates.TemplateResponse("partials/data_table.html", {
            "request": request,
            "library": lib,
            "symbol": sym,
            "df": df_page,
            "columns": display_cols,
            "page": page,
            "page_size": page_size,
            "total_rows": total_rows,
            "total_pages": total_pages,
            "sort_col": sort_info.get("col", ""),
            "sort_dir": sort_info.get("dir", "asc"),
            "has_index": isinstance(df_page.index, pd.MultiIndex) or df_page.index.name is not None or not isinstance(df_page.index, pd.RangeIndex),
            "is_filtered": has_query,
        })
    except Exception as e:
        return HTMLResponse(f"<p class='text-danger'>Error loading data: {e}</p>", status_code=500)



# ── MultiIndex contract helpers ──

def _detect_multiindex_contracts(df: pd.DataFrame) -> bool:
    """Check if DataFrame has a MultiIndex with (date, localsymbol) structure."""
    if not isinstance(df.index, pd.MultiIndex):
        return False
    if df.index.nlevels != 2:
        return False
    # Check if first level looks like dates and second like strings
    try:
        level0 = df.index.get_level_values(0)
        level1 = df.index.get_level_values(1)
        return (pd.api.types.is_datetime64_any_dtype(level0) and
                level1.dtype == object)
    except Exception:
        return False


def _get_contract_names(df: pd.DataFrame) -> list[str]:
    """Get unique contract names from MultiIndex level 1."""
    if not isinstance(df.index, pd.MultiIndex):
        return []
    return sorted(df.index.get_level_values(1).unique().tolist())


def _extract_contract_by_rank(df: pd.DataFrame, rank: int, col: str = "close") -> pd.Series:
    """Extract a continuous front-month series by rank (1=front, 2=2nd front, etc.).
    For each date, ranks contracts by DTE ascending, picks the nth one."""
    if "dte" not in df.columns:
        # Fallback: rank by localsymbol alphabetically per date
        dates = df.index.get_level_values(0)
        contracts = df.index.get_level_values(1)
        result = {}
        for date in dates.unique():
            mask = dates == date
            date_contracts = contracts[mask]
            date_df = df.loc[mask]
            sorted_contracts = sorted(date_contracts.unique())
            if rank <= len(sorted_contracts):
                contract = sorted_contracts[rank - 1]
                result[date] = date_df.loc[(date, contract), col]
        return pd.Series(result, name=f"rank{rank}_{col}")

    # Rank by DTE
    result = {}
    dates = df.index.get_level_values(0)
    for date in dates.unique():
        date_slice = df.loc[date]
        if isinstance(date_slice, pd.Series):
            # Only one contract on this date
            if rank == 1:
                result[date] = date_slice[col] if col in date_slice.index else None
            continue
        # Filter out expired contracts (dte > 0)
        valid = date_slice[date_slice["dte"] > 0].sort_values("dte")
        if rank <= len(valid):
            result[date] = valid.iloc[rank - 1][col]
    return pd.Series(result, name=f"rank{rank}_{col}")


def _compute_spread(df: pd.DataFrame, rank1: int, rank2: int, col: str = "close") -> pd.Series:
    """Compute spread: rank1 - rank2 for a given column."""
    s1 = _extract_contract_by_rank(df, rank1, col)
    s2 = _extract_contract_by_rank(df, rank2, col)
    spread = s1 - s2
    spread.name = f"spread_rank{rank1}_rank{rank2}_{col}"
    return spread


def _build_continuous_series(
    df: pd.DataFrame,
    rank: int,
    col: str = "close",
    method: str = "back_diff",
    roll_rule: str = "expiry",
) -> pd.Series:
    """Build a continuous futures series from per-contract data.

    method:
        'none'       — spliced/unadjusted (jumps at rolls)
        'back_diff'  — back-adjusted by difference (preserves point P&L)
        'back_ratio' — back-adjusted by ratio (preserves % returns)
        'perpetual'  — constant-maturity blend of rank-1 and rank-2

    roll_rule:
        'expiry'     — switch when dte <= 0 (rank by dte ascending)
        'calendar_N' — roll N business days before expiry (dte > N)
        'volume'     — rank by volume descending (active contract)
    """
    if "dte" not in df.columns:
        return _extract_contract_by_rank(df, rank, col)

    if method == "perpetual":
        return _build_perpetual_series(df, col, roll_rule)

    calendar_offset = 0
    if roll_rule.startswith("calendar_"):
        try:
            calendar_offset = int(roll_rule.split("_", 1)[1])
        except (ValueError, IndexError):
            calendar_offset = 0

    use_volume = roll_rule == "volume" and "volume" in df.columns

    dates = sorted(df.index.get_level_values(0).unique())
    selections: list[tuple] = []
    for date in dates:
        try:
            date_slice = df.loc[date]
        except KeyError:
            continue
        if isinstance(date_slice, pd.Series):
            if rank == 1 and col in date_slice.index and pd.notna(date_slice[col]):
                selections.append((date, "_", float(date_slice[col])))
            continue

        valid = date_slice[date_slice["dte"] > 0]
        if len(valid) == 0:
            continue

        if use_volume:
            ranked = valid.sort_values("volume", ascending=False, na_position="last")
        elif calendar_offset > 0:
            filtered = valid[valid["dte"] > calendar_offset]
            ranked = filtered.sort_values("dte") if len(filtered) >= rank else valid.sort_values("dte")
        else:
            ranked = valid.sort_values("dte")

        if len(ranked) < rank:
            continue
        pick = ranked.iloc[rank - 1]
        price = pick[col]
        if pd.isna(price):
            continue
        selections.append((date, pick.name, float(price)))

    if not selections:
        return pd.Series([], dtype=float, name=f"cont{rank}_{col}_{method}")

    dates_arr = [s[0] for s in selections]
    contracts_arr = [s[1] for s in selections]
    prices_arr = [s[2] for s in selections]
    n = len(prices_arr)

    if method == "none":
        return pd.Series(prices_arr, index=dates_arr, name=f"cont{rank}_{col}_unadj")

    # Walk backward, accumulating roll adjustments
    adj = [0.0 if method == "back_diff" else 1.0] * n
    for i in range(n - 1, 0, -1):
        adj[i - 1] = adj[i]
        if contracts_arr[i] == contracts_arr[i - 1]:
            continue
        new_today = prices_arr[i]
        try:
            raw = df.loc[(dates_arr[i], contracts_arr[i - 1]), col]
            old_today = float(raw) if pd.notna(raw) else prices_arr[i - 1]
        except (KeyError, TypeError):
            old_today = prices_arr[i - 1]

        if method == "back_diff":
            adj[i - 1] = adj[i] + (new_today - old_today)
        elif method == "back_ratio" and old_today > 0 and new_today > 0:
            adj[i - 1] = adj[i] * (new_today / old_today)

    if method == "back_diff":
        result = [prices_arr[i] + adj[i] for i in range(n)]
        name = f"cont{rank}_{col}_backdiff"
    else:
        result = [prices_arr[i] * adj[i] for i in range(n)]
        name = f"cont{rank}_{col}_backratio"
    return pd.Series(result, index=dates_arr, name=name)


def _continuous_label(sym: str, rank: int, col: str, method: str) -> str:
    suffix = {
        "back_diff": " [adj-Δ]",
        "back_ratio": " [adj-r]",
        "perpetual": " [perp]",
    }.get(method, "")
    if method == "perpetual":
        return f"{sym} {col}{suffix}"
    return f"{sym}{rank} {col}{suffix}"


def _build_perpetual_series(df: pd.DataFrame, col: str = "close", roll_rule: str = "calendar_5") -> pd.Series:
    """Constant-maturity blend: w*front + (1-w)*deferred, w = min(1, dte_front / window)."""
    window = 5
    if roll_rule.startswith("calendar_"):
        try:
            window = max(1, int(roll_rule.split("_", 1)[1]))
        except (ValueError, IndexError):
            window = 5

    dates = sorted(df.index.get_level_values(0).unique())
    result: dict = {}
    for date in dates:
        try:
            date_slice = df.loc[date]
        except KeyError:
            continue
        if isinstance(date_slice, pd.Series):
            if col in date_slice.index and pd.notna(date_slice[col]):
                result[date] = float(date_slice[col])
            continue

        valid = date_slice[date_slice["dte"] > 0].sort_values("dte")
        if len(valid) == 0:
            continue
        if len(valid) == 1:
            v = valid.iloc[0][col]
            if pd.notna(v):
                result[date] = float(v)
            continue

        f_price, d_price = valid.iloc[0][col], valid.iloc[1][col]
        if pd.isna(f_price) or pd.isna(d_price):
            continue
        front_dte = float(valid.iloc[0]["dte"])
        if front_dte >= window:
            w = 1.0
        elif front_dte <= 0:
            w = 0.0
        else:
            w = front_dte / window
        result[date] = w * float(f_price) + (1 - w) * float(d_price)

    return pd.Series(result, name=f"perpetual_{col}")


def _compute_study(values: list, study_type: str, period: int) -> list:
    """Compute a study (MA or EMA) on a list of values."""
    s = pd.Series(values, dtype=float)
    if study_type == "sma":
        result = s.rolling(window=period, min_periods=1).mean()
    elif study_type == "ema":
        result = s.ewm(span=period, min_periods=1, adjust=False).mean()
    else:
        return values
    return [None if pd.isna(v) else round(v, 6) for v in result.tolist()]


def _detect_ohlc_cols(df: pd.DataFrame) -> dict[str, str] | None:
    """Try to auto-detect Open/High/Low/Close columns."""
    cols_lower = {c.lower(): c for c in df.columns}
    mapping = {}
    for key in ("open", "high", "low", "close"):
        if key in cols_lower:
            mapping[key] = cols_lower[key]
        else:
            return None
    return mapping


def _format_index_value(v) -> str:
    """Format an index value for the x-axis, handling Timestamps and MultiIndex tuples."""
    if isinstance(v, tuple):
        # MultiIndex tuple — use first element (typically date)
        return _format_index_value(v[0])
    if isinstance(v, pd.Timestamp):
        return v.strftime("%Y-%m-%d")
    s = str(v)
    # Clean up Timestamp(...) strings that slip through
    if s.startswith("Timestamp("):
        try:
            return pd.Timestamp(v).strftime("%Y-%m-%d")
        except Exception:
            pass
    return s[:10] if len(s) > 16 else s


def _resample_chart_data(df: pd.DataFrame, period: str) -> pd.DataFrame:
    """Resample data to a lower frequency. Period: W, M, Q, Y."""
    freq_map = {"W": "W", "M": "ME", "Q": "QE", "Y": "YE"}
    freq = freq_map.get(period)
    if not freq:
        return df

    if isinstance(df.index, pd.MultiIndex):
        # For MultiIndex (date, localsymbol): reset to use date level for resampling
        date_level = df.index.get_level_values(0)
        if not pd.api.types.is_datetime64_any_dtype(date_level):
            return df
        # Flatten: use date as index, keep only numeric columns
        flat = df.copy()
        flat.index = date_level
        numeric = flat.select_dtypes(include="number")
        if numeric.empty:
            return df
        # Group duplicate dates (multiple contracts per date), take last, then resample
        numeric = numeric.groupby(numeric.index).last()
        return numeric.resample(freq).last().dropna(how="all")

    if not isinstance(df.index, pd.DatetimeIndex):
        return df
    numeric = df.select_dtypes(include="number")
    if numeric.empty:
        return df
    return numeric.resample(freq).last().dropna(how="all")


def _build_chart_data(df: pd.DataFrame, x_col: str, y_cols_str: str, chart_type: str):
    """Build chart data dict for the template."""
    # X axis
    if x_col == "__index__" or not x_col:
        x_values = [_format_index_value(v) for v in df.index]
        x_label = df.index.name or "index"
        if isinstance(df.index, pd.MultiIndex):
            x_label = df.index.names[0] or "date"
    else:
        x_values = [_format_index_value(v) for v in df[x_col]]
        x_label = x_col

    # Subsample helper
    max_points = 2000
    if len(df) > max_points:
        step = max(1, len(df) // max_points)
        sample_idx = list(range(0, len(df), step))
    else:
        sample_idx = None

    if sample_idx:
        x_values = [x_values[i] for i in sample_idx]

    # Candlestick
    if chart_type == "candlestick":
        ohlc = _detect_ohlc_cols(df)
        if not ohlc:
            return None, "Candlestick requires Open, High, Low, Close columns."

        if sample_idx:
            candle_data = [{
                "x": x_values[j],
                "o": df[ohlc["open"]].iloc[i] if pd.notna(df[ohlc["open"]].iloc[i]) else None,
                "h": df[ohlc["high"]].iloc[i] if pd.notna(df[ohlc["high"]].iloc[i]) else None,
                "l": df[ohlc["low"]].iloc[i] if pd.notna(df[ohlc["low"]].iloc[i]) else None,
                "c": df[ohlc["close"]].iloc[i] if pd.notna(df[ohlc["close"]].iloc[i]) else None,
            } for j, i in enumerate(sample_idx)]
        else:
            candle_data = [{
                "x": x_values[i],
                "o": row[ohlc["open"]] if pd.notna(row[ohlc["open"]]) else None,
                "h": row[ohlc["high"]] if pd.notna(row[ohlc["high"]]) else None,
                "l": row[ohlc["low"]] if pd.notna(row[ohlc["low"]]) else None,
                "c": row[ohlc["close"]] if pd.notna(row[ohlc["close"]]) else None,
            } for i, (_, row) in enumerate(df.iterrows())]

        return {
            "x_values": x_values,
            "x_label": x_label,
            "chart_type": "candlestick",
            "datasets": [{"label": "OHLC", "data": candle_data}],
        }, None

    # Line / bar / scatter
    if y_cols_str:
        y_col_list = [c.strip() for c in y_cols_str.split(",") if c.strip() in df.columns]
    else:
        y_col_list = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])][:5]

    if sample_idx:
        datasets = [{
            "label": col,
            "data": [df[col].iloc[i] if pd.notna(df[col].iloc[i]) else None for i in sample_idx],
        } for col in y_col_list]
    else:
        datasets = [{
            "label": col,
            "data": [v if pd.notna(v) else None for v in df[col].tolist()],
        } for col in y_col_list]

    return {
        "x_values": x_values,
        "x_label": x_label,
        "chart_type": chart_type,
        "datasets": datasets,
    }, None


@router.get("/api/chart/{lib}/{sym:path}", response_class=HTMLResponse)
async def get_chart(
    request: Request,
    lib: str,
    sym: str,
    x_col: str = "",
    y_cols: str = "",
    chart_type: str = "line",
    query: str = "",
    subplots: str = "",
    contract_mode: str = "",
    contract_rank: int = 1,
    contract_col: str = "close",
    spread_rank1: int = 1,
    spread_rank2: int = 2,
    studies: str = "",
    period: str = "",
    continuous_method: str = "none",
    roll_rule: str = "expiry",
):
    try:
        df = ops.read_data(lib, sym)
        q = _parse_query(query)
        if q:
            df, _ = _execute_query(df, q)

        is_multi = _detect_multiindex_contracts(df)

        def _fmt_dates(idx):
            return [_format_index_value(v) for v in idx]

        def _series_data(s):
            return [v if pd.notna(v) else None for v in s.tolist()]

        def _apply_period_to_series(s):
            """Resample a pd.Series with DatetimeIndex to lower frequency."""
            if period and isinstance(s.index, pd.DatetimeIndex):
                freq_map = {"W": "W", "M": "ME", "Q": "QE", "Y": "YE"}
                freq = freq_map.get(period)
                if freq:
                    s = s.resample(freq).last().dropna()
            return s

        # Contract mode: extract single rank or spread
        if is_multi and contract_mode == "single":
            series = _build_continuous_series(df, contract_rank, contract_col, continuous_method, roll_rule)
            series = _apply_period_to_series(series)
            label = _continuous_label(sym, contract_rank, contract_col, continuous_method)
            datasets = [{"label": label, "data": _series_data(series)}]
            study_datasets = _build_study_datasets(datasets[0]["data"], studies)
            datasets.extend(study_datasets)

            main_data = {
                "x_values": _fmt_dates(series.index),
                "x_label": df.index.names[0] or "date",
                "chart_type": chart_type if chart_type != "candlestick" else "line",
                "datasets": datasets,
            }
            err = None

        elif is_multi and contract_mode == "spread":
            spread = _compute_spread(df, spread_rank1, spread_rank2, contract_col)
            spread = _apply_period_to_series(spread)
            data_vals = _series_data(spread)
            datasets = [{"label": f"{sym}{spread_rank1} - {sym}{spread_rank2} ({contract_col})", "data": data_vals}]
            study_datasets = _build_study_datasets(data_vals, studies)
            datasets.extend(study_datasets)

            main_data = {
                "x_values": _fmt_dates(spread.index),
                "x_label": df.index.names[0] or "date",
                "chart_type": chart_type if chart_type != "candlestick" else "line",
                "datasets": datasets,
            }
            err = None

        elif is_multi and contract_mode == "overlay":
            ranks = [spread_rank1, spread_rank2]
            datasets = []
            x_values = None
            for rank in ranks:
                series = _build_continuous_series(df, rank, contract_col, continuous_method, roll_rule)
                series = _apply_period_to_series(series)
                if x_values is None:
                    x_values = _fmt_dates(series.index)
                datasets.append({
                    "label": _continuous_label(sym, rank, contract_col, continuous_method),
                    "data": _series_data(series),
                })
            if x_values is None:
                x_values = []

            main_data = {
                "x_values": x_values,
                "x_label": df.index.names[0] or "date",
                "chart_type": "line",
                "datasets": datasets,
            }
            err = None

        else:
            # Standard chart — apply period resampling if requested
            if period:
                df = _resample_chart_data(df, period)

            main_data, err = _build_chart_data(df, x_col, y_cols, chart_type)

            # Apply studies to standard line charts
            if main_data and chart_type in ("line", "scatter") and studies:
                study_datasets = []
                for ds in main_data["datasets"]:
                    study_datasets.extend(_build_study_datasets(ds["data"], studies))
                main_data["datasets"].extend(study_datasets)

        if err:
            return HTMLResponse(f"<p class='text-warning'><i class='bi bi-exclamation-triangle'></i> {err}</p>")

        # Subplots
        subplot_list = []
        if subplots:
            try:
                sp_configs = json.loads(subplots)
                for sp in sp_configs:
                    sp_data, sp_err = _build_chart_data(
                        df, x_col, sp.get("y_cols", ""), sp.get("type", "bar")
                    )
                    if sp_data:
                        subplot_list.append(sp_data)
            except (json.JSONDecodeError, TypeError):
                pass

        return templates.TemplateResponse("partials/chart.html", {
            "request": request,
            "main_chart": json.dumps(main_data),
            "subplots": json.dumps(subplot_list),
            "chart_id": "main-chart",
        })
    except Exception as e:
        return HTMLResponse(f"<p class='text-danger'>Error: {e}</p>", status_code=500)


def _build_study_datasets(data: list, studies_str: str) -> list[dict]:
    """Parse studies string and compute study datasets."""
    if not studies_str:
        return []
    try:
        study_configs = json.loads(studies_str)
    except (json.JSONDecodeError, TypeError):
        return []
    datasets = []
    for sc in study_configs:
        s_type = sc.get("type", "sma")
        period = int(sc.get("period", 20))
        computed = _compute_study(data, s_type, period)
        label = f"{s_type.upper()}({period})"
        datasets.append({
            "label": label,
            "data": computed,
            "is_study": True,
        })
    return datasets


@router.get("/api/chart-info/{lib}/{sym:path}", response_class=JSONResponse)
async def get_chart_info(request: Request, lib: str, sym: str):
    """Return metadata about the symbol for chart UI configuration."""
    try:
        df = ops.read_data(lib, sym, row_range=(0, 10))
        is_multi = _detect_multiindex_contracts(df)
        contracts = []
        if is_multi:
            full_df = ops.read_data(lib, sym)
            contracts = _get_contract_names(full_df)
        return JSONResponse({
            "is_multiindex": is_multi,
            "contracts": contracts,
            "index_names": list(df.index.names) if isinstance(df.index, pd.MultiIndex) else [df.index.name],
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/sidepane/metadata/{lib}/{sym:path}", response_class=HTMLResponse)
async def sidepane_metadata(request: Request, lib: str, sym: str):
    """Try to load metadata from universe library. Returns metadata card or 404."""
    try:
        # Look for a symbol matching the library name (capitalized) in the "universe" library
        universe_symbol = lib.capitalize()
        if not ops.has_library("universe"):
            return HTMLResponse("", status_code=404)
        if not ops.has_symbol("universe", universe_symbol):
            # Try exact case
            universe_symbols = ops.list_symbols("universe")
            match = None
            for us in universe_symbols:
                if us.lower() == lib.lower():
                    match = us
                    break
            if not match:
                return HTMLResponse("", status_code=404)
            universe_symbol = match

        df = ops.read_data("universe", universe_symbol)
        # Find the row matching the symbol name (check common column names)
        match_row = None
        for col in ["ibkr_symbol", "symbol", "ticker", "name"]:
            if col in df.columns:
                matches = df[df[col].astype(str).str.upper() == sym.upper()]
                if len(matches) > 0:
                    match_row = matches.iloc[0]
                    break

        if match_row is None:
            return HTMLResponse("", status_code=404)

        # Convert to dict for template
        metadata = {k: v for k, v in match_row.to_dict().items() if pd.notna(v)}

        return templates.TemplateResponse("partials/sidepane_metadata.html", {
            "request": request,
            "library": lib,
            "symbol": sym,
            "universe_symbol": universe_symbol,
            "metadata": metadata,
        })
    except Exception:
        return HTMLResponse("", status_code=404)


@router.get("/api/sidepane/libraries", response_class=HTMLResponse)
async def sidepane_libraries(request: Request):
    """Return library list for the side pane."""
    try:
        libraries = sorted(ops.list_libraries())
        return templates.TemplateResponse("partials/sidepane_libraries.html", {
            "request": request,
            "libraries": libraries,
        })
    except Exception as e:
        return HTMLResponse(f"<p class='text-danger'>Error: {e}</p>", status_code=500)


@router.get("/api/sidepane/symbols/{lib}", response_class=HTMLResponse)
async def sidepane_symbols(request: Request, lib: str):
    """Return symbol list for the side pane."""
    try:
        symbols = sorted(ops.list_symbols(lib))
        return templates.TemplateResponse("partials/sidepane_symbols.html", {
            "request": request,
            "library": lib,
            "symbols": symbols,
        })
    except Exception as e:
        return HTMLResponse(f"<p class='text-danger'>Error: {e}</p>", status_code=500)


@router.get("/api/sidepane/data/{lib}/{sym:path}", response_class=HTMLResponse)
async def sidepane_data(
    request: Request,
    lib: str,
    sym: str,
    page: int = 0,
    page_size: int = 25,
):
    """Return data table for the side pane."""
    try:
        desc = ops.get_description(lib, sym)
        total_rows = desc["rows"]
        row_range = (page * page_size, (page + 1) * page_size)
        df_page = ops.read_data(lib, sym, row_range=row_range)
        total_pages = max(1, (total_rows + page_size - 1) // page_size)

        return templates.TemplateResponse("partials/sidepane_datatable.html", {
            "request": request,
            "library": lib,
            "symbol": sym,
            "df": df_page,
            "columns": desc["columns"],
            "page": page,
            "page_size": page_size,
            "total_rows": total_rows,
            "total_pages": total_pages,
            "has_index": isinstance(df_page.index, pd.MultiIndex) or df_page.index.name is not None or not isinstance(df_page.index, pd.RangeIndex),
        })
    except Exception as e:
        return HTMLResponse(f"<p class='text-danger'>Error: {e}</p>", status_code=500)


@router.put("/api/data/{lib}/{sym:path}", response_class=HTMLResponse)
async def edit_cell(request: Request, lib: str, sym: str):
    form = await request.form()
    row_idx = int(form["row_idx"])
    col_name = form["col_name"]
    new_value = form["value"]

    try:
        df = ops.read_data(lib, sym)
        original_dtype = df[col_name].dtype
        if pd.api.types.is_numeric_dtype(original_dtype):
            parsed = _parse_value(new_value)
            if isinstance(parsed, float):
                new_value = int(parsed) if pd.api.types.is_integer_dtype(original_dtype) else parsed

        df.at[df.index[row_idx], col_name] = new_value
        ops.write_data(lib, sym, df)

        return HTMLResponse(
            content=str(new_value),
            headers={"HX-Trigger": '{"showToast":{"message":"Cell updated successfully"}}'},
        )
    except Exception as e:
        return HTMLResponse(
            content=str(e),
            headers={"HX-Trigger": f'{{"showToast":{{"message":"Error: {e}","type":"error"}}}}'},
            status_code=400,
        )


@router.delete("/api/rows/{lib}/{sym:path}", response_class=HTMLResponse)
async def delete_rows(request: Request, lib: str, sym: str):
    form = await request.form()
    row_indices = json.loads(form["row_indices"])

    try:
        df = ops.read_data(lib, sym)
        df = df.drop(df.index[row_indices])
        ops.write_data(lib, sym, df)

        return HTMLResponse(
            content="",
            headers={"HX-Trigger": '{"showToast":{"message":"Rows deleted successfully"},"refreshTable":"true"}'},
        )
    except Exception as e:
        return HTMLResponse(
            content="",
            headers={"HX-Trigger": f'{{"showToast":{{"message":"Error: {e}","type":"error"}}}}'},
            status_code=400,
        )


@router.post("/api/symbol/{lib}/create")
async def create_symbol(request: Request, lib: str):
    try:
        body = await request.json()
        symbol = body.get("symbol", "").strip()
        index_type = body.get("index_type", "none")
        index_name = body.get("index_name")
        columns = body.get("columns", [])

        if not symbol:
            return HTMLResponse("Symbol name is required", status_code=400)
        if not columns:
            return HTMLResponse("At least one column is required", status_code=400)

        # Build dtype mapping
        dtype_map = {"float": "float64", "int": "int64", "str": "object", "bool": "bool"}
        col_dict = {}
        for col in columns:
            dtype = dtype_map.get(col["type"], "object")
            col_dict[col["name"]] = pd.Series(dtype=dtype)

        df = pd.DataFrame(col_dict)

        # Set up index
        if index_type == "datetime":
            idx_name = index_name or "timestamp"
            df.index = pd.DatetimeIndex([], name=idx_name)
        elif index_type == "integer":
            idx_name = index_name or "index"
            df.index = pd.Index([], dtype="int64", name=idx_name)

        ops.write_data(lib, symbol, df)
        return HTMLResponse(content="OK", status_code=200)
    except Exception as e:
        return HTMLResponse(content=str(e), status_code=400)


@router.post("/api/addrow/{lib}/{sym:path}")
async def add_row(request: Request, lib: str, sym: str):
    try:
        body = await request.json()
        df = ops.read_data(lib, sym)

        # Build new row with correct dtypes
        new_row = {}
        for col in df.columns:
            val = body.get(col, "")
            if val == "":
                new_row[col] = None
            elif pd.api.types.is_numeric_dtype(df[col]):
                parsed = _parse_value(val)
                new_row[col] = parsed if isinstance(parsed, float) else None
            else:
                new_row[col] = val

        # Handle index
        new_df = pd.DataFrame([new_row], columns=df.columns)
        if isinstance(df.index, pd.DatetimeIndex):
            idx_val = body.get(df.index.name or "index", "")
            if idx_val:
                new_df.index = pd.DatetimeIndex([_parse_timestamp(idx_val)], name=df.index.name)
            else:
                new_df.index = pd.DatetimeIndex([pd.Timestamp.now()], name=df.index.name)
        elif df.index.name and df.index.name in body:
            new_df.index = pd.Index([body[df.index.name]], name=df.index.name)

        combined = pd.concat([df, new_df])
        if isinstance(combined.index, pd.DatetimeIndex):
            combined = combined.sort_index()
        ops.write_data(lib, sym, combined)

        return HTMLResponse(content="OK", status_code=200)
    except Exception as e:
        return HTMLResponse(content=str(e), status_code=400)


@router.post("/api/data/{lib}/upload", response_class=HTMLResponse)
async def upload_csv(request: Request, lib: str, symbol: str = Form(...), file: UploadFile = File(...)):
    try:
        content = await file.read()
        df = pd.read_csv(io.BytesIO(content))
        ops.write_data(lib, symbol, df)

        symbols = sorted(ops.list_symbols(lib))
        return templates.TemplateResponse("partials/symbol_list.html", {
            "request": request,
            "library": lib,
            "symbols": symbols,
        }, headers={"HX-Trigger": f'{{"showToast":{{"message":"Uploaded {symbol} ({len(df)} rows)"}}}}'})
    except Exception as e:
        return HTMLResponse(
            content="",
            headers={"HX-Trigger": f'{{"showToast":{{"message":"Error: {e}","type":"error"}}}}'},
            status_code=400,
        )
