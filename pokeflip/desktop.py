"""The desktop app.

``pokeflip app`` starts the server on a private port bound to loopback, waits
for it to answer, and opens the dashboard in a native window. It is the same
dashboard the browser gets - there is no second UI to keep in step - but it
launches from an icon, keeps the scheduler running behind it, and shuts
everything down when you close the window.

The native window needs ``pywebview``. Without it the app still works: it opens
your normal browser instead and says so, rather than failing at the one moment
you wanted a window.
"""

from __future__ import annotations

import contextlib
import logging
import socket
import threading
import time
import webbrowser
from typing import Any

import httpx

from .config import Config

log = logging.getLogger("pokeflip.desktop")

WINDOW_TITLE = "pokeflip"
DEFAULT_SIZE = (1280, 860)
MIN_SIZE = (900, 600)
# How long to wait for the server thread before giving up on it.
STARTUP_TIMEOUT = 25.0


def free_port(host: str = "127.0.0.1") -> int:
    """An unused port, so two copies of the app never fight over one."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def wait_for_server(url: str, timeout: float = STARTUP_TIMEOUT) -> bool:
    """Block until the API answers, or give up."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{url}/api/health", timeout=2.0)
            if response.status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.25)
    return False


class ServerThread:
    """Runs uvicorn in the background for the lifetime of the window."""

    def __init__(self, config: Config, host: str = "127.0.0.1",
                 port: int | None = None, start_scheduler: bool = True):
        self.config = config
        self.host = host
        self.port = port or free_port(host)
        self.start_scheduler = start_scheduler
        self._server: Any = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> str:
        import uvicorn

        from .api import create_app

        app = create_app(self.config, start_scheduler=self.start_scheduler)
        self._server = uvicorn.Server(uvicorn.Config(
            app, host=self.host, port=self.port, log_level="warning",
        ))
        self._thread = threading.Thread(target=self._server.run,
                                        name="pokeflip-server", daemon=True)
        self._thread.start()

        if not wait_for_server(self.url):
            raise RuntimeError(
                f"the server did not start on {self.url} within "
                f"{STARTUP_TIMEOUT:.0f}s"
            )
        return self.url

    def stop(self) -> None:
        if self._server is not None:
            # uvicorn watches this flag and unwinds its own lifespan, which is
            # what stops the scheduler and closes the database cleanly.
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=10)


def _window_url(server: ServerThread, config: Config) -> str:
    """The dashboard URL, carrying the API token when one is set.

    The window is just a browser, so it needs the same credential anything else
    would. It never leaves the loopback interface.
    """
    token = config.server.api_token
    return f"{server.url}/?token={token}" if token else f"{server.url}/"


def run_app(config: Config, start_scheduler: bool = True,
            force_browser: bool = False) -> int:
    """Open the desktop window. Returns a process exit code."""
    server = ServerThread(config, start_scheduler=start_scheduler)
    try:
        server.start()
    except Exception as exc:
        log.error("could not start the server: %s", exc)
        print(f"Could not start pokeflip: {exc}")
        return 1

    url = _window_url(server, config)
    print(f"pokeflip running at {server.url}")

    try:
        if force_browser:
            return _run_in_browser(url, server)
        return _run_in_window(url, server)
    finally:
        server.stop()


def _run_in_window(url: str, server: ServerThread) -> int:
    try:
        import webview
    except ImportError:
        print("pywebview is not installed, so opening your browser instead.")
        print("  For a real app window:  pip install 'pokeflip[desktop]'")
        return _run_in_browser(url, server)

    window = webview.create_window(
        WINDOW_TITLE,
        url,
        width=DEFAULT_SIZE[0],
        height=DEFAULT_SIZE[1],
        min_size=MIN_SIZE,
        confirm_close=False,
    )
    # Closing the window is the app quitting, so the server goes with it.
    window.events.closed += server.stop

    try:
        webview.start()
    except Exception as exc:
        # A missing GTK/Qt/WebView2 runtime surfaces here rather than at import.
        log.warning("native window unavailable (%s); using the browser", exc)
        print(f"Could not open a native window ({exc}). Opening your browser.")
        return _run_in_browser(url, server)
    return 0


def _run_in_browser(url: str, server: ServerThread) -> int:
    """Fallback: the system browser, with the process held open for the jobs."""
    opened = webbrowser.open(url)
    if not opened:
        print(f"Open this in your browser: {url}")
    print("Running. Press Ctrl-C to quit.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping...")
    return 0


@contextlib.contextmanager
def running_server(config: Config, start_scheduler: bool = False):
    """A started server for the duration of the block. Used by tests."""
    server = ServerThread(config, start_scheduler=start_scheduler)
    server.start()
    try:
        yield server
    finally:
        server.stop()
