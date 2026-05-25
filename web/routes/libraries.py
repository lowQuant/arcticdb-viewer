from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from core import operations as ops
from web.app import templates

router = APIRouter()


@router.get("/libraries", response_class=HTMLResponse)
async def list_libraries(request: Request):
    libs = ops.list_libraries()
    return templates.TemplateResponse("libraries.html", {
        "request": request,
        "libraries": sorted(libs),
    })


@router.post("/libraries", response_class=HTMLResponse)
async def create_library(request: Request):
    form = await request.form()
    name = form.get("name", "").strip()
    if not name:
        return HTMLResponse(
            headers={"HX-Trigger": '{"showToast":{"message":"Library name is required","type":"error"}}'},
            content="",
            status_code=400,
        )
    try:
        ops.create_library(name)
    except Exception as e:
        return HTMLResponse(
            headers={"HX-Trigger": f'{{"showToast":{{"message":"Error: {e}","type":"error"}}}}'},
            content="",
            status_code=400,
        )
    libs = ops.list_libraries()
    return templates.TemplateResponse("partials/library_list.html", {
        "request": request,
        "libraries": sorted(libs),
    }, headers={"HX-Trigger": '{"showToast":{"message":"Library created successfully"}}'})


@router.delete("/libraries/{name}", response_class=HTMLResponse)
async def delete_library(request: Request, name: str):
    try:
        ops.delete_library(name)
    except Exception as e:
        return HTMLResponse(
            headers={"HX-Trigger": f'{{"showToast":{{"message":"Error: {e}","type":"error"}}}}'},
            content="",
            status_code=400,
        )
    libs = ops.list_libraries()
    return templates.TemplateResponse("partials/library_list.html", {
        "request": request,
        "libraries": sorted(libs),
    }, headers={"HX-Trigger": '{"showToast":{"message":"Library deleted successfully"}}'})
