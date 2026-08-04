from __future__ import annotations

import secrets
import socket
import threading
import webbrowser

import uvicorn

from ..config import AIConfigStore
from ..paths import AppPaths
from .api import create_app


def run_console() -> int:
    AIConfigStore().ensure()
    paths = AppPaths()
    paths.ensure()
    token = secrets.token_urlsafe(24)
    app = create_app(token, paths=paths)

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = int(listener.getsockname()[1])
    url = f"http://127.0.0.1:{port}/?token={token}"

    print("Puppy 控制台已启动", flush=True)
    print(url, flush=True)
    opener = threading.Timer(0.4, lambda: webbrowser.open(url))
    opener.daemon = True
    opener.start()

    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            access_log=False,
        )
    )
    server.run(sockets=[listener])
    return 0
