# keygate

**Share one real LLM API key with your team — mint per-user virtual keys, budgets, and an audit log.**

Local-first, **stdlib-only** Python gateway. You keep the provider key. Teammates get `kg_v1_...` virtual keys. Every request is attributed.

> Not LiteLLM Enterprise. A tiny self-hosted control plane for freelancers / small teams who need **identity + budgets** in front of OpenAI-compatible APIs.

## Install

```bash
pip install -e .
export KEYGATE_UPSTREAM_API_KEY='sk-...'
keygate init ./kg-data
keygate user add alice --budget 5
keygate user mint alice   # prints kg_v1_... once
keygate serve --path ./kg-data --host 127.0.0.1 --port 8787
```

Point clients at `http://127.0.0.1:8787/v1` with `Authorization: Bearer kg_v1_...`.

## Commands

| Command | Purpose |
|---------|---------|
| `keygate init` | Create workspace (sqlite + config) |
| `keygate user add/list` | Manage users + budgets |
| `keygate user mint` | Issue virtual key (shown once) |
| `keygate serve` | Run OpenAI-compatible proxy |
| `keygate usage` | Who spent what |

## License

MIT © Yogeshwar C M
