from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.jobs import JobManager, JobOptions
from app.main import create_app

LOGIN = ("owner", "test-only-random-password")


@pytest.fixture
def client(settings):
    with TestClient(
        create_app(
            replace(
                settings,
                app_username=LOGIN[0],
                app_password=LOGIN[1],
            )
        )
    ) as client:
        yield client


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/app.js",
        "/style.css",
        "/api/capabilities",
        "/api/docs",
        "/api/openapi.json",
        "/api/jobs/example",
        "/api/jobs/example/files/0",
    ],
)
def test_all_read_routes_require_login(client, path):
    response = client.get(path)
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Basic ")
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("header", ["Basic invalid", "Bearer token", "Basic Og=="])
def test_malformed_login_rejected(client, header):
    assert client.get("/", headers={"Authorization": header}).status_code == 401


@pytest.mark.parametrize("login", [("wrong", LOGIN[1]), (LOGIN[0], "wrong")])
def test_wrong_login_rejected(client, login):
    assert client.get("/", auth=login).status_code == 401


@pytest.mark.parametrize(
    "method,path",
    [
        ("post", "/api/jobs"),
        ("post", "/api/uploads?filename=video.mp4"),
        ("post", "/api/jobs/example/files/0/convert"),
        ("delete", "/api/jobs/example"),
    ],
)
def test_write_routes_require_login(client, method, path):
    assert client.request(method, path, content=b"x").status_code == 401
    assert not client.app.state.manager.jobs


def test_valid_login_serves_app(client):
    response = client.get("/", auth=LOGIN)
    assert response.status_code == 200 and "Media Downloader" in response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["content-security-policy"] == "frame-ancestors 'none'"
    assert client.get("/api/openapi.json", auth=LOGIN).status_code == 200


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://attacker.example"},
        {"Origin": "null"},
        {"Sec-Fetch-Site": "cross-site"},
    ],
)
def test_cross_site_writes_rejected(client, headers):
    response = client.post(
        "/api/uploads?filename=video.mp4",
        auth=LOGIN,
        headers=headers,
        content=b"x",
    )
    assert response.status_code == 403
    assert not client.app.state.manager.jobs


def test_same_origin_write_reaches_api(client):
    response = client.post(
        "/api/uploads?filename=video.mp4",
        auth=LOGIN,
        headers={"Origin": "http://testserver"},
        content=b"",
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "The uploaded video is empty."


@pytest.mark.parametrize("username,password", [("owner", None), (None, "password")])
def test_partial_auth_configuration_fails_closed(settings, username, password):
    with pytest.raises(ValueError, match="both APP_USERNAME and APP_PASSWORD"):
        create_app(replace(settings, app_username=username, app_password=password))


def test_password_not_in_settings_repr(settings):
    assert LOGIN[1] not in repr(replace(settings, app_password=LOGIN[1]))


def test_auth_settings_from_environment(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("DOTENV_PATH", "/nonexistent")
    monkeypatch.setenv("APP_USERNAME", LOGIN[0])
    monkeypatch.setenv("APP_PASSWORD", LOGIN[1])
    settings = Settings.from_env()
    assert (settings.app_username, settings.app_password) == LOGIN


def test_automatic_browser_login_is_instagram_only(settings):
    from app.jobs import Job
    from app.urls import normalize_url

    manager = JobManager(replace(settings, auto_browser_cookies="always"))
    for url, allowed in [
        ("https://www.instagram.com/p/Dd07RwEzu_O/", True),
        ("https://youtu.be/jNQXAC9IVRw", False),
    ]:
        job = Job(
            "test",
            normalize_url(url, settings.allowed_domains),
            JobOptions(),
            "client",
            settings.jobs_dir,
        )
        assert manager._browser_login_allowed(job) is allowed


@pytest.mark.parametrize("protected", [False, True])
def test_container_healthcheck_supports_login(monkeypatch, protected):
    import base64
    from unittest.mock import MagicMock

    from app.healthcheck import check

    monkeypatch.setenv("DOTENV_PATH", "/nonexistent")
    monkeypatch.setenv("PORT", "8766")
    monkeypatch.setenv("APP_USERNAME", LOGIN[0] if protected else "")
    monkeypatch.setenv("APP_PASSWORD", LOGIN[1] if protected else "")
    urlopen = MagicMock()
    urlopen.return_value.__enter__.return_value.status = 200
    monkeypatch.setattr("app.healthcheck.urllib.request.urlopen", urlopen)
    check()
    request = urlopen.call_args.args[0]
    assert request.full_url == "http://127.0.0.1:8766/api/capabilities"
    expected = "Basic " + base64.b64encode(":".join(LOGIN).encode()).decode()
    assert request.get_header("Authorization") == (expected if protected else None)
