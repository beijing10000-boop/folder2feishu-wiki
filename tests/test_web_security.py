from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from folder2feishu.web_security import LocalRequestGuard


def make_client() -> TestClient:
    app = FastAPI()
    app.add_middleware(LocalRequestGuard, csrf_token="expected")

    @app.get("/api/value")
    def get_value() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/api/value")
    def set_value() -> dict[str, bool]:
        return {"ok": True}

    return TestClient(app, raise_server_exceptions=False)


def test_state_change_requires_csrf_header() -> None:
    client = make_client()
    assert client.get("/api/value").status_code == 200
    assert client.post("/api/value").status_code == 403
    response = client.post("/api/value", headers={"X-F2F-CSRF": "expected"})
    assert response.status_code == 200


def test_cross_site_request_is_rejected() -> None:
    client = make_client()
    response = client.post(
        "/api/value",
        headers={
            "X-F2F-CSRF": "expected",
            "Origin": "https://evil.example",
            "Sec-Fetch-Site": "cross-site",
        },
    )
    assert response.status_code == 403


def test_security_headers_are_present() -> None:
    response = make_client().get("/api/value")
    assert response.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
