"""Admin JSON API and the packaged dashboard assets.

:func:`dispatch` turns a request under ``/admin/api/`` into a
``(status, payload)`` pair; :mod:`keygate.proxy` owns the socket and just
serialises whatever comes back. Keeping the two apart means the whole admin
surface is testable without a server, and the proxy's hot path stays readable.

**There is no authentication here.** Anyone who can open a TCP connection to
the gateway can mint and revoke keys. That is why ``listen_host`` defaults to
``127.0.0.1`` and why ``keygate serve`` shouts when you move it. Put it behind
TLS and a trusted network before binding anything routable.
"""

from __future__ import annotations

import json
import re
from importlib import resources
from typing import Any
from urllib.parse import parse_qs, unquote

from . import __version__
from .store import KeygateError, Workspace, parse_since

#: Everything the admin API answers lives under this prefix.
API_PREFIX = "/admin/api"

#: Where the single-page dashboard is served from.
DASH_PREFIX = "/dash"

INDEX_NAME = "index.html"

CONTENT_TYPES = {
    "html": "text/html; charset=utf-8",
    "css": "text/css; charset=utf-8",
    "js": "text/javascript; charset=utf-8",
    "json": "application/json",
    "svg": "image/svg+xml",
    "png": "image/png",
    "ico": "image/x-icon",
    "woff2": "font/woff2",
    "map": "application/json",
}

#: Asset names we are willing to look up. No slashes, no dot-segments, so a
#: request can never climb out of the dashboard directory.
_SAFE_ASSET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

#: Audit actor recorded for mutations that arrive over HTTP rather than the CLI.
ACTOR = "dashboard"

#: Rolling window used for the "last 24h" figures on the overview.
RECENT_WINDOW = "24h"

MAX_LIMIT = 1000


class AdminError(Exception):
    """An admin request that should come back as a clean JSON error."""

    def __init__(self, message: str, code: str = "bad_request", status: int = 400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


# ---------------------------------------------------------------------------
# static assets
# ---------------------------------------------------------------------------


def read_asset(name: str) -> tuple[bytes, str] | None:
    """Return ``(bytes, content_type)`` for a packaged dashboard file.

    ``None`` means "no such asset" -- including for names that look like a
    traversal attempt, which are refused before they ever touch the filesystem.
    """
    if not _SAFE_ASSET.match(name):
        return None
    # Chained joinpath rather than joinpath("dashboard", name): multiple
    # descendants only became legal on Traversable in 3.11.
    resource = resources.files(__package__).joinpath("dashboard").joinpath(name)
    try:
        if not resource.is_file():
            return None
        payload = resource.read_bytes()
    except (OSError, FileNotFoundError):  # pragma: no cover - unreadable install
        return None
    suffix = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return payload, CONTENT_TYPES.get(suffix, "application/octet-stream")


# ---------------------------------------------------------------------------
# request helpers
# ---------------------------------------------------------------------------


def error_payload(message: str, code: str) -> dict[str, Any]:
    return {"error": {"message": message, "code": code}}


def _json_body(body: bytes | None) -> dict[str, Any]:
    if not body:
        return {}
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise AdminError("request body must be JSON", "invalid_json") from None
    if not isinstance(parsed, dict):
        raise AdminError("request body must be a JSON object", "invalid_json")
    return parsed


def _one(query: dict[str, list[str]], name: str) -> str | None:
    values = query.get(name)
    if not values:
        return None
    value = values[-1].strip()
    return value or None


def _flag(query: dict[str, list[str]], name: str) -> bool:
    value = _one(query, name)
    return value is not None and value.lower() not in {"0", "false", "no", ""}


def _limit(query: dict[str, list[str]], default: int) -> int:
    raw = _one(query, "limit")
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise AdminError(f"limit must be a whole number, got {raw!r}") from None
    return max(1, min(value, MAX_LIMIT))


def _opt_money(payload: dict[str, Any], name: str) -> float | None:
    """Read an optional USD amount. Absent, ``null`` and ``""`` all mean none."""
    value = payload.get(name)
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise AdminError(f"{name} must be a number")
    try:
        amount = float(value)
    except (TypeError, ValueError):
        raise AdminError(f"{name} must be a number, got {value!r}") from None
    if amount != amount or amount in (float("inf"), float("-inf")):
        raise AdminError(f"{name} must be a finite number")
    if amount < 0:
        raise AdminError(f"{name} may not be negative")
    return amount


def _opt_text(payload: dict[str, Any], name: str) -> str | None:
    value = payload.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise AdminError(f"{name} must be a string")
    return value.strip() or None


def _since(query: dict[str, list[str]]) -> str | None:
    raw = _one(query, "since")
    return parse_since(raw) if raw else None


def _round(value: float) -> float:
    return round(float(value), 6)


# ---------------------------------------------------------------------------
# serialisers
# ---------------------------------------------------------------------------


def _user_json(store, user) -> dict[str, Any]:
    spent = store.spent_for_user(user.id)
    remaining = None if user.budget_usd is None else max(0.0, user.budget_usd - spent)
    return {
        "name": user.name,
        "email": user.email,
        "budget_usd": user.budget_usd,
        "spent_usd": _round(spent),
        "remaining_usd": None if remaining is None else _round(remaining),
        "live_keys": len(store.list_keys(user.name)),
        "disabled": user.disabled,
        "created_at": user.created_at,
    }


def _key_json(store, key, user_names: dict[int, str]) -> dict[str, Any]:
    return {
        "prefix": key.display_prefix,
        "user": user_names.get(key.user_id),
        "label": key.label,
        "budget_usd": key.budget_usd,
        "spent_usd": _round(store.spent_for_key(key.id)),
        "created_at": key.created_at,
        "last_used_at": key.last_used_at,
        "revoked_at": key.revoked_at,
        "revoked": key.revoked,
    }


# ---------------------------------------------------------------------------
# endpoints
# ---------------------------------------------------------------------------


def _overview(workspace: Workspace) -> dict[str, Any]:
    store = workspace.store
    lifetime = store.usage_summary()
    recent = store.usage_summary(parse_since(RECENT_WINDOW))
    return {
        "version": __version__,
        "workspace": str(workspace.path),
        "upstream_base_url": workspace.upstream_base_url,
        "upstream_key_configured": bool(workspace.upstream_api_key()),
        "users": len(store.list_users()),
        "live_keys": len(store.list_keys()),
        "total_spent_usd": _round(sum(row["cost_usd"] for row in lifetime)),
        "requests_total": sum(row["requests"] for row in lifetime),
        "requests_24h": sum(row["requests"] for row in recent),
        "errors_24h": sum(row["errors"] for row in recent),
        "spent_24h": _round(sum(row["cost_usd"] for row in recent)),
        "recent": store.usage_rows(limit=8),
    }


def _list_users(workspace: Workspace) -> dict[str, Any]:
    store = workspace.store
    return {"users": [_user_json(store, u) for u in store.list_users()]}


def _create_user(workspace: Workspace, body: bytes | None) -> dict[str, Any]:
    payload = _json_body(body)
    name = _opt_text(payload, "name")
    if not name:
        raise AdminError("name is required", "missing_field")
    user = workspace.store.add_user(
        name,
        email=_opt_text(payload, "email"),
        budget_usd=_opt_money(payload, "budget_usd"),
        actor=ACTOR,
    )
    return {"user": _user_json(workspace.store, user)}


def _patch_user(workspace: Workspace, name: str, body: bytes | None) -> dict[str, Any]:
    payload = _json_body(body)
    if "budget_usd" not in payload:
        raise AdminError("nothing to update; send budget_usd", "missing_field")
    user = workspace.store.set_user_budget(
        name, _opt_money(payload, "budget_usd"), actor=ACTOR
    )
    return {"user": _user_json(workspace.store, user)}


def _list_keys(workspace: Workspace, query: dict[str, list[str]]) -> dict[str, Any]:
    store = workspace.store
    user_names = {u.id: u.name for u in store.list_users()}
    keys = store.list_keys(_one(query, "user"), include_revoked=_flag(query, "all"))
    return {"keys": [_key_json(store, k, user_names) for k in keys]}


def _mint_key(workspace: Workspace, body: bytes | None) -> dict[str, Any]:
    payload = _json_body(body)
    user = _opt_text(payload, "user")
    if not user:
        raise AdminError("user is required", "missing_field")
    plaintext, key = workspace.store.mint_key(
        user,
        label=_opt_text(payload, "label"),
        budget_usd=_opt_money(payload, "budget_usd"),
        actor=ACTOR,
    )
    # The only response that ever carries the plaintext. It is not recoverable
    # afterwards -- only its SHA-256 digest was stored.
    return {
        "key": plaintext,
        "prefix": key.display_prefix,
        "user": user,
        "label": key.label,
        "budget_usd": key.budget_usd,
        "created_at": key.created_at,
    }


def _revoke_key(workspace: Workspace, prefix: str) -> dict[str, Any]:
    key = workspace.store.revoke_key(prefix, actor=ACTOR)
    user_names = {u.id: u.name for u in workspace.store.list_users()}
    return {"key": _key_json(workspace.store, key, user_names)}


def _usage(workspace: Workspace, query: dict[str, list[str]]) -> dict[str, Any]:
    store = workspace.store
    since = _since(query)
    user = _one(query, "user")
    if _flag(query, "detail"):
        return {
            "detail": True,
            "rows": store.usage_rows(user, since, _limit(query, 100)),
        }
    summary = store.usage_summary(since)
    if user is not None:
        store.require_user(user)
        summary = [row for row in summary if row["user"] == user]
    return {
        "detail": False,
        "summary": summary,
        "total_usd": _round(sum(row["cost_usd"] for row in summary)),
    }


def _audit(workspace: Workspace, query: dict[str, list[str]]) -> dict[str, Any]:
    return {"rows": workspace.store.audit_rows(_limit(query, 100))}


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------


def dispatch(
    workspace: Workspace,
    method: str,
    path: str,
    query_string: str = "",
    body: bytes | None = None,
) -> tuple[int, Any]:
    """Route one admin call.

    ``path`` is the full request path (``/admin/api/users``); the query string
    is passed separately. Returns ``(status, json_serialisable_payload)`` and
    never raises for ordinary bad input.
    """
    query = parse_qs(query_string or "")
    route = path[len(API_PREFIX) :].strip("/")
    parts = [unquote(p) for p in route.split("/")] if route else []

    try:
        return _route(workspace, method, parts, query, body)
    except AdminError as exc:
        return exc.status, error_payload(exc.message, exc.code)
    except KeygateError as exc:
        return 400, error_payload(str(exc), "keygate_error")


def _route(
    workspace: Workspace,
    method: str,
    parts: list[str],
    query: dict[str, list[str]],
    body: bytes | None,
) -> tuple[int, Any]:
    head = parts[0] if parts else ""

    if head == "overview" and len(parts) == 1:
        _require(method, "GET")
        return 200, _overview(workspace)

    if head == "users":
        if len(parts) == 1:
            if method == "GET":
                return 200, _list_users(workspace)
            if method == "POST":
                return 201, _create_user(workspace, body)
            raise _not_allowed(method, "GET, POST")
        if len(parts) == 2:
            _require(method, "PATCH")
            return 200, _patch_user(workspace, parts[1], body)

    if head == "keys":
        if len(parts) == 1:
            if method == "GET":
                return 200, _list_keys(workspace, query)
            if method == "POST":
                return 201, _mint_key(workspace, body)
            raise _not_allowed(method, "GET, POST")
        if len(parts) == 3 and parts[2] == "revoke":
            _require(method, "POST")
            return 200, _revoke_key(workspace, parts[1])

    if head == "usage" and len(parts) == 1:
        _require(method, "GET")
        return 200, _usage(workspace, query)

    if head == "audit" and len(parts) == 1:
        _require(method, "GET")
        return 200, _audit(workspace, query)

    raise AdminError(
        f"unknown admin route {API_PREFIX}/{'/'.join(parts)}", "not_found", 404
    )


def _require(method: str, expected: str) -> None:
    if method != expected:
        raise _not_allowed(method, expected)


def _not_allowed(method: str, allowed: str) -> AdminError:
    return AdminError(f"{method} not allowed here; use {allowed}", "method_not_allowed", 405)
