from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from core import operations as ops
from web.app import templates

router = APIRouter()


@router.get("/libraries/{lib}/symbols", response_class=HTMLResponse)
async def list_symbols(request: Request, lib: str, q: str = ""):
    symbols = ops.list_symbols(lib)
    if q:
        symbols = [s for s in symbols if q.lower() in s.lower()]
    symbols = sorted(symbols)

    # If HTMX request, return only the partial
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse("partials/symbol_list.html", {
            "request": request,
            "library": lib,
            "symbols": symbols,
        })

    return templates.TemplateResponse("symbols.html", {
        "request": request,
        "library": lib,
        "symbols": symbols,
        "query": q,
    })


@router.delete("/libraries/{lib}/symbols/{sym:path}", response_class=HTMLResponse)
async def delete_symbol(request: Request, lib: str, sym: str):
    try:
        ops.delete_symbol(lib, sym)
    except Exception as e:
        return HTMLResponse(
            headers={"HX-Trigger": f'{{"showToast":{{"message":"Error: {e}","type":"error"}}}}'},
            content="",
            status_code=400,
        )
    symbols = sorted(ops.list_symbols(lib))
    return templates.TemplateResponse("partials/symbol_list.html", {
        "request": request,
        "library": lib,
        "symbols": symbols,
    }, headers={"HX-Trigger": '{"showToast":{"message":"Symbol deleted successfully"}}'})
