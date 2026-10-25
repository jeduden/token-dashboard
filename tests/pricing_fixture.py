"""Fixed price table for tests, so they do not move when pricing.json is updated."""
_OPUS = {"input": 15.00, "output": 75.00, "cache_read": 1.50, "cache_create_5m": 18.75, "cache_create_1h": 30.00}
_SONNET = {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_create_5m": 3.75, "cache_create_1h": 6.00}
_HAIKU = {"input": 1.00, "output": 5.00, "cache_read": 0.10, "cache_create_5m": 1.25, "cache_create_1h": 2.00}

PRICING = {
    "models": {
        "claude-opus-4-7": {"tier": "opus", **_OPUS},
        "claude-opus-4-6": {"tier": "opus", **_OPUS},
        "claude-sonnet-4-6": {"tier": "sonnet", **_SONNET},
        "claude-haiku-4-5": {"tier": "haiku", **_HAIKU},
    },
    "tier_fallback": {"opus": _OPUS, "sonnet": _SONNET, "haiku": _HAIKU},
    "plans": {
        "api": {"monthly": 0, "label": "API (pay-per-token)"},
        "pro": {"monthly": 20, "label": "Pro"},
    },
}
