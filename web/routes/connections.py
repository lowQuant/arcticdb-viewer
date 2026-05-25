from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from core.connection import (
    get_manager, ConnectionManager,
    load_connections, add_connection, remove_connection, set_last_used,
)
from web.app import templates

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def welcome(request: Request):
    saved = load_connections()
    env_conn = ConnectionManager.detect_env_connection()

    return templates.TemplateResponse("welcome.html", {
        "request": request,
        "connections": saved["connections"],
        "last_used": saved.get("last_used"),
        "env_connection": env_conn,
    })


@router.post("/connect", response_class=HTMLResponse)
async def connect(request: Request):
    form = await request.form()
    name = form.get("name", "").strip()
    uri = form.get("uri", "").strip()
    conn_type = form.get("type", "").strip()

    if not uri:
        return templates.TemplateResponse("welcome.html", {
            "request": request,
            "connections": load_connections()["connections"],
            "env_connection": ConnectionManager.detect_env_connection(),
            "error": "Connection URI is required.",
        })

    manager = get_manager()
    ok, msg = ConnectionManager.test_connection(uri)

    if not ok:
        return templates.TemplateResponse("welcome.html", {
            "request": request,
            "connections": load_connections()["connections"],
            "env_connection": ConnectionManager.detect_env_connection(),
            "error": f"Connection failed: {msg}",
        })

    manager.connect(uri, name=name)

    if conn_type != "s3_env":
        add_connection(name, conn_type, uri)
    set_last_used(name)

    response = RedirectResponse(url="/libraries", status_code=303)
    response.set_cookie("adbview_connection", name, max_age=86400 * 365)
    return response


@router.get("/connections/new", response_class=HTMLResponse)
async def new_connection_form(request: Request):
    return templates.TemplateResponse("connection_form.html", {
        "request": request,
    })


@router.post("/connections/new", response_class=HTMLResponse)
async def create_connection(request: Request):
    form = await request.form()
    conn_type = form.get("conn_type", "")
    name = form.get("name", "").strip()

    if not name:
        return templates.TemplateResponse("connection_form.html", {
            "request": request,
            "error": "Connection name is required.",
        })

    uri = ""
    if conn_type == "lmdb":
        path = form.get("lmdb_path", "").strip()
        if not path:
            return templates.TemplateResponse("connection_form.html", {
                "request": request, "error": "LMDB path is required.",
            })
        uri = f"lmdb://{path}"

    elif conn_type == "s3":
        bucket = form.get("s3_bucket", "").strip()
        region = form.get("s3_region", "").strip()
        access_key = form.get("s3_access_key", "").strip()
        secret_key = form.get("s3_secret_key", "").strip()
        endpoint = form.get("s3_endpoint", "").strip()
        use_https = form.get("s3_https") == "on"
        aws_auth = form.get("s3_aws_auth") == "on"

        if not bucket or not region:
            return templates.TemplateResponse("connection_form.html", {
                "request": request, "error": "Bucket and region are required.",
            })
        if not aws_auth and (not access_key or not secret_key):
            return templates.TemplateResponse("connection_form.html", {
                "request": request, "error": "Provide credentials or enable AWS auth.",
            })

        uri = ConnectionManager.build_s3_uri(
            bucket=bucket, region=region,
            access_key=access_key, secret_key=secret_key,
            endpoint=endpoint, use_https=use_https, aws_auth=aws_auth,
        )

    elif conn_type == "mem":
        uri = "mem://"

    if not uri:
        return templates.TemplateResponse("connection_form.html", {
            "request": request, "error": "Invalid connection type.",
        })

    ok, msg = ConnectionManager.test_connection(uri)
    if not ok:
        return templates.TemplateResponse("connection_form.html", {
            "request": request, "error": f"Connection test failed: {msg}",
        })

    add_connection(name, conn_type, uri)
    manager = get_manager()
    manager.connect(uri, name=name)
    set_last_used(name)

    response = RedirectResponse(url="/libraries", status_code=303)
    response.set_cookie("adbview_connection", name, max_age=86400 * 365)
    return response


@router.post("/connections/test", response_class=HTMLResponse)
async def test_connection(request: Request):
    form = await request.form()
    uri = form.get("uri", "").strip()
    if not uri:
        return HTMLResponse('<span class="text-danger">No URI provided</span>')

    ok, msg = ConnectionManager.test_connection(uri)
    if ok:
        return HTMLResponse(f'<span class="text-success"><i class="bi bi-check-circle"></i> {msg}</span>')
    else:
        return HTMLResponse(f'<span class="text-danger"><i class="bi bi-x-circle"></i> {msg}</span>')


@router.delete("/connections/{name}", response_class=HTMLResponse)
async def delete_connection(request: Request, name: str):
    remove_connection(name)
    manager = get_manager()
    if manager.active_name == name:
        manager.disconnect()

    return HTMLResponse(
        content="",
        headers={
            "HX-Trigger": '{"showToast":{"message":"Connection removed"}}',
            "HX-Redirect": "/",
        },
    )


@router.post("/disconnect", response_class=HTMLResponse)
async def disconnect(request: Request):
    get_manager().disconnect()
    response = RedirectResponse(url="/", status_code=303)
    response.delete_cookie("adbview_connection")
    return response
