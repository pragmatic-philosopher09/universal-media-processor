from __future__ import annotations

import os

from app.config import Settings, load_dotenv


def test_load_dotenv_does_not_override_existing(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        "# comment\n"
        "BROWSER_COOKIE_ORDER=chrome:Profile 1,chrome\n"
        "export X264_CRF=20  # inline comment\n"
        'SHARPEN="0.5"\n'
        "PORT=9999\n"
        "BROKEN LINE\n"
    )
    monkeypatch.setenv("PORT", "8123")
    for key in ("BROWSER_COOKIE_ORDER", "X264_CRF", "SHARPEN"):
        monkeypatch.delenv(key, raising=False)
    assert load_dotenv(env) == 3
    assert os.environ["BROWSER_COOKIE_ORDER"] == "chrome:Profile 1,chrome"
    assert os.environ["X264_CRF"] == "20"
    assert os.environ["SHARPEN"] == "0.5"
    assert os.environ["PORT"] == "8123"  # existing value wins


def test_settings_from_env_reads_dotenv(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("BROWSER_COOKIE_ORDER=chrome:Profile 1,chrome\nAUTO_BROWSER_COOKIES=always\n")
    monkeypatch.setenv("DOTENV_PATH", str(env))
    for key in ("BROWSER_COOKIE_ORDER", "AUTO_BROWSER_COOKIES"):
        monkeypatch.delenv(key, raising=False)
    settings = Settings.from_env()
    assert settings.browser_cookie_order == ("chrome:Profile 1", "chrome")
    assert settings.auto_browser_cookies == "always"


def test_missing_dotenv_is_fine(tmp_path):
    assert load_dotenv(tmp_path / "nope.env") == 0


def test_proxy_settings(monkeypatch):
    monkeypatch.setenv("DOTENV_PATH", "/nonexistent")
    monkeypatch.setenv("PROXY_URL", "socks5://127.0.0.1:1080")
    monkeypatch.setenv("YOUTUBE_PROXY", "http://proxy.example:8080")
    settings = Settings.from_env()
    assert settings.proxy_for("youtube") == "http://proxy.example:8080"
    assert settings.proxy_for("tiktok") == "socks5://127.0.0.1:1080"
    monkeypatch.delenv("PROXY_URL")
    monkeypatch.delenv("YOUTUBE_PROXY")
    assert Settings.from_env().proxy_for("tiktok") is None
