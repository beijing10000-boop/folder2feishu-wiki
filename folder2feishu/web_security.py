from __future__ import annotations

import secrets
from urllib.parse import urlsplit

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]"}


class LocalRequestGuard(BaseHTTPMiddleware):
    """Protect a privileged localhost application from cross-site requests."""

    def __init__(self, app: ASGIApp, csrf_token: str) -> None:
        super().__init__(app)
        self.csrf_token = csrf_token

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        if request.method not in SAFE_METHODS:
            fetch_site = request.headers.get("sec-fetch-site", "")
            if fetch_site == "cross-site":
                return self._denied("拒绝跨站请求")
            origin = request.headers.get("origin")
            if origin:
                parsed = urlsplit(origin)
                if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
                    return self._denied("请求来源不是本机控制台")
            supplied = request.headers.get("x-f2f-csrf", "")
            if not secrets.compare_digest(supplied, self.csrf_token):
                return self._denied("请求校验失败，请刷新页面")

        response = await call_next(request)
        response.headers["Cache-Control"] = (
            "no-store" if request.url.path.startswith("/api/") else "no-cache"
        )
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self'; "
            "img-src 'self' data:; "
            "font-src 'self'; "
            "connect-src 'self'; "
            "object-src 'none'; "
            "base-uri 'none'; "
            "frame-ancestors 'none'; "
            "form-action 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
        )
        return response

    @staticmethod
    def _denied(message: str) -> JSONResponse:
        response = JSONResponse(
            status_code=403,
            content={"error": {"code": "LOCAL_REQUEST_REJECTED", "message": message}},
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)
