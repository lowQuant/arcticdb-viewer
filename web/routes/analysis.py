"""Read-only analysis, export, and code-export routes.

These endpoints never write to ArcticDB. They read a symbol, optionally
apply the active query pipeline (so analysis matches what's on screen), and
return either an HTMX partial or a downloadable file.
"""
import io
import json

import pandas as pd
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from core import analysis as an
from core import operations as ops
from web.app import templates
from web.routes.data import _parse_query, _execute_query

router = APIRouter()


def _read_view(lib: str, sym: str, query: str) -> pd.DataFrame:
    """Read a symbol and apply the active query pipeline if present."""
    df = ops.read_data(lib, sym)
    q = _parse_query(query)
    if q:
        df, _ = _execute_query(df, q)
    return df


# ── Describe ──

@router.get("/api/analysis/describe/{lib}/{sym:path}", response_class=HTMLResponse)
async def analysis_describe(request: Request, lib: str, sym: str, query: str = ""):
    try:
        df = _read_view(lib, sym, query)
        return templates.TemplateResponse("partials/analysis_describe.html", {
            "request": request,
            "rows": an.describe_frame(df),
            "n_rows": len(df),
        })
    except Exception as e:
        return HTMLResponse(f"<p class='text-danger'>Error: {e}</p>", status_code=500)


# ── Column distribution ──

@router.get("/api/analysis/column/{lib}/{sym:path}", response_class=HTMLResponse)
async def analysis_column(request: Request, lib: str, sym: str, col: str = "", query: str = ""):
    try:
        df = _read_view(lib, sym, query)
        if not col:
            num = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
            col = num[0] if num else (df.columns[0] if len(df.columns) else "")
        report = an.column_analysis(df, col)
        return templates.TemplateResponse("partials/analysis_column.html", {
            "request": request,
            "library": lib,
            "symbol": sym,
            "columns": [str(c) for c in df.columns],
            "selected": col,
            "report": report,
            "report_json": json.dumps(report),
        })
    except KeyError:
        return HTMLResponse("<p class='text-warning'>Column not found.</p>")
    except Exception as e:
        return HTMLResponse(f"<p class='text-danger'>Error: {e}</p>", status_code=500)


# ── Correlation ──

@router.get("/api/analysis/correlation/{lib}/{sym:path}", response_class=HTMLResponse)
async def analysis_correlation(request: Request, lib: str, sym: str, method: str = "pearson", query: str = ""):
    try:
        df = _read_view(lib, sym, query)
        result = an.correlation_matrix(df, method)
        return templates.TemplateResponse("partials/analysis_correlation.html", {
            "request": request,
            "method": method,
            "result": result,
            "result_json": json.dumps(result),
        })
    except Exception as e:
        return HTMLResponse(f"<p class='text-danger'>Error: {e}</p>", status_code=500)


# ── Data quality ──

@router.get("/api/analysis/quality/{lib}/{sym:path}", response_class=HTMLResponse)
async def analysis_quality(request: Request, lib: str, sym: str, query: str = ""):
    try:
        df = _read_view(lib, sym, query)
        return templates.TemplateResponse("partials/analysis_quality.html", {
            "request": request,
            "report": an.quality_report(df),
        })
    except Exception as e:
        return HTMLResponse(f"<p class='text-danger'>Error: {e}</p>", status_code=500)


# ── Returns / risk ──

@router.get("/api/analysis/returns/{lib}/{sym:path}", response_class=HTMLResponse)
async def analysis_returns(request: Request, lib: str, sym: str, col: str = "", query: str = ""):
    try:
        df = _read_view(lib, sym, query)
        num = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
        if not col:
            # Prefer a 'close' column for price series.
            lower = {c.lower(): c for c in num}
            col = lower.get("close") or lower.get("price") or (num[0] if num else "")
        stats = an.returns_stats(df, col) if col else {"error": "No numeric column available."}
        return templates.TemplateResponse("partials/analysis_returns.html", {
            "request": request,
            "library": lib,
            "symbol": sym,
            "columns": num,
            "selected": col,
            "stats": stats,
        })
    except Exception as e:
        return HTMLResponse(f"<p class='text-danger'>Error: {e}</p>", status_code=500)


# ── Export (CSV / Parquet) ──

@router.get("/api/export/{fmt}/{lib}/{sym:path}")
async def export_view(lib: str, sym: str, fmt: str, query: str = ""):
    try:
        df = _read_view(lib, sym, query)
    except Exception as e:
        return HTMLResponse(f"Error: {e}", status_code=500)

    safe = sym.replace("/", "_")
    if fmt == "csv":
        buf = io.StringIO()
        df.to_csv(buf)
        return StreamingResponse(
            io.BytesIO(buf.getvalue().encode()),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{safe}.csv"'},
        )
    if fmt == "parquet":
        try:
            buf = io.BytesIO()
            df.to_parquet(buf)
        except Exception as e:
            return HTMLResponse(
                f"Parquet export needs pyarrow installed: {e}", status_code=400
            )
        buf.seek(0)
        return StreamingResponse(
            buf,
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{safe}.parquet"'},
        )
    return HTMLResponse("Unknown format", status_code=400)


# ── Code export (reproducible pandas/arcticdb) ──

@router.get("/api/code/{lib}/{sym:path}", response_class=HTMLResponse)
async def code_export(request: Request, lib: str, sym: str, query: str = ""):
    q = _parse_query(query)
    code = _build_code(lib, sym, q)
    return templates.TemplateResponse("partials/code_export.html", {
        "request": request,
        "code": code,
    })


def _py(v) -> str:
    return repr(v)


def _build_code(lib: str, sym: str, q: dict) -> str:
    """Generate a standalone pandas snippet reproducing the current view."""
    lines = [
        "import pandas as pd",
        "from arcticdb import Arctic",
        "",
        "# Connect (fill in your URI) and read the symbol",
        "ac = Arctic(<your-uri>)",
        f"lib = ac[{_py(lib)}]",
        f"df = lib.read({_py(sym)}).data",
        "",
    ]
    if not q:
        lines.append("# No query pipeline active — full symbol as read.")
        return "\n".join(lines)

    for f in q.get("filters", []):
        col, op, val = f.get("col"), f.get("op"), f.get("val")
        lines.append(f"# filter: {col} {op} {val}")
        if col == "__index__":
            target = "df.index"
        else:
            target = f"df[{_py(col)}]"
        cmp = {"eq": "==", "neq": "!=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}.get(op)
        if cmp:
            lines.append(f"df = df[{target} {cmp} {_py(val)}]")
        elif op == "between":
            parts = [p.strip() for p in str(val).split(",")]
            if len(parts) == 2:
                lines.append(f"df = df[({target} >= {_py(parts[0])}) & ({target} <= {_py(parts[1])})]")
        elif op == "contains":
            lines.append(f"df = df[{target}.astype(str).str.contains({_py(val)}, case=False, na=False)]")
        elif op == "regex":
            lines.append(f"df = df[{target}.astype(str).str.contains({_py(val)}, case=False, na=False, regex=True)]")
        elif op in ("startswith", "endswith"):
            lines.append(f"df = df[{target}.astype(str).str.{op}({_py(val)})]")

    dedup = q.get("deduplicate")
    if dedup and dedup.get("col"):
        keep = dedup.get("keep", "last")
        if dedup["col"] == "__index__":
            lines.append(f"df = df[~df.index.duplicated(keep={_py(keep)})]")
        else:
            lines.append(f"df = df.drop_duplicates(subset=[{_py(dedup['col'])}], keep={_py(keep)})")

    gb = q.get("group_by")
    if gb and gb.get("col"):
        agg = gb.get("agg", "last")
        col = "df.index" if gb["col"] == "__index__" else _py(gb["col"])
        if agg == "count":
            lines.append(f"df = df.groupby({col}, sort=False).size().reset_index(name='count')")
        else:
            lines.append(f"df = df.groupby({col}, sort=False).agg({_py(agg)}).reset_index()")

    sort = q.get("sort")
    if sort and sort.get("col"):
        asc = sort.get("dir", "asc") != "desc"
        if sort["col"] == "__index__":
            lines.append(f"df = df.sort_index(ascending={asc})")
        else:
            lines.append(f"df = df.sort_values({_py(sort['col'])}, ascending={asc})")

    limit = q.get("limit")
    if limit and limit.get("n"):
        n = int(limit["n"])
        lines.append(f"df = df.{'tail' if limit.get('mode') == 'last' else 'head'}({n})")

    cols = q.get("columns")
    if cols:
        lines.append(f"df = df[{_py(list(cols))}]")

    return "\n".join(lines)
