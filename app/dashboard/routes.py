import os
import sys
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, FileResponse, Response

router = APIRouter(tags=["dashboard"])

if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    UI_DIR = os.path.join(sys._MEIPASS, "app", "ui")
else:
    UI_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "ui"))


@router.get("/", response_class=HTMLResponse)
@router.get("/login", response_class=HTMLResponse)
@router.get("/dashboard", response_class=HTMLResponse)
@router.get("/middleware", response_class=HTMLResponse)
def index_page(request: Request):
    index_file = os.path.join(UI_DIR, "index.html")
    if os.path.exists(index_file):
        with open(index_file, "r", encoding="utf-8") as f:
            html = f.read()
        # Every /api/* route now requires X-Middleware-Key (see security/auth.py).
        # Injecting the current key here — this route is the only unauthenticated
        # one — lets the browser UI authenticate itself automatically; any other
        # caller still needs to supply the header explicitly.
        api_key = getattr(request.app.state, "api_key", "") or ""
        key_script = f'<script>window.MW_API_KEY="{api_key}";</script>\n'
        if "</head>" in html:
            html = html.replace("</head>", key_script + "</head>", 1)
        else:
            html = key_script + html
        return HTMLResponse(content=html)
    return HTMLResponse("<h1>RentAsst Middleware Service Running</h1>")



@router.get("/favicon.ico", include_in_schema=False)
@router.get("/favicon.svg", include_in_schema=False)
def favicon():
    svg_favicon = os.path.join(UI_DIR, "favicon.svg")
    if os.path.exists(svg_favicon):
        return FileResponse(svg_favicon, media_type="image/svg+xml")
    ico_favicon = os.path.join(UI_DIR, "favicon.ico")
    if os.path.exists(ico_favicon):
        return FileResponse(ico_favicon, media_type="image/x-icon")
    return Response(status_code=204)


@router.get("/.well-known/appspecific/com.chrome.devtools.json", include_in_schema=False)
def chrome_devtools():
    return Response(status_code=204)

