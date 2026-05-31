from __future__ import annotations

import io
import os
from typing import Any

import pandas as pd
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from core import analysis as an
from core import operations as ops
from core.settings import is_read_only

load_dotenv()

mcp = FastMCP("ArcticDB", json_response=True)


# ── Library tools ──

@mcp.tool()
def list_libraries() -> list[str]:
    """List all libraries in the ArcticDB instance."""
    return ops.list_libraries()


# create_library / delete_library are write operations — they are registered
# conditionally below (only when not in read-only mode).
def _create_library(name: str) -> str:
    """Create a new library."""
    ops.create_library(name)
    return f"Library '{name}' created successfully."


# ── Symbol tools ──

@mcp.tool()
def list_symbols(library: str) -> list[str]:
    """List all symbols in a library."""
    return ops.list_symbols(library)


@mcp.tool()
def describe_symbol(library: str, symbol: str) -> dict[str, Any]:
    """Get metadata about a symbol: row count, columns, dtypes."""
    return ops.get_description(library, symbol)


@mcp.tool()
def read_data(
    library: str,
    symbol: str,
    rows: int = 100,
    offset: int = 0,
    columns: list[str] | None = None,
) -> dict[str, Any]:
    """Read data from a symbol with pagination. Returns JSON records.

    Args:
        library: Library name
        symbol: Symbol name
        rows: Number of rows to read (default 100)
        offset: Row offset to start from (default 0)
        columns: Optional list of column names to include
    """
    df = ops.read_data(library, symbol, row_range=(offset, offset + rows), columns=columns)
    return {
        "columns": list(df.columns),
        "index": [str(i) for i in df.index],
        "data": df.to_dict(orient="records"),
        "row_count": len(df),
    }


# ── Read-only analysis tools (always available) ──

@mcp.tool()
def describe_statistics(library: str, symbol: str) -> dict[str, Any]:
    """Per-column summary statistics (count, missing, distinct, mean, std,
    min, quartiles, max, skew, kurtosis). Read-only."""
    df = ops.read_data(library, symbol)
    return {"columns": an.describe_frame(df), "rows": len(df)}


@mcp.tool()
def correlation_matrix(library: str, symbol: str, method: str = "pearson") -> dict[str, Any]:
    """Correlation matrix of numeric columns. method: pearson|spearman|kendall. Read-only."""
    df = ops.read_data(library, symbol)
    return an.correlation_matrix(df, method)


@mcp.tool()
def column_distribution(library: str, symbol: str, column: str) -> dict[str, Any]:
    """Distribution of a single column: histogram or value counts, plus IQR
    outlier detection for numeric columns. Read-only."""
    df = ops.read_data(library, symbol)
    return an.column_analysis(df, column)


@mcp.tool()
def data_quality_report(library: str, symbol: str) -> dict[str, Any]:
    """Data-quality summary: missing cells, duplicate rows/index, constant
    columns, and time-series gap/monotonicity checks. Read-only."""
    df = ops.read_data(library, symbol)
    return an.quality_report(df)


@mcp.tool()
def returns_summary(library: str, symbol: str, column: str = "close") -> dict[str, Any]:
    """Risk/return statistics for a price column: total return, CAGR,
    annualised volatility, Sharpe, max drawdown, hit-rate. Read-only."""
    df = ops.read_data(library, symbol)
    return an.returns_stats(df, column)


@mcp.tool()
def signal_analysis(library: str, symbol: str, signal_column: str,
                    price_column: str = "close", bucket_horizon: int = 1) -> dict[str, Any]:
    """Evaluate a predictive signal: Information Coefficient (rank/Pearson
    correlation of the signal with forward returns) across horizons, plus
    mean forward return per signal quantile bucket. Read-only."""
    df = ops.read_data(library, symbol)
    return an.signal_analysis(df, signal_column, price_column, bucket_horizon=bucket_horizon)


# ── Write tools (only registered when not in read-only mode) ──

def _write_data(library: str, symbol: str, csv_data: str) -> str:
    """Write data to a symbol (overwrites existing). Data should be CSV format.

    Args:
        library: Library name
        symbol: Symbol name
        csv_data: Data in CSV format (with header row)
    """
    df = pd.read_csv(io.StringIO(csv_data))
    ops.write_data(library, symbol, df)
    return f"Written {len(df)} rows to '{symbol}' in '{library}'."


def _update_data(library: str, symbol: str, csv_data: str) -> str:
    """Update existing data in a symbol. Overwrites rows in the date range of the provided data.

    Args:
        library: Library name
        symbol: Symbol name
        csv_data: Data in CSV format (with header row)
    """
    df = pd.read_csv(io.StringIO(csv_data))
    ops.update_data(library, symbol, df)
    return f"Updated '{symbol}' with {len(df)} rows."


def _append_data(library: str, symbol: str, csv_data: str) -> str:
    """Append rows to an existing symbol.

    Args:
        library: Library name
        symbol: Symbol name
        csv_data: Data in CSV format (with header row)
    """
    df = pd.read_csv(io.StringIO(csv_data))
    ops.append_data(library, symbol, df)
    return f"Appended {len(df)} rows to '{symbol}'."


def _delete_symbol(library: str, symbol: str) -> str:
    """Delete a symbol and all its data. This cannot be undone."""
    ops.delete_symbol(library, symbol)
    return f"Symbol '{symbol}' deleted from '{library}'."


def _delete_library(name: str) -> str:
    """Delete a library and all its data. This cannot be undone."""
    ops.delete_library(name)
    return f"Library '{name}' deleted."


_WRITE_TOOLS = [_create_library, _delete_library, _write_data, _update_data,
                _append_data, _delete_symbol]

if not is_read_only():
    for _fn in _WRITE_TOOLS:
        # Re-expose under the public name (strip the leading underscore).
        _fn.__name__ = _fn.__name__.lstrip("_")
        mcp.tool()(_fn)


def run(transport: str = "stdio", host: str = "0.0.0.0", port: int = 8001):
    """Run the MCP server with the specified transport."""
    if transport == "stdio":
        mcp.run(transport="stdio")
    elif transport == "sse":
        mcp.run(transport="sse", host=host, port=port)
    else:
        raise ValueError(f"Unknown transport: {transport}. Use 'stdio' or 'sse'.")
