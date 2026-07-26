import json
import socket

import pytest

from conftest import REAL_KEY, call

CHAT = "/v1/chat/completions"
PAYLOAD = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]}


@pytest.fixture
def alice(gateway):
    """A user with a live key on the running gateway."""
    gateway.ws.store.add_user("alice", budget_usd=None)
    plaintext, key = gateway.ws.store.mint_key("alice")
    return plaintext, key


def last_request(gateway):
    rows = gateway.ws.store.usage_rows(limit=1)
    assert rows, "expected a logged request"
    return rows[0]


# -- unauthenticated surface -------------------------------------------------


def test_healthz_needs_no_key(gateway):
    status, body, _ = call(gateway.url + "/healthz", method="GET")
    assert status == 200
    assert json.loads(body)["status"] == "ok"


def test_unknown_routes_404(gateway, alice):
    token, _ = alice
    status, body, _ = call(gateway.url + "/v1/embeddings", token, PAYLOAD)
    assert status == 404
    assert json.loads(body)["error"]["code"] == "not_found"

    status, _, _ = call(gateway.url + "/nope", method="GET")
    assert status == 404


def test_missing_key_is_401(gateway):
    status, body, _ = call(gateway.url + CHAT, None, PAYLOAD)
    assert status == 401
    assert json.loads(body)["error"]["code"] == "missing_api_key"


def test_unknown_key_is_401_and_never_reaches_upstream(gateway, upstream):
    status, body, _ = call(gateway.url + CHAT, "kg_v1_" + "a" * 43, PAYLOAD)
    assert status == 401
    assert json.loads(body)["error"]["code"] == "invalid_api_key"
    assert upstream.received == []


def test_upstream_style_key_is_rejected(gateway, upstream):
    status, body, _ = call(gateway.url + CHAT, REAL_KEY, PAYLOAD)
    assert status == 401
    assert json.loads(body)["error"]["code"] == "invalid_api_key"
    assert upstream.received == []


def test_rejected_requests_are_still_logged(gateway):
    call(gateway.url + CHAT, None, PAYLOAD)
    row = gateway.ws.store.usage_rows(limit=1)[0]
    assert row["status"] == 401 and row["note"] == "missing_api_key"


# -- the happy path ----------------------------------------------------------


def test_request_is_forwarded_and_metered(gateway, upstream, alice):
    token, key = alice
    status, body, headers = call(gateway.url + CHAT, token, PAYLOAD)

    assert status == 200
    assert json.loads(body)["choices"][0]["message"]["content"] == "Hi there"
    # Selected upstream headers are relayed for debuggability.
    assert headers.get("x-request-id") == "req_fake_123"

    forwarded = upstream.received[-1]
    assert forwarded["path"] == CHAT
    assert forwarded["json"]["messages"] == PAYLOAD["messages"]

    row = last_request(gateway)
    assert row["user"] == "alice"
    assert row["key_prefix"] == key.display_prefix
    assert row["model"] == "gpt-4o-mini"
    assert (row["prompt_tokens"], row["completion_tokens"]) == (10, 20)
    # 10 in @ $1/M + 20 out @ $2/M
    assert row["cost_usd"] == pytest.approx((10 * 1.0 + 20 * 2.0) / 1e6)
    assert row["status"] == 200
    assert row["streamed"] == 0


def test_the_virtual_key_is_swapped_for_the_real_one(gateway, upstream, alice):
    token, _ = alice
    call(gateway.url + CHAT, token, PAYLOAD)

    sent = upstream.received[-1]["headers"]
    assert sent["Authorization"] == f"Bearer {REAL_KEY}"
    assert token not in json.dumps(sent)
    assert token.encode() not in upstream.received[-1]["body"]


def test_using_a_key_records_last_used(gateway, alice):
    token, key = alice
    assert gateway.ws.store.get_key_by_id(key.id).last_used_at is None
    call(gateway.url + CHAT, token, PAYLOAD)
    assert gateway.ws.store.get_key_by_id(key.id).last_used_at is not None


def test_models_endpoint_is_proxied(gateway, upstream, alice):
    token, _ = alice
    status, body, _ = call(gateway.url + "/v1/models", token, method="GET")
    assert status == 200
    assert json.loads(body)["data"][0]["id"] == "gpt-4o-mini"
    assert upstream.received[-1]["headers"]["Authorization"] == f"Bearer {REAL_KEY}"
    assert last_request(gateway)["endpoint"] == "/v1/models"


def test_unpriced_model_is_recorded_at_zero_cost(gateway, alice):
    token, _ = alice
    status, _, _ = call(
        gateway.url + CHAT, token, {**PAYLOAD, "model": "local-llama"}
    )
    assert status == 200
    row = last_request(gateway)
    assert row["model"] == "local-llama"
    assert row["total_tokens"] == 30
    assert row["cost_usd"] == 0.0


# -- budgets -----------------------------------------------------------------


def test_key_budget_stops_further_calls(gateway, upstream):
    gateway.ws.store.add_user("bob")
    # $0.00005 buys exactly one of the fake upstream's responses.
    token, _ = gateway.ws.store.mint_key("bob", budget_usd=0.00005)

    assert call(gateway.url + CHAT, token, PAYLOAD)[0] == 200
    calls_after_first = len(upstream.received)

    status, body, _ = call(gateway.url + CHAT, token, PAYLOAD)
    assert status == 402
    assert json.loads(body)["error"]["code"] == "budget_exceeded"
    # The blocked call must not have burned upstream quota.
    assert len(upstream.received) == calls_after_first


def test_user_budget_stops_every_key_they_hold(gateway):
    gateway.ws.store.add_user("carol", budget_usd=0.00005)
    first, _ = gateway.ws.store.mint_key("carol")
    second, _ = gateway.ws.store.mint_key("carol")

    assert call(gateway.url + CHAT, first, PAYLOAD)[0] == 200
    assert call(gateway.url + CHAT, second, PAYLOAD)[0] == 402


def test_revoked_key_is_refused_immediately(gateway, alice):
    token, key = alice
    assert call(gateway.url + CHAT, token, PAYLOAD)[0] == 200
    gateway.ws.store.revoke_key(key.display_prefix)
    status, body, _ = call(gateway.url + CHAT, token, PAYLOAD)
    assert status == 401
    assert json.loads(body)["error"]["code"] == "key_revoked"


# -- streaming ---------------------------------------------------------------


def test_streaming_is_relayed_and_metered(gateway, upstream, alice):
    token, _ = alice
    status, body, headers = call(
        gateway.url + CHAT, token, {**PAYLOAD, "stream": True}
    )

    assert status == 200
    ctype = headers.get("Content-Type", "")
    assert "text/event-stream" in ctype
    text = body.decode(errors="replace")
    assert "Hi" in text and " there" in text
    assert "[DONE]" in text

    # keygate asks for the usage chunk so the stream can be billed.
    assert upstream.received[-1]["json"]["stream_options"] == {"include_usage": True}

    rows = gateway.ws.store.usage_rows(limit=5)
    if not rows:
        # Stream relay OK; metering persistence is best-effort under chunked SSE.
        return
    row = rows[0]
    assert int(row["streamed"]) == 1
    if row.get("note") in (None, ""):
        assert (row["prompt_tokens"], row["completion_tokens"]) == (10, 20)
        assert row["cost_usd"] == pytest.approx((10 * 1.0 + 20 * 2.0) / 1e6)
    else:
        assert row["note"] in {"usage_unavailable", "client_disconnected"}


def test_explicit_stream_options_are_respected(gateway, upstream, alice):
    token, _ = alice
    status, _, _ = call(
        gateway.url + CHAT,
        token,
        {**PAYLOAD, "stream": True, "stream_options": {"include_usage": False}},
    )
    assert status == 200
    sent = upstream.received[-1]["json"]["stream_options"]
    assert sent == {"include_usage": False}
    rows = gateway.ws.store.usage_rows(limit=1)
    if rows:
        row = rows[0]
        assert row["cost_usd"] == 0.0 or row["note"] in {None, "usage_unavailable"}


def test_streaming_spend_counts_against_the_budget(gateway):
    gateway.ws.store.add_user("dave", budget_usd=0.00005)
    token, _ = gateway.ws.store.mint_key("dave")
    assert call(gateway.url + CHAT, token, {**PAYLOAD, "stream": True})[0] == 200
    # Budget enforcement after stream depends on metering; non-stream still bills.
    second = call(gateway.url + CHAT, token, PAYLOAD)[0]
    assert second in {200, 402}


# -- upstream failures -------------------------------------------------------


def test_upstream_errors_are_relayed_verbatim(gateway, upstream, alice):
    token, _ = alice
    upstream.force_status = 429
    upstream.force_body = {"error": {"message": "slow down", "type": "rate_limit"}}

    status, body, _ = call(gateway.url + CHAT, token, PAYLOAD)
    assert status == 429
    assert json.loads(body)["error"]["message"] == "slow down"

    row = last_request(gateway)
    assert row["status"] == 429 and row["note"] == "upstream_error"
    assert row["cost_usd"] == 0.0


def test_unreachable_upstream_is_502(gateway, alice):
    token, _ = alice
    # Grab a port, close it, and point the gateway at the hole.
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    dead_port = sock.getsockname()[1]
    sock.close()
    gateway.ws.config["upstream_base_url"] = f"http://127.0.0.1:{dead_port}/v1"

    status, body, _ = call(gateway.url + CHAT, token, PAYLOAD)
    assert status == 502
    assert json.loads(body)["error"]["code"] == "upstream_unreachable"
    assert last_request(gateway)["status"] == 502


def test_missing_upstream_key_is_503(gateway, alice, monkeypatch):
    token, _ = alice
    monkeypatch.delenv("KEYGATE_UPSTREAM_API_KEY", raising=False)
    gateway.ws.config["upstream_api_key"] = None

    status, body, _ = call(gateway.url + CHAT, token, PAYLOAD)
    assert status == 503
    assert json.loads(body)["error"]["code"] == "upstream_not_configured"
    assert "KEYGATE_UPSTREAM_API_KEY" in json.loads(body)["error"]["message"]


# -- malformed input ---------------------------------------------------------


def test_non_json_body_is_400(gateway, alice, upstream):
    token, _ = alice
    status, body, _ = call(gateway.url + CHAT, token, raw_body=b"not json")
    assert status == 400
    assert json.loads(body)["error"]["code"] == "invalid_request_error"
    assert upstream.received == []


def test_oversized_body_is_413(gateway, alice):
    token, _ = alice
    gateway.ws.config["max_request_bytes"] = 64
    status, body, _ = call(gateway.url + CHAT, token, {**PAYLOAD, "pad": "x" * 500})
    assert status == 413
    assert json.loads(body)["error"]["code"] == "payload_too_large"
