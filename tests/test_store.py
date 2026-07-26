import json
import os
import stat
from datetime import datetime, timedelta, timezone

import pytest

from keygate import keys
from keygate.store import (
    CONFIG_NAME,
    DB_NAME,
    SCHEMA_VERSION,
    KeygateError,
    Workspace,
    parse_since,
)


@pytest.fixture
def ws(tmp_path) -> Workspace:
    return Workspace.create(tmp_path / "ws")


# -- workspace ---------------------------------------------------------------


def test_create_lays_out_the_workspace(tmp_path):
    ws = Workspace.create(tmp_path / "ws")
    assert (ws.path / CONFIG_NAME).is_file()
    assert (ws.path / DB_NAME).is_file()
    assert ws.store.schema_version() == SCHEMA_VERSION


def test_config_is_not_world_readable(tmp_path):
    ws = Workspace.create(tmp_path / "ws")
    mode = stat.S_IMODE(os.stat(ws.path / CONFIG_NAME).st_mode)
    assert mode & 0o077 == 0, "config.json may hold the upstream key"


def test_create_refuses_to_clobber_without_force(tmp_path):
    Workspace.create(tmp_path / "ws")
    with pytest.raises(KeygateError, match="already exists"):
        Workspace.create(tmp_path / "ws")
    Workspace.create(tmp_path / "ws", force=True)  # allowed with --force


def test_open_requires_an_initialized_workspace(tmp_path):
    with pytest.raises(KeygateError, match="no keygate workspace"):
        Workspace.open(tmp_path / "nope")


def test_open_reports_broken_config(tmp_path):
    ws = Workspace.create(tmp_path / "ws")
    (ws.path / CONFIG_NAME).write_text("{not json")
    with pytest.raises(KeygateError, match="not valid JSON"):
        Workspace.open(ws.path)


def test_open_backfills_missing_config_keys(tmp_path):
    ws = Workspace.create(tmp_path / "ws")
    (ws.path / CONFIG_NAME).write_text(json.dumps({"listen_port": 9999}))
    reopened = Workspace.open(ws.path)
    assert reopened.config["listen_port"] == 9999
    assert reopened.config["upstream_base_url"]  # default survived


def test_upstream_key_prefers_environment(tmp_path, monkeypatch):
    ws = Workspace.create(tmp_path / "ws")
    ws.config["upstream_api_key"] = "from-config"
    assert ws.upstream_api_key() == "from-config"
    monkeypatch.setenv("KEYGATE_UPSTREAM_API_KEY", "from-env")
    assert ws.upstream_api_key() == "from-env"


def test_upstream_key_absent(tmp_path, monkeypatch):
    monkeypatch.delenv("KEYGATE_UPSTREAM_API_KEY", raising=False)
    assert Workspace.create(tmp_path / "ws").upstream_api_key() is None


# -- pricing -----------------------------------------------------------------


def test_price_lookup_falls_back_to_longest_prefix(ws):
    ws.config["pricing"] = {
        "default": {"input": 0.5, "output": 0.5},
        "gpt-4o": {"input": 2.5, "output": 10.0},
        "gpt-4o-mini": {"input": 0.15, "output": 0.6},
    }
    assert ws.price_for("gpt-4o") == (2.5, 10.0)
    assert ws.price_for("gpt-4o-2024-11-20") == (2.5, 10.0)
    assert ws.price_for("gpt-4o-mini-2024-07-18") == (0.15, 0.6)
    assert ws.price_for("some-local-model") == (0.5, 0.5)
    assert ws.price_for(None) == (0.5, 0.5)


def test_cost_is_per_million_tokens(ws):
    ws.config["pricing"] = {
        "default": {"input": 0.0, "output": 0.0},
        "m": {"input": 3.0, "output": 6.0},
    }
    assert ws.cost_usd("m", 1_000_000, 0) == pytest.approx(3.0)
    assert ws.cost_usd("m", 0, 500_000) == pytest.approx(3.0)
    assert ws.cost_usd("unknown", 1_000_000, 1_000_000) == 0.0


# -- users and keys ----------------------------------------------------------


def test_add_and_list_users(ws):
    ws.store.add_user("alice", "alice@example.com", 10.0)
    ws.store.add_user("bob")
    assert [u.name for u in ws.store.list_users()] == ["alice", "bob"]
    alice = ws.store.require_user("alice")
    assert alice.email == "alice@example.com"
    assert alice.budget_usd == 10.0
    assert ws.store.require_user("bob").budget_usd is None


def test_duplicate_and_empty_users_are_rejected(ws):
    ws.store.add_user("alice")
    with pytest.raises(KeygateError, match="already exists"):
        ws.store.add_user("alice")
    with pytest.raises(KeygateError, match="may not be empty"):
        ws.store.add_user("   ")
    with pytest.raises(KeygateError, match="negative"):
        ws.store.add_user("carol", budget_usd=-1)


def test_require_user_rejects_unknown(ws):
    with pytest.raises(KeygateError, match="no such user"):
        ws.store.require_user("ghost")


def test_mint_stores_only_the_digest(ws):
    ws.store.add_user("alice")
    plaintext, key = ws.store.mint_key("alice", label="laptop", budget_usd=5.0)

    raw = (ws.path / DB_NAME).read_bytes()
    # Plaintext must never hit disk; digest is looked up via the store API
    # (SQLite pages may not contain a greppable ASCII hex substring).
    assert plaintext.encode() not in raw
    auth = ws.store.authenticate(plaintext)
    assert auth.ok
    assert auth.key.key_hash == keys.hash_key(plaintext)
    assert key.display_prefix == plaintext[:14]
    assert key.label == "laptop"


def test_mint_requires_a_known_user(ws):
    with pytest.raises(KeygateError, match="no such user"):
        ws.store.mint_key("ghost")


def test_list_keys_and_revocation(ws):
    ws.store.add_user("alice")
    _, first = ws.store.mint_key("alice", label="one")
    ws.store.mint_key("alice", label="two")
    assert len(ws.store.list_keys("alice")) == 2

    revoked = ws.store.revoke_key(first.display_prefix)
    assert revoked.revoked
    assert len(ws.store.list_keys("alice")) == 1
    assert len(ws.store.list_keys("alice", include_revoked=True)) == 2

    with pytest.raises(KeygateError, match="no live key"):
        ws.store.revoke_key(first.display_prefix)


def test_revoke_refuses_ambiguous_prefix(ws):
    ws.store.add_user("alice")
    ws.store.mint_key("alice")
    ws.store.mint_key("alice")
    with pytest.raises(KeygateError, match="be more specific"):
        ws.store.revoke_key("kg_v1_")


# -- authentication ----------------------------------------------------------


def test_authenticate_happy_path(ws):
    ws.store.add_user("alice")
    plaintext, key = ws.store.mint_key("alice")
    result = ws.store.authenticate(plaintext)
    assert result.ok
    assert result.user is not None and result.user.name == "alice"
    assert result.key is not None and result.key.id == key.id


@pytest.mark.parametrize(
    "presented, code",
    [
        (None, "missing_api_key"),
        ("", "missing_api_key"),
        ("sk-not-a-virtual-key", "invalid_api_key"),
        ("kg_v1_" + "a" * 43, "invalid_api_key"),
    ],
)
def test_authenticate_rejects_bad_keys(ws, presented, code):
    result = ws.store.authenticate(presented)
    assert not result.ok
    assert result.code == code
    assert result.status == 401


def test_authenticate_rejects_revoked_key(ws):
    ws.store.add_user("alice")
    plaintext, key = ws.store.mint_key("alice")
    ws.store.revoke_key(key.display_prefix)
    result = ws.store.authenticate(plaintext)
    assert not result.ok and result.code == "key_revoked"


def test_authenticate_rejects_disabled_user(ws):
    ws.store.add_user("alice")
    plaintext, _ = ws.store.mint_key("alice")
    with ws.store.conn:
        ws.store.conn.execute("UPDATE users SET disabled_at = '2026-01-01' ")
    result = ws.store.authenticate(plaintext)
    assert not result.ok and result.code == "user_disabled" and result.status == 403


def test_key_budget_blocks_once_spent(ws):
    ws.store.add_user("alice")
    plaintext, key = ws.store.mint_key("alice", budget_usd=1.0)
    ws.store.record_request(
        endpoint="/v1/chat/completions",
        status=200,
        key_id=key.id,
        user_id=key.user_id,
        cost_usd=0.4,
    )
    assert ws.store.authenticate(plaintext).ok

    ws.store.record_request(
        endpoint="/v1/chat/completions",
        status=200,
        key_id=key.id,
        user_id=key.user_id,
        cost_usd=0.7,
    )
    result = ws.store.authenticate(plaintext)
    assert not result.ok
    assert result.code == "budget_exceeded" and result.status == 402
    assert "1.1" in result.message


def test_user_budget_spans_all_their_keys(ws):
    user = ws.store.add_user("alice", budget_usd=1.0)
    first_plain, first = ws.store.mint_key("alice")
    second_plain, _ = ws.store.mint_key("alice")
    ws.store.record_request(
        endpoint="/v1/chat/completions",
        status=200,
        key_id=first.id,
        user_id=user.id,
        cost_usd=1.5,
    )
    # Spend on one key exhausts the user budget for every key they hold.
    assert not ws.store.authenticate(first_plain).ok
    assert ws.store.authenticate(second_plain).code == "budget_exceeded"


def test_budgets_are_independent_between_users(ws):
    ws.store.add_user("alice", budget_usd=1.0)
    ws.store.add_user("bob", budget_usd=1.0)
    alice_plain, alice_key = ws.store.mint_key("alice")
    bob_plain, _ = ws.store.mint_key("bob")
    ws.store.record_request(
        endpoint="/v1/chat/completions",
        status=200,
        key_id=alice_key.id,
        user_id=alice_key.user_id,
        cost_usd=2.0,
    )
    assert not ws.store.authenticate(alice_plain).ok
    assert ws.store.authenticate(bob_plain).ok


def test_set_user_budget(ws):
    ws.store.add_user("alice", budget_usd=1.0)
    assert ws.store.set_user_budget("alice", 5.0).budget_usd == 5.0
    assert ws.store.set_user_budget("alice", None).budget_usd is None


def test_touch_key_records_last_use(ws):
    ws.store.add_user("alice")
    _, key = ws.store.mint_key("alice")
    assert key.last_used_at is None
    ws.store.touch_key(key.id)
    assert ws.store.get_key_by_id(key.id).last_used_at is not None


# -- usage and audit ---------------------------------------------------------


def test_usage_summary_rolls_up_per_user(ws):
    ws.store.add_user("alice", budget_usd=10.0)
    ws.store.add_user("bob")
    _, alice_key = ws.store.mint_key("alice")
    for cost, status in [(0.25, 200), (0.75, 200), (0.0, 429)]:
        ws.store.record_request(
            endpoint="/v1/chat/completions",
            status=status,
            key_id=alice_key.id,
            user_id=alice_key.user_id,
            model="gpt-4o-mini",
            prompt_tokens=10,
            completion_tokens=20,
            total_tokens=30,
            cost_usd=cost,
        )

    summary = {row["user"]: row for row in ws.store.usage_summary()}
    assert summary["alice"]["requests"] == 3
    assert summary["alice"]["errors"] == 1
    assert summary["alice"]["cost_usd"] == pytest.approx(1.0)
    assert summary["alice"]["total_tokens"] == 90
    assert summary["alice"]["budget_usd"] == 10.0
    # Users with no traffic still appear, at zero.
    assert summary["bob"]["requests"] == 0
    assert summary["bob"]["cost_usd"] == 0


def test_usage_rows_filter_by_user_and_window(ws):
    ws.store.add_user("alice")
    ws.store.add_user("bob")
    _, alice_key = ws.store.mint_key("alice")
    _, bob_key = ws.store.mint_key("bob")
    for key in (alice_key, bob_key):
        ws.store.record_request(
            endpoint="/v1/chat/completions",
            status=200,
            key_id=key.id,
            user_id=key.user_id,
            cost_usd=0.1,
        )

    assert len(ws.store.usage_rows()) == 2
    alice_rows = ws.store.usage_rows("alice")
    assert len(alice_rows) == 1
    assert alice_rows[0]["key_prefix"] == alice_key.display_prefix

    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    assert ws.store.usage_rows(since=parse_since(future)) == []
    assert len(ws.store.usage_rows(since=parse_since("1h"))) == 2
    assert len(ws.store.usage_rows(limit=1)) == 1


def test_admin_actions_are_audited(ws):
    ws.store.add_user("alice")
    _, key = ws.store.mint_key("alice")
    ws.store.revoke_key(key.display_prefix)

    actions = [row["action"] for row in ws.store.audit_rows()]
    assert actions[:3] == ["key.revoke", "key.mint", "user.add"]
    assert "workspace.init" in actions

    mint_row = next(r for r in ws.store.audit_rows() if r["action"] == "key.mint")
    assert mint_row["target"] == key.display_prefix
    assert json.loads(mint_row["detail"])["user"] == "alice"


def test_audit_never_contains_a_plaintext_key(ws):
    ws.store.add_user("alice")
    plaintext, _ = ws.store.mint_key("alice")
    blob = json.dumps(ws.store.audit_rows())
    assert plaintext not in blob


# -- helpers -----------------------------------------------------------------


def test_parse_since_relative_and_absolute():
    now = datetime.now(timezone.utc)
    hour_ago = datetime.fromisoformat(parse_since("1h").replace("Z", "+00:00"))
    assert timedelta(minutes=55) < now - hour_ago < timedelta(minutes=65)

    assert parse_since("2026-01-02").startswith("2026-01-02T00:00:00")
    assert parse_since("2026-01-02T03:04:05Z").startswith("2026-01-02T03:04:05")
    assert parse_since("30m") < parse_since("1m")
    assert parse_since("7d") < parse_since("1d")


def test_parse_since_rejects_nonsense():
    with pytest.raises(KeygateError, match="could not read"):
        parse_since("last tuesday")
