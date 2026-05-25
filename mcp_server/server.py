from __future__ import annotations

import io
import os
from typing import Any

import pandas as pd
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from core import operations as ops

load_dotenv()

mcp = FastMCP("ArcticDB", json_response=True)


# ── Library tools ──

@mcp.tool()
def list_libraries() -> list[str]:
    """List all libraries in the ArcticDB instance."""
    return ops.list_libraries()


@mcp.tool()
def create_library(name: str) -> str:
    """Create a new library."""
    ops.create_library(name)
    return f"Library '{name}' created successfully."


@mcp.tool()
def delete_library(name: str) -> str:
    """Delete a library and all its data. This cannot be undone."""
    ops.delete_library(name)
    return f"Library '{name}' deleted."


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


@mcp.tool()
def write_data(library: str, symbol: str, csv_data: str) -> str:
    """Write data to a symbol (overwrites existing). Data should be CSV format.

    Args:
        library: Library name
        symbol: Symbol name
        csv_data: Data in CSV format (with header row)
    """
    df = pd.read_csv(io.StringIO(csv_data))
    ops.write_data(library, symbol, df)
    return f"Written {len(df)} rows to '{symbol}' in '{library}'."


@mcp.tool()
def update_data(library: str, symbol: str, csv_data: str) -> str:
    """Update existing data in a symbol. Overwrites rows in the date range of the provided data.

    Args:
        library: Library name
        symbol: Symbol name
        csv_data: Data in CSV format (with header row)
    """
    df = pd.read_csv(io.StringIO(csv_data))
    ops.update_data(library, symbol, df)
    return f"Updated '{symbol}' with {len(df)} rows."


@mcp.tool()
def append_data(library: str, symbol: str, csv_data: str) -> str:
    """Append rows to an existing symbol.

    Args:
        library: Library name
        symbol: Symbol name
        csv_data: Data in CSV format (with header row)
    """
    df = pd.read_csv(io.StringIO(csv_data))
    ops.append_data(library, symbol, df)
    return f"Appended {len(df)} rows to '{symbol}'."


@mcp.tool()
def delete_symbol(library: str, symbol: str) -> str:
    """Delete a symbol and all its data. This cannot be undone."""
    ops.delete_symbol(library, symbol)
    return f"Symbol '{symbol}' deleted from '{library}'."


def run(transport: str = "stdio", host: str = "0.0.0.0", port: int = 8001):
    """Run the MCP server with the specified transport."""
    if transport == "stdio":
        mcp.run(transport="stdio")
    elif transport == "sse":
        mcp.run(transport="sse", host=host, port=port)
    else:
        raise ValueError(f"Unknown transport: {transport}. Use 'stdio' or 'sse'.")
