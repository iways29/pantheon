"""One live call to OpenRouter, to check the transport against the real API.

The transport's parsing was written from documentation and tested against a
mock. This makes one tiny request per mode and reports, field by field,
whether the response carries what `_parse` relies on: the served model, the
provider, token counts and `usage.cost`.

It calls the transport directly, not the gateway, because the transport is the
thing under test and this must run without a database. It is a manual check
for the owner, never something an agent calls.

    cd api && uv run python -m scripts.openrouter_smoke            # cheap tier
    cd api && uv run python -m scripts.openrouter_smoke standard

Cost: two requests capped at 16 output tokens each. On the cheap tier that is
a small fraction of a cent.
"""

import json
import sys
from pathlib import Path
from typing import Any

from app.config import Settings
from app.gateway.errors import UpstreamError
from app.gateway.factory import tier_map_from, transport_from
from app.gateway.transport import SENSITIVE_PROVIDER_PREFERENCES, ModelResponse

ROOT_ENV = Path(__file__).resolve().parents[2] / ".env"
MESSAGES = [{"role": "user", "content": "Reply with the single word: pong"}]
MAX_TOKENS = 16


def _report(label: str, response: ModelResponse) -> bool:
    usage: dict[str, Any] = response.raw.get("usage") or {}
    checks = {
        "text returned": bool(response.text.strip()),
        "tokens_in > 0": response.tokens_in > 0,
        "tokens_out > 0": response.tokens_out > 0,
        "usage.cost present": usage.get("cost") is not None,
        "top-level provider present": response.provider is not None,
    }
    print(f"\n== {label}")
    print(f"  served model : {response.model}")
    print(f"  provider     : {response.provider}")
    print(f"  text         : {response.text.strip()!r}")
    print(f"  tokens in/out: {response.tokens_in}/{response.tokens_out}")
    print(f"  cost_usd     : {response.cost_usd:.8f}")
    print(f"  latency_ms   : {response.latency_ms}")
    print(f"  body keys    : {sorted(response.raw)}")
    print(f"  usage        : {json.dumps(usage, sort_keys=True)}")
    for name, ok in checks.items():
        print(f"  [{'ok' if ok else 'MISSING'}] {name}")
    return all(checks.values())


def main(tier: str) -> int:
    settings = Settings(_env_file=ROOT_ENV)  # type: ignore[call-arg]
    model = tier_map_from(settings).model_for(tier)
    transport = transport_from(settings)
    print(f"tier {tier} -> {model}")

    ok = _report(
        "plain request",
        transport.complete(model=model, messages=MESSAGES, max_tokens=MAX_TOKENS),
    )

    # A refusal here is a finding, not a bug: it means no zero-retention
    # endpoint serves this model, so sensitive work cannot use this tier.
    try:
        _report(
            "sensitive request (data_collection=deny, zdr=true)",
            transport.complete(
                model=model,
                messages=MESSAGES,
                max_tokens=MAX_TOKENS,
                provider_preferences=SENSITIVE_PROVIDER_PREFERENCES,
            ),
        )
    except UpstreamError as error:
        print(f"\n== sensitive request refused: {error}")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "cheap"))
