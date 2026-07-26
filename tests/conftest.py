"""Shared fixtures: a fake OpenAI-compatible upstream and a live gateway."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from keygate.proxy import build_server
from keygate.store import Workspace

#: The single "real" upstream key the gateway is supposed to hold.
REAL_KEY = "sk-test-real-upstream-key"

SSE_DELTAS = [
    b'data: {"id":"c1","model":"gpt-4o-mini","choices":[{"delta":{"content":"Hi"}}]}\n\n',
    b'data: {"id":"c1","model":"gpt-4o-mini","choices":[{"delta":{"content":" there"}}]}\n\n',
]
SSE_USAGE = (
    b'data: {"id":"c1","model":"gpt-4o-mini","choices":[],'
    b'"usage":{"prompt_tokens":10,"completion_tokens":20,"total_tokens":30}}\n\n'
)
SSE_DONE = b"data: [DONE]\n\n"


class _UpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: Any) -> None:  # keep pytest output clean
        pass

    @property
    def fake(self) -> "FakeUpstream":
        return self.server.fake  # type: ignore[attr-defined]

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("x-request-id", "req_fake_123")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        return self.headers.get("Authorization") == f"Bearer {REAL_KEY}"

    def do_GET(self) -> None:  # noqa: N802
        self.fake.record(self, b"")
        if not self._authorized():
            self._json(401, {"error": {"message": "bad upstream key"}})
            return
        if self.path == "/v1/models":
            self._json(200, {"object": "list", "data": [{"id": "gpt-4o-mini"}]})
            return
        self._json(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.fake.record(self, body)

        if not self._authorized():
            self._json(401, {"error": {"message": "bad upstream key"}})
            return
        if self.path != "/v1/chat/completions":
            self._json(404, {"error": {"message": "not found"}})
            return
        if self.fake.force_status is not None:
            self._json(
                self.fake.force_status,
                self.fake.force_body or {"error": {"message": "forced failure"}},
            )
            return

        payload = json.loads(body)
        if payload.get("stream"):
            options = payload.get("stream_options") or {}
            self._stream(include_usage=bool(options.get("include_usage")))
            return
        self._json(
            200,
            {
                "id": "chatcmpl-fake",
                "object": "chat.completion",
                "model": payload.get("model", "gpt-4o-mini"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "Hi there"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 20,
                    "total_tokens": 30,
                },
            },
        )

    def _stream(self, include_usage: bool) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        chunks = list(SSE_DELTAS) + ([SSE_USAGE] if include_usage else []) + [SSE_DONE]
        for chunk in chunks:
            self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
        self.wfile.write(b"0\r\n\r\n")


class FakeUpstream:
    """A minimal OpenAI-compatible server that records what it received."""

    def __init__(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _UpstreamHandler)
        self.server.fake = self  # type: ignore[attr-defined]
        self.server.daemon_threads = True
        self.received: list[dict[str, Any]] = []
        self.force_status: int | None = None
        self.force_body: dict[str, Any] | None = None
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def record(self, handler: _UpstreamHandler, body: bytes) -> None:
        self.received.append(
            {
                "method": handler.command,
                "path": handler.path,
                "headers": dict(handler.headers.items()),
                "body": body,
                "json": json.loads(body) if body else None,
            }
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def upstream():
    fake = FakeUpstream()
    fake.start()
    try:
        yield fake
    finally:
        fake.stop()


@pytest.fixture
def workspace(tmp_path, upstream, monkeypatch) -> Workspace:
    """A workspace pointed at the fake upstream, with the real key in the env."""
    monkeypatch.setenv("KEYGATE_UPSTREAM_API_KEY", REAL_KEY)
    ws = Workspace.create(tmp_path / "ws", upstream_base_url=upstream.base_url)
    ws.config["pricing"]["gpt-4o-mini"] = {"input": 1.0, "output": 2.0}
    ws.save_config()
    return ws


@pytest.fixture
def gateway(workspace):
    """A running gateway; yields an object with ``.url`` and the workspace."""
    server = build_server(workspace, host="127.0.0.1", port=0, quiet=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    class Gateway:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        ws = workspace

    try:
        yield Gateway()
    finally:
        server.shutdown()
        server.server_close()


def call(
    url: str,
    token: str | None = None,
    payload: dict[str, Any] | None = None,
    method: str = "POST",
    raw_body: bytes | None = None,
) -> tuple[int, bytes, dict[str, str]]:
    """Make an HTTP call, returning ``(status, body, headers)`` even on 4xx/5xx."""
    data = raw_body
    if data is None and payload is not None:
        data = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read(), dict(response.headers.items())
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers.items())
