"""keygate -- a local, single-tenant API-key gateway.

Hold one real upstream OpenAI-compatible key, hand out per-user virtual keys
(``kg_v1_...``), enforce budgets, and keep an audit trail. Stdlib only.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
