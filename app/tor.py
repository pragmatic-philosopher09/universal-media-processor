"""Managed Tor client so geo-blocked platforms (TikTok from India, ...) work with no setup.

The app starts a local `tor` process on demand, bound to 127.0.0.1 and restricted to exit
nodes in `TOR_EXIT_COUNTRIES`, and hands its SOCKS address to yt-dlp. Nothing runs until a
job actually needs it, and it is stopped when the app shuts down.
"""

from __future__ import annotations

import atexit
import logging
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from collections import deque

from .config import Settings
from .media import CancelToken, JobCancelled

log = logging.getLogger(__name__)

BOOTSTRAPPED = re.compile(r"Bootstrapped 100%")
COMMON_PATHS = (
    "/opt/homebrew/bin/tor",
    "/usr/local/bin/tor",
    "/usr/bin/tor",
    "/usr/sbin/tor",
)
INSTALL_HINT = "Install Tor (`brew install tor` / `apt install tor`) or set TOR_BIN."


class TorError(RuntimeError):
    pass


def find_tor(settings: Settings) -> str | None:
    """Resolve the tor binary: TOR_BIN, then PATH, then the usual install locations."""
    if settings.tor_bin:
        return shutil.which(settings.tor_bin)
    found = shutil.which("tor")
    if found:
        return found
    return next((p for p in COMMON_PATHS if os.access(p, os.X_OK)), None)


def tor_available(settings: Settings) -> bool:
    return settings.tor_mode != "off" and find_tor(settings) is not None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class TorClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[str] | None = None
        self._port = 0
        self._ready = threading.Event()
        self._tail: deque[str] = deque(maxlen=30)

    def command(self, binary: str, port: int) -> list[str]:
        settings = self.settings
        cmd = [
            binary,
            "--SocksPort",
            f"127.0.0.1:{port}",
            "--DataDirectory",
            str(settings.data_dir / "tor"),
            "--ClientOnly",
            "1",
            "--AvoidDiskWrites",
            "1",
            "--Log",
            "notice stdout",
        ]
        if settings.tor_exit_countries:
            nodes = ",".join(f"{{{code}}}" for code in settings.tor_exit_countries)
            cmd += ["--ExitNodes", nodes, "--StrictNodes", "1"]
        return cmd

    def _running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _read_output(self, proc: subprocess.Popen[str]) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip()
            self._tail.append(line)
            if BOOTSTRAPPED.search(line):
                self._ready.set()

    def ensure(self, cancel: CancelToken | None = None) -> str:
        """Start tor if needed, wait until it has built a circuit, return the SOCKS proxy URL."""
        with self._lock:
            if self._running() and self._ready.is_set():
                return self._url
            self._stop_locked()
            binary = find_tor(self.settings)
            if not binary:
                raise TorError(f"Tor is not installed. {INSTALL_HINT}")
            data_dir = self.settings.data_dir / "tor"
            data_dir.mkdir(parents=True, exist_ok=True)
            data_dir.chmod(0o700)
            self._port = self.settings.tor_socks_port or _free_port()
            self._ready.clear()
            self._tail.clear()
            log.info("starting tor on 127.0.0.1:%s", self._port)
            try:
                self._proc = subprocess.Popen(
                    self.command(binary, self._port),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
            except OSError as exc:
                raise TorError(f"Could not start Tor: {exc}") from exc
            threading.Thread(
                target=self._read_output, args=(self._proc,), name="tor-output", daemon=True
            ).start()

            deadline = time.monotonic() + self.settings.tor_bootstrap_timeout
            while not self._ready.wait(0.5):
                if cancel is not None and cancel.cancelled:
                    self._stop_locked()
                    raise JobCancelled()
                if not self._running():
                    detail = self._tail[-1] if self._tail else "no output"
                    self._stop_locked()
                    raise TorError(f"Tor exited during startup: {detail}")
                if time.monotonic() > deadline:
                    self._stop_locked()
                    raise TorError(
                        f"Tor did not connect within {self.settings.tor_bootstrap_timeout}s."
                    )
            return self._url

    @property
    def _url(self) -> str:
        # socks5h: resolve hostnames inside Tor so DNS never leaks the user's region
        return f"socks5h://127.0.0.1:{self._port}"

    def _stop_locked(self) -> None:
        proc, self._proc = self._proc, None
        self._ready.clear()
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()


_client: TorClient | None = None
_client_lock = threading.Lock()


def get_tor(settings: Settings) -> TorClient:
    global _client
    with _client_lock:
        if _client is None or _client.settings != settings:
            if _client is not None:
                _client.stop()
            _client = TorClient(settings)
        return _client


def shutdown_tor() -> None:
    global _client
    with _client_lock:
        client, _client = _client, None
    if client is not None:
        client.stop()


atexit.register(shutdown_tor)
