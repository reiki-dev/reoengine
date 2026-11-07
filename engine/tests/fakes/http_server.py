"""Local scripted HTTP server for API adapter tests."""
from __future__ import annotations

import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any


class ScriptedHttpServer:
    def __init__(self, responses: list[tuple[int, bytes]]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.redirect_to: str | None = None
        self.slow_drip = False
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                request_body = self.rfile.read(length)
                server.requests.append({"path": self.path, "headers": dict(self.headers.items()),
                                       "body": json.loads(request_body)})
                status, body = server.responses.pop(0)
                self.send_response(status)
                if server.redirect_to is not None:
                    self.send_header("Location", server.redirect_to)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if server.slow_drip:
                    for byte in body:
                        try:
                            self.wfile.write(bytes((byte,)))
                            self.wfile.flush()
                            time.sleep(0.2)
                        except (BrokenPipeError, ConnectionResetError):
                            break
                else:
                    self.wfile.write(body)

            def do_GET(self) -> None:
                server.requests.append({"method": "GET", "path": self.path, "headers": dict(self.headers.items())})
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, format: str, *args: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.server.block_on_close = False
        self.thread = Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> ScriptedHttpServer:
        self.thread.start()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
