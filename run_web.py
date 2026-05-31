#!/usr/bin/env python3
import uvicorn

from core.settings import is_read_only

if __name__ == "__main__":
    mode = "READ-ONLY (safe)" if is_read_only() else "READ-WRITE"
    print(f"[arcticdb-viewer] Web UI on http://localhost:8000 — {mode} mode")
    if not is_read_only():
        print("[arcticdb-viewer] WARNING: writes are ENABLED (ADBVIEW_READONLY=0).")
    uvicorn.run("web.app:app", host="0.0.0.0", port=8000, reload=True)
