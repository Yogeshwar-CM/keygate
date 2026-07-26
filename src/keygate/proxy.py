"""Stdlib HTTP gateway that swaps virtual keys for the real upstream key.

The server accepts OpenAI-compatible requests authenticated with a keygate
virtual key (``kg_v1_...``), checks the caller's budget, forwards the request
to the configured upstream with the single real API key, and records token
usage and cost in the request log.

Supported routes:

``GET  /healthz``               local liveness check, no auth
``GET  /v1/models``             authenticated pass-through
``POST /v1/chat/completions``   authenticated, metered, streaming or not
"""

from __future__ import annotations

import json
import socket
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import __version__
from .keys import parse_bearer
from .store import Workspace

#: Client headers we relay upstream. Everything else (notably Authorization,
#: Host and Accept-Encoding) is dropped or replaced.
FORWARDED_REQUEST_HEADERS = frozenset(
    {
        "content-type",
        "accept",
        "user-agent",
        "openai-organization",
        "openai-project",
        "openai-beta",
    }
)

#: Upstream response headers we relay back. Hop-by-hop headers and framing
#: headers are re-derived by us, so they must not be copied.
FORWARDED_RESPONSE_HEADER_PREFIXES = ("openai-", "x-ratelimit-", "x-request-id")

CHAT_COMPLETIONS_PATH = "/v1/chat/completions"
MODELS_PATH = "/v1/models"
HEALTH_PATH = "/healthz"


class UpstreamNotConfigured(Exception):
    """Raised when no real upstream API key can be resolved."""


def _error_body(message: str, code: str, etype: str = "keygate_error") -> bytes:
    return json.dumps(
        {"error": {"message": message, "type": etype, "code": code, "param": None}}
    ).encode("utf-8")


class GatewayHandler(BaseHTTPRequestHandler):
    """Request handler; :func:`make_handler` binds a workspace to a subclass."""

    protocol_version = "HTTP/1.1"
    server_version = f"keygate/{__version__}"
    sys_version = ""

    # Bound by make_handler().
    workspace: Workspace
    quiet: bool = False

    # -- plumbing ------------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:
        if not self.quiet:
            sys.stderr.write(
                "%s - - [%s] %s\n"
                % (self.address_string(), self.log_date_time_string(), fmt % args)
            )

    def _client_ip(self) -> str:
        return self.client_address[0] if self.client_address else "-"

    def _send_bytes(
        self,
        status: int,
        body: bytes,
        content_type: str = "application/json",
        close: bool = False,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if close:
            # We are answering without having drained the request body, so the
            # connection can't be reused -- the leftover bytes would be read as
            # the next request.
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_error_json(
        self, status: int, message: str, code: str, close: bool = False
    ) -> None:
        self._send_bytes(status, _error_body(message, code), close=close)

    def _read_body(self) -> bytes | None:
        """Read the request body, or send an error and return ``None``."""
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            self._send_error_json(
                411,
                "chunked request bodies are not supported; send Content-Length",
                "length_required",
                close=True,
            )
            return None
        raw_len = self.headers.get("Content-Length")
        if raw_len is None:
            self._send_error_json(
                411, "Content-Length is required", "length_required", close=True
            )
            return None
        try:
            length = int(raw_len)
        except ValueError:
            self._send_error_json(
                400, "malformed Content-Length", "bad_request", close=True
            )
            return None
        limit = int(self.workspace.config.get("max_request_bytes", 10 * 1024 * 1024))
        if length < 0 or length > limit:
            self._send_error_json(
                413,
                f"request body exceeds {limit} bytes",
                "payload_too_large",
                close=True,
            )
            return None
        return self.rfile.read(length)

    # -- routing -------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - name mandated by BaseHTTPRequestHandler
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == HEALTH_PATH:
            self._send_bytes(
                200, json.dumps({"status": "ok", "version": __version__}).encode()
            )
            return
        if path == MODELS_PATH:
            self._handle_proxied(MODELS_PATH, method="GET", body=None)
            return
        self._send_error_json(404, f"unknown route {path}", "not_found")

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path != CHAT_COMPLETIONS_PATH:
            self._send_error_json(
                404,
                f"keygate {__version__} only proxies POST {CHAT_COMPLETIONS_PATH}",
                "not_found",
                close=True,
            )
            return
        body = self._read_body()
        if body is None:
            return
        self._handle_proxied(CHAT_COMPLETIONS_PATH, method="POST", body=body)

    # -- the actual proxy ----------------------------------------------------

    def _handle_proxied(self, path: str, method: str, body: bytes | None) -> None:
        started = time.monotonic()
        store = self.workspace.store
        client_ip = self._client_ip()

        auth = store.authenticate(parse_bearer(self.headers.get("Authorization")))
        if not auth.ok:
            store.record_request(
                endpoint=path,
                status=auth.status,
                key_id=auth.key.id if auth.key else None,
                user_id=auth.user.id if auth.user else None,
                latency_ms=int((time.monotonic() - started) * 1000),
                client_ip=client_ip,
                note=auth.code,
            )
            self._send_error_json(
                auth.status, auth.message or "unauthorized", auth.code or "unauthorized"
            )
            return

        assert auth.key is not None and auth.user is not None
        store.touch_key(auth.key.id)

        payload: dict[str, Any] = {}
        model: str | None = None
        streaming = False
        if body:
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError:
                parsed = None
            if not isinstance(parsed, dict):
                self._record_and_fail(
                    path, auth, client_ip, started, 400,
                    "request body must be a JSON object", "invalid_request_error",
                )
                return
            payload = parsed
            model = payload.get("model") if isinstance(payload.get("model"), str) else None
            streaming = bool(payload.get("stream"))
            if streaming:
                # Ask the upstream to emit a final usage chunk so the stream can
                # still be metered. Never clobber an explicit caller setting.
                opts = payload.get("stream_options")
                if not isinstance(opts, dict):
                    payload["stream_options"] = {"include_usage": True}
                elif "include_usage" not in opts:
                    opts["include_usage"] = True
                body = json.dumps(payload).encode("utf-8")

        try:
            upstream_key = self._require_upstream_key()
        except UpstreamNotConfigured as exc:
            self._record_and_fail(
                path, auth, client_ip, started, 503, str(exc), "upstream_not_configured"
            )
            return

        url = self.workspace.upstream_base_url + path[len("/v1") :]
        headers = {
            "Authorization": f"Bearer {upstream_key}",
            "Accept-Encoding": "identity",
        }
        for name, value in self.headers.items():
            if name.lower() in FORWARDED_REQUEST_HEADERS:
                headers[name] = value
        if body is not None:
            headers["Content-Type"] = headers.get("Content-Type", "application/json")
            headers["Content-Length"] = str(len(body))

        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        timeout = float(self.workspace.config.get("request_timeout_s", 600))

        try:
            response = urllib.request.urlopen(request, timeout=timeout)  # noqa: S310
        except urllib.error.HTTPError as exc:
            self._relay_error_response(exc, path, auth, client_ip, started, model)
            return
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
            self._record_and_fail(
                path, auth, client_ip, started, 502,
                f"could not reach upstream {self.workspace.upstream_base_url}: {exc}",
                "upstream_unreachable",
            )
            return

        with response:
            if streaming:
                self._relay_stream(response, path, auth, client_ip, started, model)
            else:
                self._relay_buffered(response, path, auth, client_ip, started, model)

    def _require_upstream_key(self) -> str:
        key = self.workspace.upstream_api_key()
        if not key:
            env_name = self.workspace.config.get(
                "upstream_api_key_env", "KEYGATE_UPSTREAM_API_KEY"
            )
            raise UpstreamNotConfigured(
                f"no upstream API key: set ${env_name} or 'upstream_api_key' in "
                f"{self.workspace.path}/config.json"
            )
        return key

    def _record_and_fail(
        self, path, auth, client_ip, started, status, message, code
    ) -> None:
        self.workspace.store.record_request(
            endpoint=path,
            status=status,
            key_id=auth.key.id if auth.key else None,
            user_id=auth.user.id if auth.user else None,
            latency_ms=int((time.monotonic() - started) * 1000),
            client_ip=client_ip,
            note=code,
        )
        self._send_error_json(status, message, code)

    def _relay_response_headers(self, status: int, source: Any) -> None:
        self.send_response(status)
        for name, value in source.headers.items():
            lowered = name.lower()
            if any(lowered.startswith(p) for p in FORWARDED_RESPONSE_HEADER_PREFIXES):
                self.send_header(name, value)

    def _relay_buffered(self, response, path, auth, client_ip, started, model) -> None:
        payload = response.read()
        status = int(response.status)
        prompt_t, completion_t, total_t, model = _usage_from_json(payload, model)
        cost = self.workspace.cost_usd(model, prompt_t, completion_t)

        self.workspace.store.record_request(
            endpoint=path,
            status=status,
            key_id=auth.key.id,
            user_id=auth.user.id,
            model=model,
            prompt_tokens=prompt_t,
            completion_tokens=completion_t,
            total_tokens=total_t,
            cost_usd=cost,
            latency_ms=int((time.monotonic() - started) * 1000),
            client_ip=client_ip,
        )

        self._relay_response_headers(status, response)
        self.send_header(
            "Content-Type", response.headers.get("Content-Type", "application/json")
        )
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _relay_stream(self, response, path, auth, client_ip, started, model) -> None:
        """Relay server-sent events, harvesting the trailing usage chunk."""
        status = int(response.status)
        self._relay_response_headers(status, response)
        self.send_header(
            "Content-Type", response.headers.get("Content-Type", "text/event-stream")
        )
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        prompt_t = completion_t = total_t = 0
        note: str | None = "usage_unavailable"
        try:
            while True:
                line = response.readline()
                if not line:
                    break
                self.wfile.write(b"%x\r\n" % len(line) + line + b"\r\n")
                self.wfile.flush()
                usage = _usage_from_sse_line(line)
                if usage is not None:
                    prompt_t, completion_t, total_t, seen_model = usage
                    model = seen_model or model
                    note = None
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # Client hung up mid-stream. Bill for what the upstream already
            # produced and let the connection close.
            note = "client_disconnected"
            self.close_connection = True

        self.workspace.store.record_request(
            endpoint=path,
            status=status,
            key_id=auth.key.id,
            user_id=auth.user.id,
            model=model,
            streamed=True,
            prompt_tokens=prompt_t,
            completion_tokens=completion_t,
            total_tokens=total_t,
            cost_usd=self.workspace.cost_usd(model, prompt_t, completion_t),
            latency_ms=int((time.monotonic() - started) * 1000),
            client_ip=client_ip,
            note=note,
        )

    def _relay_error_response(
        self, exc: urllib.error.HTTPError, path, auth, client_ip, started, model
    ) -> None:
        payload = exc.read() or _error_body(
            f"upstream returned {exc.code}", "upstream_error"
        )
        status = int(exc.code)
        self.workspace.store.record_request(
            endpoint=path,
            status=status,
            key_id=auth.key.id,
            user_id=auth.user.id,
            model=model,
            latency_ms=int((time.monotonic() - started) * 1000),
            client_ip=client_ip,
            note="upstream_error",
        )
        self._relay_response_headers(status, exc)
        self.send_header(
            "Content-Type", exc.headers.get("Content-Type", "application/json")
        )
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _coerce_usage(
    usage: Any, model: str | None
) -> tuple[int, int, int, str | None] | None:
    if not isinstance(usage, dict):
        return None
    prompt_t = int(usage.get("prompt_tokens") or 0)
    completion_t = int(usage.get("completion_tokens") or 0)
    total_t = int(usage.get("total_tokens") or (prompt_t + completion_t))
    return prompt_t, completion_t, total_t, model


def _usage_from_json(payload: bytes, model: str | None) -> tuple[int, int, int, str | None]:
    """Pull token counts out of a non-streamed completion response."""
    try:
        parsed = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return 0, 0, 0, model
    if not isinstance(parsed, dict):
        return 0, 0, 0, model
    if isinstance(parsed.get("model"), str):
        model = parsed["model"]
    coerced = _coerce_usage(parsed.get("usage"), model)
    return coerced if coerced is not None else (0, 0, 0, model)


def _usage_from_sse_line(line: bytes) -> tuple[int, int, int, str | None] | None:
    """Return usage from an SSE ``data:`` line, or ``None`` if it carries none."""
    stripped = line.strip()
    if not stripped.startswith(b"data:"):
        return None
    data = stripped[len(b"data:") :].strip()
    if not data or data == b"[DONE]":
        return None
    try:
        parsed = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(parsed, dict) or not parsed.get("usage"):
        return None
    model = parsed.get("model") if isinstance(parsed.get("model"), str) else None
    return _coerce_usage(parsed["usage"], model)


def make_handler(workspace: Workspace, quiet: bool = False) -> type[GatewayHandler]:
    """Bind ``workspace`` to a fresh handler class."""
    return type(
        "BoundGatewayHandler",
        (GatewayHandler,),
        {"workspace": workspace, "quiet": quiet},
    )


def build_server(
    workspace: Workspace,
    host: str | None = None,
    port: int | None = None,
    quiet: bool = False,
) -> ThreadingHTTPServer:
    """Create (but do not start) the gateway server."""
    host = host if host is not None else str(workspace.config["listen_host"])
    port = port if port is not None else int(workspace.config["listen_port"])
    server = ThreadingHTTPServer((host, port), make_handler(workspace, quiet))
    server.daemon_threads = True
    return server
