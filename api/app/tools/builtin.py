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


# --- Tasks (Step 7.3) ---------------------------------------------------------


class CreateTaskArgs(_Args):
    assign_to: str = Field(min_length=1, max_length=64, description="The agent's name")
    title: str = Field(min_length=3, max_length=200)
    instructions: str = Field(min_length=3, max_length=4000)
    max_cost_usd: float | None = Field(default=None, ge=0, le=10)


def _create_task(ctx: ToolContext, args: CreateTaskArgs) -> dict[str, Any]:
    from decimal import Decimal

    from app.tasks import delegate

    task_id = ctx.extras.get("task_id")
    if not task_id:
        raise RuntimeError("create_task works only inside a task")
    with ctx.connection.cursor() as cursor:
        task = delegate(
            cursor,
            org_id=ctx.org_id,
            parent_task_id=task_id,
            by_agent_id=ctx.agent_id,
            to_agent=args.assign_to,
            title=args.title,
            instructions=args.instructions,
            max_cost_usd=None if args.max_cost_usd is None else Decimal(str(args.max_cost_usd)),
        )
    return {"task_id": str(task.id), "status": task.status, "new": task.created}


register(
    ToolSpec(
        name="create_task",
        description=(
            "Hand part of your task to another agent as a sub-task. You stop after planning; "
            "you are woken with their results when all your sub-tasks finish."
        ),
        args=CreateTaskArgs,
        risk_class="R1",
        side_effect=True,
        handler=_create_task,
    )
)


class ReportArgs(_Args):
    summary: str = Field(min_length=3, max_length=4000)
    details: dict[str, Any] = Field(default_factory=dict)


def _report_result(ctx: ToolContext, args: ReportArgs) -> dict[str, Any]:
    from app.tasks import report_result

    task_id = ctx.extras.get("task_id")
    if not task_id:
        raise RuntimeError("report_result works only inside a task")
    with ctx.connection.cursor() as cursor:
        report_result(
            cursor,
            task_id=task_id,
            agent_id=ctx.agent_id,
            result={"summary": args.summary, **args.details},
        )
    return {"recorded": True}


register(
    ToolSpec(
        name="report_result",
        description="Record the short result of the task you were given, for whoever asked.",
        args=ReportArgs,
        risk_class="R1",
        side_effect=True,
        handler=_report_result,
    )
)


# --- Research department (Step 8.1) --------------------------------------------


class PushArgs(_Args):
    preview_id: str = Field(min_length=36, max_length=36)


def _web_push_preview(ctx: ToolContext, args: PushArgs) -> dict[str, Any]:
    if ctx.links is None:
        raise RuntimeError("Web access is not available in this session")
    preview = ctx.links.push(args.preview_id, org_id=ctx.org_id)
    return {
        "preview_id": str(preview.id),
        "status": preview.status,
        "results": [
            {"claim": r["claim"], "outcome": r["outcome"], "reasons": r["reasons"]}
            for r in (preview.results or [])
        ],
    }


register(
    ToolSpec(
        name="web_push_preview",
        description=(
            "Send the facts of a page you previewed, and that was screened clean, through "
            "the brain's write gate. Each fact is checked against the page and the brain; "
            "some may be rejected or held for the owner."
        ),
        args=PushArgs,
        risk_class="R1",
        side_effect=True,
        handler=_web_push_preview,
    )
)


class HygieneArgs(_Args):
    limit: int = Field(default=20, ge=1, le=50)


def _brain_hygiene_scan(ctx: ToolContext, args: HygieneArgs) -> dict[str, Any]:
    """Facts that need a look: past their review date, or disputed."""
    with ctx.connection.cursor() as cursor:
        cursor.execute(
            """
            select id, claim, source, source_ref, status, review_after, created_at
            from public.facts
            where org_id = %s
              and (status = 'disputed'
                   or (status = 'active' and review_after is not null
                       and review_after <= current_date))
            order by status = 'disputed' desc, review_after nulls last, created_at
            limit %s
            """,
            (str(ctx.org_id), args.limit),
        )
        rows = cursor.fetchall()
        cursor.execute(
            "select count(*) filter (where status = 'active') as active, "
            "count(*) filter (where status = 'disputed') as disputed, "
            "count(*) filter (where status = 'superseded') as superseded "
            "from public.facts where org_id = %s",
            (str(ctx.org_id),),
        )
        totals = cursor.fetchone()
    return {
        "totals": dict(totals),
        "needs_a_look": [
            {
                "fact_id": str(r["id"]),
                "claim": r["claim"],
                "source": r["source"],
                "source_ref": r["source_ref"],
                "why": "disputed" if r["status"] == "disputed" else "past its review date",
                "review_after": str(r["review_after"]) if r["review_after"] else None,
            }
            for r in rows
        ],
    }


register(
    ToolSpec(
        name="brain_hygiene_scan",
        description=(
            "List facts in the brain that need a look: disputed ones, and ones past their "
            "review date (prices, headcounts, titles and other things that change). "
            "Changes nothing."
        ),
        args=HygieneArgs,
        risk_class="R0",
        handler=_brain_hygiene_scan,
    )
)
