# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

arcticdb-viewer is a web-based data browser and MCP server for ArcticDB, a serverless DataFrame database. It provides a Bootstrap 5 + HTMX web UI for browsing/editing data and an MCP server for LLM integration. Supports LMDB (local), S3 (cloud), and in-memory backends.

## Architecture

Three-layer design with a shared core:

```
core/              Shared ArcticDB CRUD operations (pure Python)
  connection.py    ConnectionManager: multi-instance, save/load ~/.adbview/connections.json
  operations.py    All library/symbol/data functions used by both web and MCP
  settings.py      Read-only mode + write-guard (single chokepoint)
  analysis.py      Read-only analytics (describe/correlation/quality/returns) — pure pandas/numpy

web/               FastAPI + Jinja2 + HTMX + Bootstrap 5 (dark + light themes)
  app.py           FastAPI app, connection middleware, template globals, ReadOnlyError handler
  routes/          connections.py, libraries.py, symbols.py, data.py, analysis.py
  templates/       Server-rendered HTML with HTMX partials

mcp_server/        MCP server (read + analysis tools always; write tools gated by read-only)
  server.py        Uses mcp.server.fastmcp.FastMCP, supports stdio + SSE transport

run_web.py         Web UI entry point (uvicorn, port 8000)
run_mcp.py         MCP server entry point (--transport stdio|sse), auto-connects from .env
```

The core layer is the single source of truth for all ArcticDB interactions. Both the web UI and MCP server import from `core.operations`. Never access ArcticDB directly from routes or MCP tools.

## Read-only mode (safe by default)

This viewer is meant for production market data, so **writes are disabled by default**. The model:

- `core/settings.py` resolves `is_read_only()` from env `ADBVIEW_READONLY` (default ON; only `0/false/no/off` enables writes). `guard_write(op)` raises `ReadOnlyError`.
- Every mutating function in `core/operations.py` calls `guard_write(...)` first — the single chokepoint protects web routes **and** MCP tools alike. Never add a write path that bypasses it.
- Web: `templates.env.globals["read_only"]` gates all destructive UI (edit/add/delete/upload). Mutation routes `except ReadOnlyError: raise` so the central `@app.exception_handler(ReadOnlyError)` returns a 403 toast. `ReadOnlyError`'s message is ASCII-only because it can land in an `HX-Trigger` header (latin-1).
- MCP: write tools are only registered when `not is_read_only()` (see `_WRITE_TOOLS` in `mcp_server/server.py`); read + analysis tools are always present.
- Navbar shows a `READ-ONLY` badge; `JS const READ_ONLY` short-circuits `editCell`.

## Analysis features (all read-only)

`core/analysis.py` holds pure functions adapted from dtale, surfaced via `web/routes/analysis.py` (HTMX partials in `templates/partials/analysis_*.html`) and as MCP tools:
- `describe_frame` (per-column stats incl. skew/kurtosis), `column_analysis` (numpy histogram / value-counts + IQR outliers), `correlation_matrix` (pearson/spearman/kendall), `quality_report` (missing/dup/constant + `timeseries_gaps` for calendar holes/monotonicity), `returns_stats` (CAGR, ann. vol, Sharpe, max drawdown, hit-rate — period inferred from index spacing).
- All analysis endpoints accept the same `query` param as the data table and apply `_execute_query`, so analysis matches the on-screen view. Export (`/api/export/{csv,parquet}/...`) and code-export (`/api/code/...`, reproducible pandas) are likewise read-only and query-aware.

## Connection Management

- Web UI: welcome page at `/` with connection manager. Supports LMDB, S3, auto-detect from .env
- Saved connections stored in `~/.adbview/connections.json` (schema: `{connections: [{name, type, uri}], last_used}`)
- Active connection tracked via in-memory `ConnectionManager` singleton + `adbview_connection` cookie for auto-reconnect
- Middleware in `web/app.py` redirects to `/` if not connected (except `OPEN_PATHS` + anything under `/connections/`)
- MCP server connection fallback chain (`run_mcp.py`): `--connection NAME` → `.env` S3 vars → `last_used` saved connection → first saved connection. Exits if none available.

## Running the App

```bash
pip install -r requirements.txt                # Python 3.10+ required
python run_web.py                              # Web UI at http://localhost:8000 (uvicorn --reload)
python run_mcp.py --transport stdio            # MCP for local LLM clients
python run_mcp.py --transport sse --port 8001  # MCP for remote access
python run_mcp.py --connection NAME            # Use a saved connection instead of .env
```

## Key Conventions

- **NEVER read or commit .env files** - they contain AWS credentials
- `.env` vars for S3: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `BUCKET_NAME`, `AWS_REGION`
- Web UI uses HTMX partial swaps - routes return full pages for navigation, fragments for HTMX
- Data view has a query builder pipeline: filter → deduplicate → group → sort → limit → columns → paginate
- Pagination uses ArcticDB's native `row_range` for unqueried views; query operations do full reads
- MCP write tools accept CSV strings (parsed via `pd.read_csv(io.StringIO(...))`)
- Bootstrap 5.3 (`data-bs-theme` dark/light, persisted to `localStorage`) + Bootstrap Icons + Chart.js via CDN, no JS build step
- Route path conflicts: `/api/data/{lib}/{sym:path}` is greedy — put specific routes like `/api/chart/`, `/api/rows/`, `/api/addrow/`, `/api/sidepane/` on separate prefixes
- Filter values support `10^6` (caret = exponent), `2.5e3`, `1_000_000`, ISO dates, and `dayfirst=True` European dates (`DD.MM.YYYY`)
- MultiIndex symbols with `(date, localsymbol)` get a "contract mode" chart UI: rank-based front-month extraction, spread (rank1 − rank2), overlay; uses a `dte` column to rank if present, else alphabetical
- Side-pane metadata convention: data view looks for a symbol matching the library name (e.g. library `futures` → symbol `Futures`) in a `universe` library, matched against rows by `ibkr_symbol` / `symbol` / `ticker` / `name`
- Mutations return empty bodies with HTMX `HX-Trigger` headers for toasts and table refreshes — don't replace this with JSON responses

## ArcticDB API Quick Reference

```python
# Arctic level (from core.connection.get_arctic())
ac.list_libraries() / create_library(name) / delete_library(name)

# Library level (ac[library_name])
lib.list_symbols() / read(symbol, row_range=, columns=) / write(symbol, df)
lib.update(symbol, df) / append(symbol, df) / delete(symbol)
lib.get_description(symbol)  # returns NameWithDType objects (use .name, .dtype)
```

Docs: https://docs.arcticdb.io/latest/
