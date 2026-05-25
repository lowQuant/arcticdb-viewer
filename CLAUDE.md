# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

ADBView is a web-based data browser and MCP server for ArcticDB, a serverless DataFrame database. It provides a Bootstrap 5 + HTMX web UI for browsing/editing data and an MCP server for LLM integration. Supports LMDB (local), S3 (cloud), and in-memory backends.

## Architecture

Three-layer design with a shared core:

```
core/              Shared ArcticDB CRUD operations (pure Python)
  connection.py    ConnectionManager: multi-instance, save/load ~/.adbview/connections.json
  operations.py    All library/symbol/data functions used by both web and MCP

web/               FastAPI + Jinja2 + HTMX + Bootstrap 5 (dark theme)
  app.py           FastAPI app, connection middleware, template globals
  routes/          connections.py, libraries.py, symbols.py, data.py
  templates/       Server-rendered HTML with HTMX partials

mcp_server/        MCP server with 10 CRUD tools
  server.py        Uses mcp.server.fastmcp.FastMCP, supports stdio + SSE transport

run_web.py         Web UI entry point (uvicorn, port 8000)
run_mcp.py         MCP server entry point (--transport stdio|sse), auto-connects from .env
```

The core layer is the single source of truth for all ArcticDB interactions. Both the web UI and MCP server import from `core.operations`. Never access ArcticDB directly from routes or MCP tools.

## Connection Management

- Web UI: welcome page at `/` with connection manager. Supports LMDB, S3, auto-detect from .env
- Saved connections stored in `~/.adbview/connections.json`
- Active connection tracked via in-memory `ConnectionManager` singleton + browser cookie
- Middleware in `web/app.py` redirects to `/` if not connected (except open paths)
- MCP server: always connects via .env (no connection manager UI)

## Running the App

```bash
pip install -r requirements.txt
python run_web.py                              # Web UI at http://localhost:8000
python run_mcp.py --transport stdio            # MCP for local LLM clients
python run_mcp.py --transport sse --port 8001  # MCP for remote access
```

## Key Conventions

- **NEVER read or commit .env files** - they contain AWS credentials
- `.env` vars for S3: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `BUCKET_NAME`, `AWS_REGION`
- Web UI uses HTMX partial swaps - routes return full pages for navigation, fragments for HTMX
- Data view has a query builder pipeline: filter → deduplicate → group → sort → limit → columns → paginate
- Pagination uses ArcticDB's native `row_range` for unqueried views; query operations do full reads
- MCP write tools accept CSV strings (parsed via `pd.read_csv(io.StringIO(...))`)
- Bootstrap 5.3 dark theme + Bootstrap Icons + Chart.js via CDN, no JS build step
- Route path conflicts: `/api/data/{lib}/{sym:path}` is greedy — put specific routes like `/api/chart/`, `/api/rows/` on separate prefixes

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
