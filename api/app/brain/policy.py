"""What may enter the brain (owner, 2026-09-27; ADR 033).

The `brain_policy` flag in `system_flags`. With no row, the defaults below
apply: the brain grows with use (the owner's orders, questions and approved
work, the company's own sources), never with what an agent read on the web.
"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

KEY = "brain_policy"


@dataclass(frozen=True)
class BrainPolicy:
    agent_web_facts: bool = False
    agent_facts: bool = False
    remember_orders: bool = True
    remember_asks: bool = True
    remember_approved_drafts: bool = True
    #: What the owner says in the chat (ADR 037), sorted like an order.
    remember_chat: bool = True


#: Said to an agent whose facts are not kept, so it reports them instead.
NOT_KEPT = (
    "Not kept in the brain: it holds what the company does and says, not what agents "
    "read. Put what you found in your result with report_result; the brief lists it."
)


def load(connection: Any, org_id: UUID | str) -> BrainPolicy:  # noqa: ANN401 - a connection
    row = connection.execute(
        "select value from public.system_flags where org_id = %s and key = %s",
        (str(org_id), KEY),
    ).fetchone()
    value = row["value"] if row and isinstance(row["value"], dict) else {}
    fields = BrainPolicy.__dataclass_fields__
    return BrainPolicy(**{k: bool(v) for k, v in value.items() if k in fields})
