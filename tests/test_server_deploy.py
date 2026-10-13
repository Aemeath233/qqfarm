import sys

import pytest
from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from qqfarm import web_api, web_cli

PASSWORD = "reverse-proxy-test-password"


def test_web_cli_uses_internal_address_and_explicit_proxy_configuration(monkeypatch, tmp_path):
    import uvicorn

    calls = []
    app = object()
    monkeypatch.setattr(sys, "argv", ["qqfarm-web"])
    monkeypatch.setenv("FARM_WEB_HOST", "10.0.0.8")
    monkeypatch.setenv("FARM_WEB_PORT", "8791")
    monkeypatch.setenv("FARM_TRUSTED_PROXIES", "10.0.0.9")
    monkeypatch.setenv("FARM_WEB_DATA", str(tmp_path))
    monkeypatch.setattr(web_api, "create_app", lambda _root: app)
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    web_cli.main()
    assert calls == [
        (
            (app,),
            {
                "host": "10.0.0.8",
                "port": 8791,
                "log_level": "warning",
                "proxy_headers": True,
                "forwarded_allow_ips": "10.0.0.9",
            },
        )
    ]


@pytest.mark.parametrize("source,expected", [("127.0.0.1", 200), ("10.0.0.12", 403)])
def test_only_trusted_proxy_can_mark_inbound_login_as_https(tmp_path, source, expected):
    app = web_api.create_app(tmp_path, password=PASSWORD)
    proxy = ProxyHeadersMiddleware(app, trusted_hosts="127.0.0.1")
    with TestClient(proxy, base_url="http://farm.example.test", client=(source, 12345)) as client:
        response = client.post(
            "/api/login",
            headers={"X-Farm-Action": "personal-control", "X-Forwarded-Proto": "https"},
            json={"password": PASSWORD},
        )
        assert response.status_code == expected
        if expected == 200:
            assert "secure" in response.headers["set-cookie"].lower()
