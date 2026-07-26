"""SQLite persistence and workspace handling for keygate.

A *workspace* is a directory holding two files:

``config.json``
    Upstream endpoint, listen address and the token price table.
``keygate.db``
    SQLite database with users, virtual keys, the per-request usage log and
    the admin audit log.

Everything here is stdlib-only. Connections are per-thread so the threaded
HTTP server in :mod:`keygate.proxy` can share one :class:`Store`.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import keys as keymod

SCHEMA_VERSION = 1

CONFIG_NAME = "config.json"
DB_NAME = "keygate.db"

#: Token prices in USD per one million tokens.
#:
#: These are a starting point, not a source of truth -- provider prices change
#: and keygate has no way to discover them. Edit ``config.json`` to match your
#: contract. Any model not listed falls back to the ``"default"`` entry, which
#: is zero-cost so that an unknown model is never silently billed at a wrong
#: rate. Set a non-zero default if you would rather over-count than under-count.
DEFAULT_PRICING: dict[str, dict[str, float]] = {
    "default": {"input": 0.0, "output": 0.0},
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
}

PRICING_NOTE = (
    "USD per 1M tokens. Unverified starting values -- confirm against your "
    "provider's current price list. Models absent from this table are billed "
    "at the 'default' rate."
)

DEFAULT_CONFIG: dict[str, Any] = {
    "upstream_base_url": "https://api.openai.com/v1",
    "upstream_api_key_env": "KEYGATE_UPSTREAM_API_KEY",
    "upstream_api_key": None,
    "listen_host": "127.0.0.1",
    "listen_port": 8787,
    "request_timeout_s": 600,
    "max_request_bytes": 10 * 1024 * 1024,
    "pricing_note": PRICING_NOTE,
    "pricing": DEFAULT_PRICING,
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    email      TEXT,
    budget_usd REAL,
    created_at TEXT NOT NULL,
    disabled_at TEXT
);

CREATE TABLE IF NOT EXISTS keys (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL REFERENCES users(id),
    key_hash       TEXT NOT NULL UNIQUE,
    display_prefix TEXT NOT NULL,
    label          TEXT,
    budget_usd     REAL,
    created_at     TEXT NOT NULL,
    last_used_at   TEXT,
    revoked_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_keys_user ON keys(user_id);
CREATE INDEX IF NOT EXISTS idx_keys_prefix ON keys(display_prefix);

CREATE TABLE IF NOT EXISTS requests (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                TEXT NOT NULL,
    key_id            INTEGER REFERENCES keys(id),
    user_id           INTEGER REFERENCES users(id),
    endpoint          TEXT NOT NULL,
    model             TEXT,
    status            INTEGER NOT NULL,
    streamed          INTEGER NOT NULL DEFAULT 0,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens      INTEGER NOT NULL DEFAULT 0,
    cost_usd          REAL NOT NULL DEFAULT 0,
    latency_ms        INTEGER NOT NULL DEFAULT 0,
    client_ip         TEXT,
    note              TEXT
);
CREATE INDEX IF NOT EXISTS idx_requests_user_ts ON requests(user_id, ts);
CREATE INDEX IF NOT EXISTS idx_requests_key_ts ON requests(key_id, ts);

CREATE TABLE IF NOT EXISTS audit (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     TEXT NOT NULL,
    actor  TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts);
"""


class KeygateError(Exception):
    """Anything the CLI should report as a clean error rather than a traceback."""


def utcnow() -> str:
    """Current UTC time as a sortable ISO-8601 string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_since(spec: str) -> str:
    """Turn ``7d`` / ``12h`` / ``30m`` / an ISO date into an ISO-8601 lower bound."""
    spec = spec.strip()
    units = {"d": "days", "h": "hours", "m": "minutes"}
    if len(spec) > 1 and spec[-1] in units and spec[:-1].isdigit():
        delta = timedelta(**{units[spec[-1]]: int(spec[:-1])})
        return (datetime.now(timezone.utc) - delta).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    try:
        parsed = datetime.fromisoformat(spec.replace("Z", "+00:00"))
    except ValueError:
        raise KeygateError(
            f"could not read --since {spec!r}; use 7d, 12h, 30m or an ISO date"
        ) from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(frozen=True)
class User:
    id: int
    name: str
    email: str | None
    budget_usd: float | None
    created_at: str
    disabled_at: str | None

    @property
    def disabled(self) -> bool:
        return self.disabled_at is not None


@dataclass(frozen=True)
class Key:
    id: int
    user_id: int
    display_prefix: str
    label: str | None
    budget_usd: float | None
    created_at: str
    last_used_at: str | None
    revoked_at: str | None

    @property
    def revoked(self) -> bool:
        return self.revoked_at is not None


@dataclass(frozen=True)
class AuthResult:
    """Outcome of authenticating a presented virtual key."""

    ok: bool
    key: Key | None = None
    user: User | None = None
    #: Machine-readable reason when ``ok`` is false.
    code: str | None = None
    message: str | None = None
    #: HTTP status the proxy should return when ``ok`` is false.
    status: int = 401


def _row_to_user(row: sqlite3.Row) -> User:
    return User(
        id=row["id"],
        name=row["name"],
        email=row["email"],
        budget_usd=row["budget_usd"],
        created_at=row["created_at"],
        disabled_at=row["disabled_at"],
    )


def _row_to_key(row: sqlite3.Row) -> Key:
    return Key(
        id=row["id"],
        user_id=row["user_id"],
        display_prefix=row["display_prefix"],
        label=row["label"],
        budget_usd=row["budget_usd"],
        created_at=row["created_at"],
        last_used_at=row["last_used_at"],
        revoked_at=row["revoked_at"],
    )


class Store:
    """Thin typed wrapper over the SQLite database."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self._local = threading.local()

    # -- connection handling -------------------------------------------------

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, timeout=30.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def initialize(self) -> None:
        with self.conn:
            self.conn.executescript(SCHEMA)
            self.conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    def schema_version(self) -> int:
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        return int(row["value"]) if row else 0

    # -- users ---------------------------------------------------------------

    def add_user(
        self,
        name: str,
        email: str | None = None,
        budget_usd: float | None = None,
        actor: str = "cli",
    ) -> User:
        name = name.strip()
        if not name:
            raise KeygateError("user name may not be empty")
        if budget_usd is not None and budget_usd < 0:
            raise KeygateError("budget may not be negative")
        try:
            with self.conn:
                cur = self.conn.execute(
                    "INSERT INTO users(name, email, budget_usd, created_at) "
                    "VALUES(?, ?, ?, ?)",
                    (name, email, budget_usd, utcnow()),
                )
        except sqlite3.IntegrityError:
            raise KeygateError(f"user {name!r} already exists") from None
        self.log_audit(actor, "user.add", name, json.dumps({"budget_usd": budget_usd}))
        user = self.get_user_by_id(int(cur.lastrowid))
        assert user is not None
        return user

    def get_user(self, name: str) -> User | None:
        row = self.conn.execute(
            "SELECT * FROM users WHERE name = ?", (name,)
        ).fetchone()
        return _row_to_user(row) if row else None

    def require_user(self, name: str) -> User:
        user = self.get_user(name)
        if user is None:
            raise KeygateError(f"no such user: {name!r}")
        return user

    def get_user_by_id(self, user_id: int) -> User | None:
        row = self.conn.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        return _row_to_user(row) if row else None

    def list_users(self) -> list[User]:
        rows = self.conn.execute("SELECT * FROM users ORDER BY name").fetchall()
        return [_row_to_user(r) for r in rows]

    def set_user_budget(
        self, name: str, budget_usd: float | None, actor: str = "cli"
    ) -> User:
        user = self.require_user(name)
        if budget_usd is not None and budget_usd < 0:
            raise KeygateError("budget may not be negative")
        with self.conn:
            self.conn.execute(
                "UPDATE users SET budget_usd = ? WHERE id = ?", (budget_usd, user.id)
            )
        self.log_audit(
            actor, "user.budget", name, json.dumps({"budget_usd": budget_usd})
        )
        refreshed = self.get_user_by_id(user.id)
        assert refreshed is not None
        return refreshed

    # -- keys ----------------------------------------------------------------

    def mint_key(
        self,
        user_name: str,
        label: str | None = None,
        budget_usd: float | None = None,
        actor: str = "cli",
    ) -> tuple[str, Key]:
        """Mint a key for ``user_name``; returns ``(plaintext, key)``.

        The plaintext is not recoverable afterwards.
        """
        user = self.require_user(user_name)
        if budget_usd is not None and budget_usd < 0:
            raise KeygateError("budget may not be negative")
        minted = keymod.mint()
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO keys(user_id, key_hash, display_prefix, label, "
                "budget_usd, created_at) VALUES(?, ?, ?, ?, ?, ?)",
                (
                    user.id,
                    minted.key_hash,
                    minted.display_prefix,
                    label,
                    budget_usd,
                    utcnow(),
                ),
            )
        self.log_audit(
            actor,
            "key.mint",
            minted.display_prefix,
            json.dumps({"user": user.name, "label": label, "budget_usd": budget_usd}),
        )
        key = self.get_key_by_id(int(cur.lastrowid))
        assert key is not None
        return minted.plaintext, key

    def get_key_by_id(self, key_id: int) -> Key | None:
        row = self.conn.execute("SELECT * FROM keys WHERE id = ?", (key_id,)).fetchone()
        return _row_to_key(row) if row else None

    def list_keys(
        self, user_name: str | None = None, include_revoked: bool = False
    ) -> list[Key]:
        sql = "SELECT k.* FROM keys k JOIN users u ON u.id = k.user_id WHERE 1=1"
        params: list[Any] = []
        if user_name is not None:
            self.require_user(user_name)
            sql += " AND u.name = ?"
            params.append(user_name)
        if not include_revoked:
            sql += " AND k.revoked_at IS NULL"
        sql += " ORDER BY k.created_at"
        return [_row_to_key(r) for r in self.conn.execute(sql, params).fetchall()]

    def revoke_key(self, prefix: str, actor: str = "cli") -> Key:
        """Revoke the single live key whose display prefix starts with ``prefix``."""
        rows = self.conn.execute(
            "SELECT * FROM keys WHERE display_prefix LIKE ? AND revoked_at IS NULL",
            (prefix + "%",),
        ).fetchall()
        if not rows:
            raise KeygateError(f"no live key matching prefix {prefix!r}")
        if len(rows) > 1:
            raise KeygateError(
                f"prefix {prefix!r} matches {len(rows)} keys; be more specific"
            )
        key = _row_to_key(rows[0])
        with self.conn:
            self.conn.execute(
                "UPDATE keys SET revoked_at = ? WHERE id = ?", (utcnow(), key.id)
            )
        self.log_audit(actor, "key.revoke", key.display_prefix, None)
        refreshed = self.get_key_by_id(key.id)
        assert refreshed is not None
        return refreshed

    # -- authentication and budgets -----------------------------------------

    def authenticate(self, presented: str | None) -> AuthResult:
        """Resolve a presented virtual key to a user, enforcing budgets."""
        if not presented:
            return AuthResult(
                False,
                code="missing_api_key",
                message="no Authorization header; send 'Authorization: Bearer kg_v1_...'",
            )
        if not keymod.looks_like_key(presented):
            return AuthResult(
                False,
                code="invalid_api_key",
                message="not a keygate virtual key (expected kg_v1_...)",
            )
        row = self.conn.execute(
            "SELECT * FROM keys WHERE key_hash = ?", (keymod.hash_key(presented),)
        ).fetchone()
        if row is None:
            return AuthResult(False, code="invalid_api_key", message="unknown key")
        key = _row_to_key(row)
        if key.revoked:
            return AuthResult(
                False, code="key_revoked", message="this key has been revoked"
            )
        user = self.get_user_by_id(key.user_id)
        if user is None:  # pragma: no cover - foreign keys make this unreachable
            return AuthResult(False, code="invalid_api_key", message="orphaned key")
        if user.disabled:
            return AuthResult(
                False,
                code="user_disabled",
                message=f"user {user.name!r} is disabled",
                status=403,
            )

        if key.budget_usd is not None:
            spent = self.spent_for_key(key.id)
            if spent >= key.budget_usd:
                return AuthResult(
                    False,
                    key=key,
                    user=user,
                    code="budget_exceeded",
                    message=(
                        f"key {key.display_prefix} has spent "
                        f"${spent:.4f} of its ${key.budget_usd:.2f} budget"
                    ),
                    status=402,
                )
        if user.budget_usd is not None:
            spent = self.spent_for_user(user.id)
            if spent >= user.budget_usd:
                return AuthResult(
                    False,
                    key=key,
                    user=user,
                    code="budget_exceeded",
                    message=(
                        f"user {user.name} has spent ${spent:.4f} of their "
                        f"${user.budget_usd:.2f} budget"
                    ),
                    status=402,
                )
        return AuthResult(True, key=key, user=user)

    def spent_for_key(self, key_id: int) -> float:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) AS s FROM requests WHERE key_id = ?",
            (key_id,),
        ).fetchone()
        return float(row["s"])

    def spent_for_user(self, user_id: int) -> float:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) AS s FROM requests WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return float(row["s"])

    def touch_key(self, key_id: int) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE keys SET last_used_at = ? WHERE id = ?", (utcnow(), key_id)
            )

    # -- request log ---------------------------------------------------------

    def record_request(
        self,
        *,
        endpoint: str,
        status: int,
        key_id: int | None = None,
        user_id: int | None = None,
        model: str | None = None,
        streamed: bool = False,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        cost_usd: float = 0.0,
        latency_ms: int = 0,
        client_ip: str | None = None,
        note: str | None = None,
    ) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO requests(ts, key_id, user_id, endpoint, model, status, "
                "streamed, prompt_tokens, completion_tokens, total_tokens, cost_usd, "
                "latency_ms, client_ip, note) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    utcnow(),
                    key_id,
                    user_id,
                    endpoint,
                    model,
                    status,
                    int(streamed),
                    prompt_tokens,
                    completion_tokens,
                    total_tokens,
                    cost_usd,
                    latency_ms,
                    client_ip,
                    note,
                ),
            )
        return int(cur.lastrowid)

    def usage_summary(self, since: str | None = None) -> list[dict[str, Any]]:
        """Per-user rollup of the request log."""
        params: list[Any] = []
        clause = ""
        if since:
            clause = " AND r.ts >= ?"
            params.append(since)
        sql = f"""
            SELECT u.name AS user,
                   u.budget_usd AS budget_usd,
                   COUNT(r.id) AS requests,
                   COALESCE(SUM(r.prompt_tokens), 0) AS prompt_tokens,
                   COALESCE(SUM(r.completion_tokens), 0) AS completion_tokens,
                   COALESCE(SUM(r.total_tokens), 0) AS total_tokens,
                   COALESCE(SUM(r.cost_usd), 0) AS cost_usd,
                   COALESCE(SUM(CASE WHEN r.status >= 400 THEN 1 ELSE 0 END), 0)
                       AS errors
            FROM users u
            LEFT JOIN requests r ON r.user_id = u.id{clause}
            GROUP BY u.id
            ORDER BY cost_usd DESC, u.name
        """
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def usage_rows(
        self, user_name: str | None = None, since: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Most recent individual requests, newest first."""
        sql = (
            "SELECT r.*, u.name AS user, k.display_prefix AS key_prefix "
            "FROM requests r "
            "LEFT JOIN users u ON u.id = r.user_id "
            "LEFT JOIN keys k ON k.id = r.key_id WHERE 1=1"
        )
        params: list[Any] = []
        if user_name is not None:
            self.require_user(user_name)
            sql += " AND u.name = ?"
            params.append(user_name)
        if since:
            sql += " AND r.ts >= ?"
            params.append(since)
        sql += " ORDER BY r.ts DESC, r.id DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    # -- audit log -----------------------------------------------------------

    def log_audit(
        self, actor: str, action: str, target: str | None, detail: str | None
    ) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO audit(ts, actor, action, target, detail) VALUES(?,?,?,?,?)",
                (utcnow(), actor, action, target, detail),
            )

    def audit_rows(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM audit ORDER BY ts DESC, id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


class Workspace:
    """A keygate directory: config plus database."""

    def __init__(self, path: Path, config: dict[str, Any], store: Store) -> None:
        self.path = path
        self.config = config
        self.store = store

    # -- lifecycle -----------------------------------------------------------

    @classmethod
    def create(
        cls,
        path: Path | str,
        upstream_base_url: str | None = None,
        force: bool = False,
    ) -> "Workspace":
        path = Path(path).expanduser()
        config_path = path / CONFIG_NAME
        if config_path.exists() and not force:
            raise KeygateError(
                f"{config_path} already exists; pass --force to overwrite the config"
            )
        path.mkdir(parents=True, exist_ok=True)
        config = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
        if upstream_base_url:
            config["upstream_base_url"] = upstream_base_url.rstrip("/")
        _write_config(config_path, config)
        store = Store(path / DB_NAME)
        store.initialize()
        store.log_audit("cli", "workspace.init", str(path), None)
        return cls(path, config, store)

    @classmethod
    def open(cls, path: Path | str) -> "Workspace":
        path = Path(path).expanduser()
        config_path = path / CONFIG_NAME
        if not config_path.exists():
            raise KeygateError(
                f"no keygate workspace at {path} (missing {CONFIG_NAME}); "
                f"run 'keygate init {path}' first"
            )
        try:
            loaded = json.loads(config_path.read_text("utf-8"))
        except json.JSONDecodeError as exc:
            raise KeygateError(f"{config_path} is not valid JSON: {exc}") from None
        config = json.loads(json.dumps(DEFAULT_CONFIG))
        config.update(loaded)
        store = Store(path / DB_NAME)
        store.initialize()
        return cls(path, config, store)

    def save_config(self) -> None:
        _write_config(self.path / CONFIG_NAME, self.config)

    # -- derived settings ----------------------------------------------------

    @property
    def upstream_base_url(self) -> str:
        return str(self.config["upstream_base_url"]).rstrip("/")

    def upstream_api_key(self) -> str | None:
        """Resolve the real upstream key: environment first, then config."""
        env_name = self.config.get("upstream_api_key_env")
        if env_name:
            value = os.environ.get(str(env_name))
            if value:
                return value.strip()
        literal = self.config.get("upstream_api_key")
        if literal:
            return str(literal).strip()
        return None

    def price_for(self, model: str | None) -> tuple[float, float]:
        """Return ``(input, output)`` USD per 1M tokens for ``model``.

        Falls back from an exact match, to the longest configured prefix match
        (so ``gpt-4o-2024-11-20`` picks up ``gpt-4o``), to ``default``.
        """
        pricing: dict[str, Any] = self.config.get("pricing") or {}
        default = pricing.get("default") or {"input": 0.0, "output": 0.0}
        if not model:
            return float(default.get("input", 0.0)), float(default.get("output", 0.0))
        entry = pricing.get(model)
        if entry is None:
            candidates = [
                name
                for name in pricing
                if name != "default" and model.startswith(name)
            ]
            if candidates:
                entry = pricing[max(candidates, key=len)]
        if entry is None:
            entry = default
        return float(entry.get("input", 0.0)), float(entry.get("output", 0.0))

    def cost_usd(
        self, model: str | None, prompt_tokens: int, completion_tokens: int
    ) -> float:
        in_price, out_price = self.price_for(model)
        return (prompt_tokens * in_price + completion_tokens * out_price) / 1_000_000


def _write_config(config_path: Path, config: dict[str, Any]) -> None:
    """Write ``config.json`` owner-readable only -- it can hold the upstream key."""
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(config_path, 0o600)
    except OSError:  # pragma: no cover - e.g. exotic filesystems
        pass


def default_workspace_path() -> Path:
    """Workspace used when ``--dir`` is not given."""
    return Path(os.environ.get("KEYGATE_DIR", ".keygate")).expanduser()
