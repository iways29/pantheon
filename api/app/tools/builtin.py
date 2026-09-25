"""The starting toolset (Step 7.2). `create_task` and `report_result` live
with tasks (Step 7.3).

Descriptions here are seed text for the `tools` table; the owner's edited
version is what agents read.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.brain.write_gate import FactCandidate
from app.tools.registry import ToolSpec, register
from app.tools.runtime import ToolContext


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SearchArgs(_Args):
    query: str = Field(min_length=2, max_length=500)
    limit: int = Field(default=5, ge=1, le=10)


def _brain_search(ctx: ToolContext, args: SearchArgs) -> dict[str, Any]:
    if ctx.brain is None:
        raise RuntimeError("The brain is not available in this session")
    matches = ctx.brain.search(args.query, limit=args.limit)
    return {
        "facts": [
            {
                "id": str(m.fact.id),
                "claim": m.fact.claim,
                "source": m.fact.source,
                "distance": round(m.distance, 3),
            }
            for m in matches
        ]
    }


register(
    ToolSpec(
        name="brain_search",
        description="Find facts the company already holds that are close to a query.",
        args=SearchArgs,
        risk_class="R0",
        handler=_brain_search,
    )
)


class ProposeArgs(_Args):
    claim: str = Field(min_length=5, max_length=500)
    evidence: str = Field(
        min_length=5, max_length=6000, description="The text that supports the claim"
    )
    source_ref: str | None = Field(default=None, max_length=500)


def _brain_propose_fact(ctx: ToolContext, args: ProposeArgs) -> dict[str, Any]:
    if ctx.writer is None:
        raise RuntimeError("The brain write gate is not available in this session")
    result = ctx.writer.propose(
        FactCandidate(
            claim=args.claim,
            source=f"agent:{ctx.agent_name}",
            source_text=args.evidence,
            source_ref=args.source_ref or (str(ctx.run_id) if ctx.run_id else None),
        ),
        org_id=ctx.org_id,
        agent_id=ctx.agent_id,
        run_id=ctx.run_id,
    )
    return {
        "outcome": result.outcome,
        "fact_id": str(result.fact.id) if result.fact else None,
        "reasons": list(result.reasons),
    }


register(
    ToolSpec(
        name="brain_propose_fact",
        description=(
            "Propose a factual claim for the company brain, with the text that supports "
            "it. It is checked before it is stored and may be rejected or held."
        ),
        args=ProposeArgs,
        risk_class="R1",
        side_effect=True,
        handler=_brain_propose_fact,
    )
)


def _read_document(ctx: ToolContext, args: SearchArgs) -> dict[str, Any]:
    if ctx.library is None:
        raise RuntimeError("Documents are not available in this session")
    # The session acts for this agent, so RLS returns only its scopes.
    return {
        "passages": [
            {"title": m.title, "scope": m.scope, "text": m.text, "distance": round(m.distance, 3)}
            for m in ctx.library.search(args.query, limit=args.limit)
        ]
    }


register(
    ToolSpec(
        name="read_document",
        description=(
            "Search the documents this agent may read (company, its department's and its "
            "own) and return the most relevant passages."
        ),
        args=SearchArgs,
        risk_class="R0",
        handler=_read_document,
    )
)


class FetchArgs(_Args):
    url: str = Field(min_length=10, max_length=2000)


def _web_fetch_preview(ctx: ToolContext, args: FetchArgs) -> dict[str, Any]:
    if ctx.links is None or ctx.fetcher is None:
        raise RuntimeError("Web access is not available in this session")
    page = ctx.fetcher(args.url)
    preview = ctx.links.preview(page, org_id=ctx.org_id, agent_id=ctx.agent_id)
    output: dict[str, Any] = {
        "url": preview.final_url,
        "preview_id": str(preview.id),
        "screened": preview.label,
    }
    if preview.label == "clean":
        output["claims"] = list(preview.claims)
    else:
        output["reasons"] = list(preview.reasons)
    return output


register(
    ToolSpec(
        name="web_fetch_preview",
        description=(
            "Fetch a public web page, screen it, and list the facts it states. Nothing is "
            "added to the brain; the owner or a later step pushes it."
        ),
        args=FetchArgs,
        risk_class="R2",
        side_effect=True,
        handler=_web_fetch_preview,
        timeout_seconds=30,
    )
)
