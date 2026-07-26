# keygate

**Share one real LLM API key with your team — mint per-user virtual keys, budgets, an audit log, and a local web dashboard.**

Local-first, **stdlib-only** Python. No FastAPI, no React, no npm, no build step: `pip install keygate` and you have a metered OpenAI-compatible gateway *and* its control room. You keep the provider key. Teammates get `kg_v1_...` virtual keys. Every request is attributed.

> Not LiteLLM Enterprise. A tiny self-hosted control plane for freelancers / small teams who need **identity + budgets** in front of OpenAI-compatible APIs.

## Quickstart

```bash
pip install -e .
export KEYGATE_UPSTREAM_API_KEY='sk-...'   # your one real provider key
export KEYGATE_DIR=./kg-data

keygate init ./kg-data
keygate serve
```

```
keygate 0.2.0 listening on http://127.0.0.1:8787
  workspace  kg-data
  upstream   https://api.openai.com/v1
  dashboard  http://127.0.0.1:8787/dash
  admin api  http://127.0.0.1:8787/admin/api/...  (unauthenticated)
  proxy      POST /v1/chat/completions, GET /v1/models, GET /healthz
```

Open **<http://127.0.0.1:8787/dash>**. One process serves the proxy and the
dashboard on the same port; `keygate dash` is an alias for `keygate serve`.

## The 60-second demo

1. **Add a user.** In the dashboard: *Users → Add user* (`alice`, budget `$25`).
   From the CLI: `keygate user add alice --budget 25`.
2. **Mint a key.** *Mint key* on alice's row. The plaintext `kg_v1_…` appears in
   a modal **once** — copy it. keygate only ever stored its SHA-256 digest.
   From the CLI: `keygate user mint alice`.
3. **Spend it.** Point any OpenAI client at the gateway:

   ```bash
   curl http://127.0.0.1:8787/v1/chat/completions \
     -H "Authorization: Bearer kg_v1_..." \
     -H "Content-Type: application/json" \
     -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hi"}]}'
   ```

   ```python
   from openai import OpenAI
   client = OpenAI(base_url="http://127.0.0.1:8787/v1", api_key="kg_v1_...")
   ```
4. **Watch it land.** The Overview tile ticks up, alice's budget meter fills,
   and the request shows in *Usage* with tokens, cost and latency. Or:
   `keygate usage --detail`.
5. **Cut it off.** *Revoke* on the key, or `keygate key revoke kg_v1_ab`. The
   next request is refused with `401 key_revoked`. Blow the budget and it is
   `402 budget_exceeded` — before the request ever reaches your provider.

## The dashboard

`/dash` is a single vanilla-JS page packaged inside the wheel — no CDN, no
bundler, no dependency on the internet except the webfont link.

| Tab | What it gives you |
|-----|-------------------|
| Overview | Total spend, users, live keys, 24h requests/errors, budget meters, recent requests |
| Users | Add users, set/clear budgets, spend vs. cap with a danger state near the limit, key counts |
| Keys | Mint (plaintext shown once), filter by user, show revoked, revoke |
| Usage | Per-user rollup over a window, or the raw request log with status, tokens and cost |
| Audit | Every mutation, tagged `cli` or `dashboard` |

`Esc` closes any modal; `1`–`5` switch tabs; `r` refreshes. The page
auto-refreshes every 8s — click the *live* pip to pause.

## ⚠️ The dashboard and admin API are unauthenticated

There is no login. **Anyone who can open a TCP connection to the port can mint
and revoke keys, and read your spend.** keygate binds `127.0.0.1` by default for
exactly this reason, and `keygate serve` warns loudly if you move it.

If you need it on a network: put it behind a reverse proxy that terminates TLS
and does the authentication, and restrict it to a trusted network. Do not
expose port 8787 to the internet.

## Commands

| Command | Purpose |
|---------|---------|
| `keygate init <dir>` | Create workspace (sqlite + config) |
| `keygate user add/list` | Manage users + budgets |
| `keygate user mint <name>` | Issue virtual key (shown once) |
| `keygate user budget <name> <amt>` | Set or clear a cap (`none` = unlimited) |
| `keygate key list/revoke` | Inspect and kill keys |
| `keygate serve` / `keygate dash` | Run the proxy **and** the dashboard |
| `keygate usage [--detail]` | Who spent what |
| `keygate audit` | Admin action log |

Global: `-d/--dir` picks the workspace (default `$KEYGATE_DIR` or `./.keygate`).
Most read commands take `--json`.

## Admin API

Same origin as the dashboard, JSON in and out, no auth beyond the loopback bind.

| Method | Path |
|--------|------|
| `GET` | `/admin/api/overview` |
| `GET` `POST` | `/admin/api/users` |
| `PATCH` | `/admin/api/users/{name}` — `{"budget_usd": 5}` or `null` for unlimited |
| `GET` `POST` | `/admin/api/keys` — `?user=&all=` / mint, returns the plaintext once |
| `POST` | `/admin/api/keys/{prefix}/revoke` |
| `GET` | `/admin/api/usage` — `?since=7d&user=&detail=1&limit=` |
| `GET` | `/admin/api/audit` — `?limit=` |

Errors come back as `{"error": {"message": "...", "code": "..."}}`.

## Architecture

A stdlib `ThreadingHTTPServer` that swaps virtual keys for the real upstream
key, meters the response, and serves a packaged static dashboard off the same
port. State is one SQLite file.

```
src/keygate/
  store.py      workspace, sqlite schema, users/keys/usage/audit
  keys.py       kg_v1_ minting, hashing, bearer parsing
  proxy.py      the gateway: auth, budgets, forwarding, SSE metering, routing
  admin.py      /admin/api JSON handlers + static asset lookup
  cli.py        argparse front end
  dashboard/    index.html, app.js, styles.css — shipped in the wheel
```

Pricing lives in `config.json` and is a *starting point*, not a source of
truth — confirm it against your provider's current price list. Unlisted models
fall back to the zero-cost `default` entry so nothing is silently mispriced.

## Development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```

## License

MIT © Yogeshwar C M
