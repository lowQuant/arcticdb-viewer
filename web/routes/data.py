import io
import json
import re

import pandas as pd
from fastapi import APIRouter, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse

from core import operations as ops
from web.app import templates

router = APIRouter()

DEFAULT_PAGE_SIZE = 50


# ── Value parsing ──

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

    if is_index:
        series = df.index.to_series()
        if isinstance(df.index, pd.DatetimeIndex):
            try:
                if op == "between":
                    parts = [p.strip() for p in val.split(",")]
                    if len(parts) == 2:
                        return df[(series >= pd.Timestamp(parts[0])) & (series <= pd.Timestamp(parts[1]))]
                    return df
                parsed_ts = pd.Timestamp(val)
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

    return templates.TemplateResponse("data_view.html", {
        "request": request,
        "library": lib,
        "symbol": sym,
        "description": desc,
        "page_size": DEFAULT_PAGE_SIZE,
        "has_ohlc": has_ohlc,
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
            "has_index": df_page.index.name is not None or not isinstance(df_page.index, pd.RangeIndex),
            "is_filtered": has_query,
        })
    except Exception as e:
        return HTMLResponse(f"<p class='text-danger'>Error loading data: {e}</p>", status_code=500)



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


def _build_chart_data(df: pd.DataFrame, x_col: str, y_cols_str: str, chart_type: str):
    """Build chart data dict for the template."""
    # X axis
    if x_col == "__index__" or not x_col:
        x_values = [str(v) for v in df.index]
        x_label = df.index.name or "index"
    else:
        x_values = [str(v) for v in df[x_col]]
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
):
    try:
        df = ops.read_data(lib, sym)
        q = _parse_query(query)
        if q:
            df, _ = _execute_query(df, q)

        # Main chart
        main_data, err = _build_chart_data(df, x_col, y_cols, chart_type)
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
                new_df.index = pd.DatetimeIndex([pd.Timestamp(idx_val)], name=df.index.name)
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
