import os
from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["dashboard"])

UI_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "ui"))


@router.get("/", response_class=HTMLResponse)
def index_page():
    index_file = os.path.join(UI_DIR, "index.html")
    if os.path.exists(index_file):
        with open(index_file, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse("<h1>RentAsst Middleware Service Running</h1>")
