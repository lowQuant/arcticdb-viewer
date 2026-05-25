import os

from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

from core.connection import get_manager, load_connections

app = FastAPI(title="ADBView")

templates_dir = os.path.join(os.path.dirname(__file__), "templates")
templates = Jinja2Templates(directory=templates_dir)


# ── Inject connection info into all templates via Jinja2 globals ──

def _is_connected():
    return get_manager().is_connected


def _active_connection():
    return get_manager().active_name


templates.env.globals["is_connected"] = _is_connected
templates.env.globals["active_connection"] = _active_connection


# ── Middleware: require connection for data routes ──

OPEN_PATHS = {"/", "/connect", "/connections/new", "/connections/test", "/disconnect"}


class ConnectionMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in OPEN_PATHS or path.startswith("/connections/"):
            return await call_next(request)

        manager = get_manager()

        # Try to auto-reconnect from cookie
        if not manager.is_connected:
            cookie_name = request.cookies.get("adbview_connection")
            if cookie_name:
                saved = load_connections()
                for c in saved["connections"]:
                    if c["name"] == cookie_name:
                        try:
                            manager.connect(c["uri"], name=c["name"])
                        except Exception:
                            pass
                        break

        if not manager.is_connected:
            return RedirectResponse(url="/")

        return await call_next(request)


app.add_middleware(ConnectionMiddleware)


# ── Routers ──

from web.routes import connections, libraries, symbols, data  # noqa: E402

app.include_router(connections.router)
app.include_router(libraries.router)
app.include_router(symbols.router)
app.include_router(data.router)
