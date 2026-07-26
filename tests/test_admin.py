"""The admin JSON API and the packaged dashboard, over a real socket.

These run against the same ``build_server`` the gateway uses, so they also
prove the admin routes did not displace the proxy ones.
"""

import json

import pytest

from conftest import call

API = "/admin/api"


def get(gateway, path):
    status, body, headers = call(gateway.url + path, method="GET")
    return status, body, headers


def json_call(gateway, path, method="GET", payload=None):
    status, body, _ = call(gateway.url + path, payload=payload, method=method)
    return status, json.loads(body) if body else None


@pytest.fixture
def seeded(gateway):
    """A workspace with one budgeted user and one live key."""
    gateway.ws.store.add_user("alice", email="alice@example.com", budget_usd=10.0)
    plaintext, key = gateway.ws.store.mint_key("alice", label="laptop")
    gateway.plaintext = plaintext
    gateway.key = key
    return gateway


# -- overview ----------------------------------------------------------------


def test_overview_reports_the_workspace(seeded):
    status, data = json_call(seeded, API + "/overview")
    assert status == 200
    assert data["users"] == 1
    assert data["live_keys"] == 1
    assert data["total_spent_usd"] == 0.0
    assert data["workspace"] == str(seeded.ws.path)
    assert data["upstream_base_url"] == seeded.ws.upstream_base_url
    assert data["requests_24h"] == 0 and data["errors_24h"] == 0
    assert data["recent"] == []


def test_overview_counts_proxied_traffic(seeded):
    call(seeded.url + "/v1/chat/completions", seeded.plaintext, {"model": "gpt-4o-mini",
         "messages": [{"role": "user", "content": "hi"}]})
    _, data = json_call(seeded, API + "/overview")
    assert data["requests_24h"] == 1
    assert data["errors_24h"] == 0
    assert data["total_spent_usd"] > 0
    assert data["recent"][0]["user"] == "alice"


def test_overview_counts_refusals_as_errors(gateway):
    call(gateway.url + "/v1/chat/completions", None, {"model": "gpt-4o-mini"})
    _, data = json_call(gateway, API + "/overview")
    assert data["errors_24h"] == 0  # the refusal has no user to roll up under
    assert data["requests_total"] == 0


# -- users -------------------------------------------------------------------


def test_users_list_carries_spend_and_budget(seeded):
    status, data = json_call(seeded, API + "/users")
    assert status == 200
    (alice,) = data["users"]
    assert alice["name"] == "alice"
    assert alice["email"] == "alice@example.com"
    assert alice["budget_usd"] == 10.0
    assert alice["spent_usd"] == 0.0
    assert alice["remaining_usd"] == 10.0
    assert alice["live_keys"] == 1
    assert alice["disabled"] is False


def test_create_user_and_read_it_back(gateway):
    status, data = json_call(
        gateway, API + "/users", "POST", {"name": "bob", "budget_usd": 5}
    )
    assert status == 201
    assert data["user"]["name"] == "bob"
    assert data["user"]["budget_usd"] == 5.0

    _, listing = json_call(gateway, API + "/users")
    assert [u["name"] for u in listing["users"]] == ["bob"]


def test_create_user_without_a_budget_is_unlimited(gateway):
    _, data = json_call(gateway, API + "/users", "POST", {"name": "carol"})
    assert data["user"]["budget_usd"] is None
    assert data["user"]["remaining_usd"] is None


def test_duplicate_user_is_a_clean_400(seeded):
    status, data = json_call(seeded, API + "/users", "POST", {"name": "alice"})
    assert status == 400
    assert "already exists" in data["error"]["message"]


def test_user_needs_a_name(gateway):
    status, data = json_call(gateway, API + "/users", "POST", {"budget_usd": 1})
    assert status == 400
    assert data["error"]["code"] == "missing_field"


def test_negative_budget_is_refused(gateway):
    status, data = json_call(
        gateway, API + "/users", "POST", {"name": "dave", "budget_usd": -1}
    )
    assert status == 400
    assert "negative" in data["error"]["message"]


def test_patch_sets_and_clears_the_budget(seeded):
    status, data = json_call(
        seeded, API + "/users/alice", "PATCH", {"budget_usd": 42.5}
    )
    assert status == 200
    assert data["user"]["budget_usd"] == 42.5

    _, cleared = json_call(seeded, API + "/users/alice", "PATCH", {"budget_usd": None})
    assert cleared["user"]["budget_usd"] is None
    assert seeded.ws.store.get_user("alice").budget_usd is None


def test_patch_unknown_user_is_400(gateway):
    status, data = json_call(gateway, API + "/users/nobody", "PATCH", {"budget_usd": 1})
    assert status == 400
    assert "no such user" in data["error"]["message"]


# -- keys --------------------------------------------------------------------


def test_mint_returns_the_plaintext_exactly_once(gateway):
    gateway.ws.store.add_user("erin")
    status, minted = json_call(
        gateway, API + "/keys", "POST", {"user": "erin", "label": "ci", "budget_usd": 2}
    )
    assert status == 201
    assert minted["key"].startswith("kg_v1_")
    assert minted["prefix"] == minted["key"][: len(minted["prefix"])]
    assert minted["label"] == "ci" and minted["budget_usd"] == 2.0

    # The plaintext must never reappear on any listing.
    _, listing = json_call(gateway, API + "/keys")
    assert minted["key"] not in json.dumps(listing)
    (key,) = listing["keys"]
    assert key["prefix"] == minted["prefix"]
    assert key["user"] == "erin"
    assert key["revoked"] is False
    assert "key" not in key and "key_hash" not in key


def test_a_minted_key_actually_works_against_the_proxy(gateway):
    gateway.ws.store.add_user("frank")
    _, minted = json_call(gateway, API + "/keys", "POST", {"user": "frank"})
    status, _, _ = call(
        gateway.url + "/v1/chat/completions",
        minted["key"],
        {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert status == 200


def test_mint_for_an_unknown_user_is_400(gateway):
    status, data = json_call(gateway, API + "/keys", "POST", {"user": "ghost"})
    assert status == 400
    assert "no such user" in data["error"]["message"]


def test_keys_can_be_filtered_by_user(seeded):
    seeded.ws.store.add_user("bob")
    seeded.ws.store.mint_key("bob")

    _, everyone = json_call(seeded, API + "/keys")
    assert len(everyone["keys"]) == 2

    _, just_bob = json_call(seeded, API + "/keys?user=bob")
    assert [k["user"] for k in just_bob["keys"]] == ["bob"]


def test_revoke_hides_the_key_and_blocks_the_proxy(seeded):
    prefix = seeded.key.display_prefix
    status, data = json_call(seeded, API + f"/keys/{prefix}/revoke", "POST")
    assert status == 200
    assert data["key"]["revoked"] is True

    _, live = json_call(seeded, API + "/keys")
    assert live["keys"] == []

    # Revoked keys are still visible with all=1, and refused at the gateway.
    _, everything = json_call(seeded, API + "/keys?all=1")
    assert [k["prefix"] for k in everything["keys"]] == [prefix]

    status, body, _ = call(
        seeded.url + "/v1/chat/completions",
        seeded.plaintext,
        {"model": "gpt-4o-mini", "messages": []},
    )
    assert status == 401
    assert json.loads(body)["error"]["code"] == "key_revoked"


def test_revoking_twice_is_a_clean_400(seeded):
    prefix = seeded.key.display_prefix
    assert json_call(seeded, API + f"/keys/{prefix}/revoke", "POST")[0] == 200
    status, data = json_call(seeded, API + f"/keys/{prefix}/revoke", "POST")
    assert status == 400
    assert "no live key" in data["error"]["message"]


# -- usage and audit ---------------------------------------------------------


def test_usage_summary_and_detail(seeded):
    call(seeded.url + "/v1/chat/completions", seeded.plaintext,
         {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]})

    status, summary = json_call(seeded, API + "/usage")
    assert status == 200
    assert summary["detail"] is False
    assert summary["summary"][0]["user"] == "alice"
    assert summary["summary"][0]["requests"] == 1
    assert summary["total_usd"] == pytest.approx((10 * 1.0 + 20 * 2.0) / 1e6)

    _, detail = json_call(seeded, API + "/usage?detail=1&limit=5")
    assert detail["detail"] is True
    assert detail["rows"][0]["model"] == "gpt-4o-mini"
    assert detail["rows"][0]["key_prefix"] == seeded.key.display_prefix


def test_usage_since_window_is_parsed(seeded):
    assert json_call(seeded, API + "/usage?since=7d")[0] == 200
    status, data = json_call(seeded, API + "/usage?since=nonsense")
    assert status == 400
    assert "--since" in data["error"]["message"]


def test_dashboard_mutations_are_audited_as_dashboard(gateway):
    json_call(gateway, API + "/users", "POST", {"name": "gina"})
    json_call(gateway, API + "/keys", "POST", {"user": "gina"})

    status, data = json_call(gateway, API + "/audit?limit=10")
    assert status == 200
    logged = {(r["actor"], r["action"]) for r in data["rows"]}
    assert ("dashboard", "user.add") in logged
    assert ("dashboard", "key.mint") in logged
    # The CLI path keeps its own actor.
    gateway.ws.store.add_user("hank")
    _, after = json_call(gateway, API + "/audit?limit=10")
    assert ("cli", "user.add") in {(r["actor"], r["action"]) for r in after["rows"]}


# -- error shapes and method handling ----------------------------------------


def test_unknown_admin_route_is_404_json(gateway):
    status, data = json_call(gateway, API + "/nope")
    assert status == 404
    assert data["error"]["code"] == "not_found"


def test_wrong_method_is_405(gateway):
    status, data = json_call(gateway, API + "/overview", "POST", {})
    assert status == 405
    assert data["error"]["code"] == "method_not_allowed"


def test_malformed_json_body_is_400(gateway):
    status, body, _ = call(
        gateway.url + API + "/users", method="POST", raw_body=b"{not json"
    )
    assert status == 400
    assert json.loads(body)["error"]["code"] == "invalid_json"


# -- static dashboard --------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/dash", "/dash/", "/dash/index.html"])
def test_dashboard_html_is_served(gateway, path):
    status, body, headers = get(gateway, path)
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert b"<title>keygate</title>" in body
    assert b"/dash/app.js" in body


def test_dashboard_assets_have_the_right_content_type(gateway):
    status, body, headers = get(gateway, "/dash/styles.css")
    assert status == 200
    assert headers["Content-Type"].startswith("text/css")
    assert b"--accent: #7dd3fc" in body

    status, body, headers = get(gateway, "/dash/app.js")
    assert status == 200
    assert "javascript" in headers["Content-Type"]
    assert b"/admin/api" in body


def test_unknown_dashboard_asset_is_404(gateway):
    status, body, _ = get(gateway, "/dash/nope.js")
    assert status == 404
    assert json.loads(body)["error"]["code"] == "not_found"


@pytest.mark.parametrize("name", ["../store.py", "..%2Fstore.py", "sub/dir.js", ".env"])
def test_dashboard_refuses_to_escape_its_directory(gateway, name):
    status, _, _ = get(gateway, "/dash/" + name)
    assert status == 404


def test_the_proxy_routes_still_win(gateway):
    """The dashboard must not have shadowed anything the gateway owns."""
    assert get(gateway, "/healthz")[0] == 200
    status, body, _ = get(gateway, "/v1/models")
    assert status == 401  # still requires a virtual key
    assert json.loads(body)["error"]["code"] == "missing_api_key"
