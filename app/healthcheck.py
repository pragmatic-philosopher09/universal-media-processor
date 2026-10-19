"""Container readiness check, including optional app authentication."""

import base64
import urllib.request

from .config import Settings


def check() -> None:
    settings = Settings.from_env()
    request = urllib.request.Request(f"http://127.0.0.1:{settings.port}/api/capabilities")
    if settings.app_username and settings.app_password:
        credentials = f"{settings.app_username}:{settings.app_password}".encode()
        request.add_header("Authorization", "Basic " + base64.b64encode(credentials).decode())
    with urllib.request.urlopen(request, timeout=4) as response:
        if response.status != 200:
            raise RuntimeError(f"Health check failed with HTTP {response.status}")


if __name__ == "__main__":
    check()
