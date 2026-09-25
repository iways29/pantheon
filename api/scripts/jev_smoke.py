"""One live call to TypeSafe, to check the transport against the real API.

The transport was written from the API reference and tested against a
scripted server. This makes one small request with one question of each type
(the reference's own example) and reports, field by field, whether the answer
carries what the judge relies on. It also prints the response headers, to
answer an open question from the research note: does TypeSafe return a
request id we could quote to their support?

It calls the transport directly, not the judge, because the transport is the
thing under test and this must run without a database. A manual check for the
owner, never something an agent calls.

    cd api && uv run python -m scripts.jev_smoke

Cost: one request of about 300 input tokens at $0.042 per million, roughly
$0.00001.
"""

import sys
from pathlib import Path

import httpx

from app.config import Settings
from app.gateway import ChoiceAnswer, NoulAnswer, Question, ScoreAnswer, UpstreamError
from app.gateway.systemone import TypeSafeTransport

ROOT_ENV = Path(__file__).resolve().parents[2] / ".env"
MODEL = "jev-1.13.0"
STATE = "Help! My payouts have been failing for 3 days."
QUESTIONS = {
    "is_urgent": Question(
        type="noul",
        instructions="Does this convey urgency?",
        criteria={"true": "Explicitly time-sensitive", "false": "No urgency expressed"},
    ),
    "department": Question(
        type="choice",
        instructions="Which team should handle this?",
        criteria={
            "billing": "Payments, invoicing, refunds",
            "technical": "Bugs, outages, integrations",
            "sales": "Pricing, upgrades, new accounts",
        },
    ),
    "frustration": Question(
        type="score",
        instructions="How frustrated is the customer?",
        criteria=["Calm", "Frustrated", "Very angry"],
    ),
}


def main() -> int:
    settings = Settings(_env_file=ROOT_ENV)  # type: ignore[call-arg]
    key = settings.typesafe_api_key
    if not key:
        print("TYPESAFE_API_KEY is not set in the repository's .env")
        return 1
    print(f"key present ({len(key)} characters); endpoint {settings.typesafe_base_url}")

    headers: dict[str, str] = {}

    def keep_headers(response: httpx.Response) -> None:
        headers.update(response.headers)

    client = httpx.Client(timeout=10.0, event_hooks={"response": [keep_headers]})
    transport = TypeSafeTransport(key, base_url=settings.typesafe_base_url, client=client)
    try:
        response = transport.evaluate(model=MODEL, state=STATE, questions=QUESTIONS)
    except UpstreamError as error:
        print(f"FAILED: {error} (reason {error.reason}, status {error.status})")
        return 1

    urgent = response.answers.get("is_urgent")
    department = response.answers.get("department")
    frustration = response.answers.get("frustration")
    checks = {
        f"answered by the pinned model {MODEL}": response.model == MODEL,
        "noul answer parsed": isinstance(urgent, NoulAnswer),
        "choice answer parsed, option known": isinstance(department, ChoiceAnswer)
        and department.choice in (QUESTIONS["department"].criteria or {}),
        "choice probabilities sum to 1": isinstance(department, ChoiceAnswer)
        and abs(sum(department.probabilities.values()) - 1) < 0.01,
        "score answer parsed, within the levels": isinstance(frustration, ScoreAnswer)
        and 0 <= frustration.score <= 2,
        "input tokens reported": response.tokens_in > 0,
    }

    print(f"served model : {response.model}")
    print(f"latency      : {response.latency_ms} ms")
    print(f"tokens in/out: {response.tokens_in}/{response.tokens_out}")
    print(f"cost at $0.042/Mtok: ${response.tokens_in * 0.042 / 1_000_000:.8f}")
    for key_, answer in response.answers.items():
        print(f"answer       : {key_} = {answer.model_dump()}")
    print(f"body keys    : {sorted(response.raw)}")
    interesting = {
        k: v for k, v in headers.items() if "id" in k.lower() or k.lower().startswith("x-")
    }
    print(f"id headers   : {interesting or 'none'}")
    for name, ok in checks.items():
        print(f"  [{'ok' if ok else 'MISSING'}] {name}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
