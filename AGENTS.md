# AGENTS.md — keygate

Local org virtual-key gateway (stdlib). Core: `src/keygate/{store,keys,proxy,admin,cli}.py`.
Dashboard assets (vanilla JS, no build step): `src/keygate/dashboard/`, served by
`proxy.py` under `/dash`; JSON API in `admin.py` under `/admin/api`. Tests: `tests/`.
Run `pytest`. No third-party runtime deps — keep it that way.
