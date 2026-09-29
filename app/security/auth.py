import hmac
from fastapi import Header, HTTPException, Request


async def require_api_key(request: Request, x_middleware_key: str = Header(default=None, alias="X-Middleware-Key")) -> None:
    """
    Guards every /api/* route behind the middleware's own local API key (see
    api_key.get_or_create_api_key). The dashboard UI (app/dashboard/routes.py)
    is served without this dependency and injects the current key into the page
    it returns, so a browser loading the UI authenticates itself automatically —
    any OTHER caller must supply the X-Middleware-Key header explicitly.
    """
    expected = getattr(request.app.state, "api_key", None)
    if not expected or not x_middleware_key or not hmac.compare_digest(x_middleware_key, expected):
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid X-Middleware-Key header. Open the dashboard in a browser, or read the key from .data/api.key.",
        )
