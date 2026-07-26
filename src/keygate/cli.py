"""Command line interface for keygate."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .proxy import build_server
from .store import (
    CONFIG_NAME,
    DB_NAME,
    KeygateError,
    Workspace,
    default_workspace_path,
    parse_since,
)


# ---------------------------------------------------------------------------
# output helpers
# ---------------------------------------------------------------------------


def _print_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> None:
    if not rows:
        print("(none)")
        return
    cells = [["" if c is None else str(c) for c in row] for row in rows]
    widths = [
        max([len(h)] + [len(row[i]) for row in cells]) for i, h in enumerate(headers)
    ]
    print("  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip())
    print("  ".join("-" * w for w in widths))
    for row in cells:
        print("  ".join(row[i].ljust(widths[i]) for i in range(len(headers))).rstrip())


def _money(value: float | None) -> str:
    return "-" if value is None else f"${value:,.4f}"


def _budget(value: float | None) -> str:
    return "unlimited" if value is None else f"${value:,.2f}"


def _short_ts(ts: str | None) -> str:
    return "-" if not ts else ts[:19].replace("T", " ")


def _dump_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, default=str))


def _open(args: argparse.Namespace) -> Workspace:
    return Workspace.open(args.dir)


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    workspace = Workspace.create(
        args.path, upstream_base_url=args.upstream_base_url, force=args.force
    )
    env_name = workspace.config["upstream_api_key_env"]
    print(f"initialized keygate workspace at {workspace.path}")
    print(f"  {CONFIG_NAME}  upstream: {workspace.upstream_base_url}")
    print(f"  {DB_NAME}   schema v{workspace.store.schema_version()}")
    print()
    print("next:")
    print(f"  export {env_name}=<your real upstream key>")
    print(f"  export KEYGATE_DIR={workspace.path}")
    print("  keygate user add alice --budget 25")
    print("  keygate user mint alice")
    print("  keygate serve")
    return 0


def cmd_user_add(args: argparse.Namespace) -> int:
    workspace = _open(args)
    user = workspace.store.add_user(args.name, args.email, args.budget)
    print(f"added user {user.name} (budget {_budget(user.budget_usd)})")
    return 0


def cmd_user_list(args: argparse.Namespace) -> int:
    workspace = _open(args)
    store = workspace.store
    users = store.list_users()
    if args.json:
        _dump_json(
            [
                {
                    "name": u.name,
                    "email": u.email,
                    "budget_usd": u.budget_usd,
                    "spent_usd": round(store.spent_for_user(u.id), 6),
                    "live_keys": len(store.list_keys(u.name)),
                    "created_at": u.created_at,
                    "disabled": u.disabled,
                }
                for u in users
            ]
        )
        return 0
    rows = [
        [
            u.name,
            u.email or "-",
            _budget(u.budget_usd),
            _money(store.spent_for_user(u.id)),
            len(store.list_keys(u.name)),
            _short_ts(u.created_at),
        ]
        for u in users
    ]
    _print_table(["USER", "EMAIL", "BUDGET", "SPENT", "KEYS", "CREATED"], rows)
    return 0


def cmd_user_mint(args: argparse.Namespace) -> int:
    workspace = _open(args)
    plaintext, key = workspace.store.mint_key(args.name, args.label, args.budget)
    if args.json:
        _dump_json(
            {
                "user": args.name,
                "key": plaintext,
                "prefix": key.display_prefix,
                "label": key.label,
                "budget_usd": key.budget_usd,
                "created_at": key.created_at,
            }
        )
        return 0
    print(f"virtual key for {args.name} (budget {_budget(key.budget_usd)}):")
    print()
    print(f"    {plaintext}")
    print()
    print("This is the only time the key is shown -- only its SHA-256 digest is")
    print(f"stored. Revoke it later with: keygate key revoke {key.display_prefix}")
    return 0


def cmd_user_budget(args: argparse.Namespace) -> int:
    workspace = _open(args)
    if args.amount.lower() in {"none", "unlimited"}:
        budget = None
    else:
        try:
            budget = float(args.amount.lstrip("$"))
        except ValueError:
            raise KeygateError(
                f"budget must be a number or 'none', got {args.amount!r}"
            ) from None
    user = workspace.store.set_user_budget(args.name, budget)
    print(f"{user.name} budget set to {_budget(user.budget_usd)}")
    return 0


def cmd_key_list(args: argparse.Namespace) -> int:
    workspace = _open(args)
    store = workspace.store
    keys = store.list_keys(args.user, include_revoked=args.all)
    users = {u.id: u.name for u in store.list_users()}
    if args.json:
        _dump_json(
            [
                {
                    "prefix": k.display_prefix,
                    "user": users.get(k.user_id),
                    "label": k.label,
                    "budget_usd": k.budget_usd,
                    "spent_usd": round(store.spent_for_key(k.id), 6),
                    "created_at": k.created_at,
                    "last_used_at": k.last_used_at,
                    "revoked_at": k.revoked_at,
                }
                for k in keys
            ]
        )
        return 0
    rows = [
        [
            k.display_prefix + "...",
            users.get(k.user_id, "?"),
            k.label or "-",
            _budget(k.budget_usd),
            _money(store.spent_for_key(k.id)),
            _short_ts(k.last_used_at),
            "revoked" if k.revoked else "live",
        ]
        for k in keys
    ]
    _print_table(
        ["KEY", "USER", "LABEL", "BUDGET", "SPENT", "LAST USED", "STATE"], rows
    )
    return 0


def cmd_key_revoke(args: argparse.Namespace) -> int:
    workspace = _open(args)
    key = workspace.store.revoke_key(args.prefix)
    print(f"revoked {key.display_prefix}... (effective immediately)")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    workspace = _open(args)
    server = build_server(workspace, args.host, args.port, quiet=args.quiet)
    host, port = server.server_address[0], server.server_address[1]

    if not workspace.upstream_api_key():
        env_name = workspace.config["upstream_api_key_env"]
        print(
            f"warning: no upstream API key found (${env_name} is unset and "
            f"config.json has none). Proxied requests will fail with 503.",
            file=sys.stderr,
        )
    if str(host) not in {"127.0.0.1", "::1", "localhost"}:
        print(
            f"warning: listening on {host} -- keygate speaks plain HTTP with no "
            "admin auth. Put it behind TLS and a trusted network.",
            file=sys.stderr,
        )

    print(f"keygate {__version__} listening on http://{host}:{port}")
    print(f"  workspace {workspace.path}")
    print(f"  upstream  {workspace.upstream_base_url}")
    print("  routes    POST /v1/chat/completions, GET /v1/models, GET /healthz")
    workspace.store.log_audit("cli", "serve.start", f"{host}:{port}", None)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        server.server_close()
        workspace.store.log_audit("cli", "serve.stop", f"{host}:{port}", None)
    return 0


def cmd_usage(args: argparse.Namespace) -> int:
    workspace = _open(args)
    store = workspace.store
    since = parse_since(args.since) if args.since else None

    if args.detail:
        rows = store.usage_rows(args.user, since, args.limit)
        if args.json:
            _dump_json(rows)
            return 0
        _print_table(
            ["WHEN", "USER", "KEY", "MODEL", "STATUS", "IN", "OUT", "COST", "NOTE"],
            [
                [
                    _short_ts(r["ts"]),
                    r["user"] or "-",
                    (r["key_prefix"] or "-"),
                    r["model"] or "-",
                    r["status"],
                    r["prompt_tokens"],
                    r["completion_tokens"],
                    _money(r["cost_usd"]),
                    r["note"] or "",
                ]
                for r in rows
            ],
        )
        return 0

    summary = store.usage_summary(since)
    if args.user:
        summary = [s for s in summary if s["user"] == args.user]
        if not summary:
            raise KeygateError(f"no such user: {args.user!r}")
    if args.json:
        _dump_json(summary)
        return 0

    _print_table(
        ["USER", "REQUESTS", "ERRORS", "IN", "OUT", "COST", "BUDGET", "REMAINING"],
        [
            [
                s["user"],
                s["requests"],
                s["errors"],
                s["prompt_tokens"],
                s["completion_tokens"],
                _money(s["cost_usd"]),
                _budget(s["budget_usd"]),
                "-"
                if s["budget_usd"] is None
                else _money(max(0.0, s["budget_usd"] - s["cost_usd"])),
            ]
            for s in summary
        ],
    )
    total = sum(s["cost_usd"] for s in summary)
    window = f" since {args.since}" if args.since else ""
    print(f"\ntotal{window}: {_money(total)}")
    if args.since:
        print("(BUDGET/REMAINING are lifetime figures, not windowed)")
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    workspace = _open(args)
    rows = workspace.store.audit_rows(args.limit)
    if args.json:
        _dump_json(rows)
        return 0
    _print_table(
        ["WHEN", "ACTOR", "ACTION", "TARGET", "DETAIL"],
        [
            [
                _short_ts(r["ts"]),
                r["actor"],
                r["action"],
                r["target"] or "-",
                r["detail"] or "",
            ]
            for r in rows
        ],
    )
    return 0


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="keygate",
        description="Local API-key gateway: one real upstream key, many virtual keys.",
    )
    parser.add_argument("--version", action="version", version=f"keygate {__version__}")
    parser.add_argument(
        "-d",
        "--dir",
        type=Path,
        default=default_workspace_path(),
        help="workspace directory (default: $KEYGATE_DIR or ./.keygate)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="create a workspace directory")
    p_init.add_argument("path", type=Path, help="directory to create")
    p_init.add_argument(
        "--upstream-base-url", help="OpenAI-compatible base URL, including /v1"
    )
    p_init.add_argument(
        "--force", action="store_true", help="overwrite an existing config.json"
    )
    p_init.set_defaults(func=cmd_init)

    p_user = sub.add_parser("user", help="manage users")
    user_sub = p_user.add_subparsers(dest="user_command", required=True)

    p_add = user_sub.add_parser("add", help="add a user")
    p_add.add_argument("name")
    p_add.add_argument("--email")
    p_add.add_argument("--budget", type=float, help="lifetime budget in USD")
    p_add.set_defaults(func=cmd_user_add)

    p_list = user_sub.add_parser("list", help="list users with spend")
    p_list.add_argument("--json", action="store_true")
    p_list.set_defaults(func=cmd_user_list)

    p_mint = user_sub.add_parser("mint", help="mint a virtual key for a user")
    p_mint.add_argument("name")
    p_mint.add_argument("--label", help="note to identify this key, e.g. 'laptop'")
    p_mint.add_argument("--budget", type=float, help="per-key budget in USD")
    p_mint.add_argument("--json", action="store_true")
    p_mint.set_defaults(func=cmd_user_mint)

    p_ubudget = user_sub.add_parser("budget", help="set a user's budget")
    p_ubudget.add_argument("name")
    p_ubudget.add_argument("amount", help="USD amount, or 'none' to remove the cap")
    p_ubudget.set_defaults(func=cmd_user_budget)

    p_key = sub.add_parser("key", help="manage virtual keys")
    key_sub = p_key.add_subparsers(dest="key_command", required=True)

    p_klist = key_sub.add_parser("list", help="list virtual keys")
    p_klist.add_argument("--user")
    p_klist.add_argument("--all", action="store_true", help="include revoked keys")
    p_klist.add_argument("--json", action="store_true")
    p_klist.set_defaults(func=cmd_key_list)

    p_krevoke = key_sub.add_parser("revoke", help="revoke a key by prefix")
    p_krevoke.add_argument("prefix", help="leading characters of the key, e.g. kg_v1_ab")
    p_krevoke.set_defaults(func=cmd_key_revoke)

    p_serve = sub.add_parser("serve", help="run the gateway")
    p_serve.add_argument("--host", help="override config listen_host")
    p_serve.add_argument("--port", type=int, help="override config listen_port")
    p_serve.add_argument("--quiet", action="store_true", help="suppress access log")
    p_serve.set_defaults(func=cmd_serve)

    p_usage = sub.add_parser("usage", help="show spend")
    p_usage.add_argument("--user")
    p_usage.add_argument("--since", help="7d, 12h, 30m, or an ISO timestamp")
    p_usage.add_argument(
        "--detail", action="store_true", help="list individual requests"
    )
    p_usage.add_argument("--limit", type=int, default=50)
    p_usage.add_argument("--json", action="store_true")
    p_usage.set_defaults(func=cmd_usage)

    p_audit = sub.add_parser("audit", help="show the admin audit log")
    p_audit.add_argument("--limit", type=int, default=50)
    p_audit.add_argument("--json", action="store_true")
    p_audit.set_defaults(func=cmd_audit)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeygateError as exc:
        print(f"keygate: error: {exc}", file=sys.stderr)
        return 1
    except BrokenPipeError:  # pragma: no cover - e.g. piping into `head`
        return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
