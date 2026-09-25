"""Endpoints for the platform's own machinery, not for people.

The database's scheduler (ADR 008) creates a run for each due trigger and then
calls here to have it advanced, because the run itself needs the model
gateway, the checkpointer and the brain, which live in this process.
Authentication is a shared secret held in Supabase Vault and in this
service's environment, not a user token: the caller is the database, not a
person.

Calls are safe to repeat. `advance_run` claims a run under a lease, so a
duplicate call for a run that is being advanced, or is already finished,
changes nothing and says so.
"""

import hmac
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from app.agents.runs import RunBusy, RunNotFound, Runtime, advance_run
from app.config import Settings, get_settings
from app.gateway.factory import systemone_transport_from, tier_map_from, transport_from
from app.knowledge.wiring import agent_services
from app.tracing import tracer_from

#: One invocation's time budget. Vercel's function limit for this app is 60s
#: (vercel.json), so this leaves room to record where the run stopped. A run
#: that needs longer is paused and picked up by the scheduler's next poke.
ADVANCE_DEADLINE_SECONDS = 45.0

router = APIRouter(prefix="/internal", tags=["internal"])

_bearer = HTTPBearer(auto_error=False)


class AdvanceResult(BaseModel):
    run_id: UUID
    #: The run's status after this call, or "busy" when another invocation
    #: holds it or it has already finished.
    status: str
    stop_reason: str | None = None
    steps_taken: int | None = None
    cost_usd: float | None = None
    detail: str | None = None


def require_trigger_secret(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    if not settings.trigger_secret:
        # Refuse rather than accept everything: no secret configured must
        # never mean no authentication.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="TRIGGER_SECRET is not configured",
        )
    if credentials is None or not hmac.compare_digest(
        credentials.credentials.encode(), settings.trigger_secret.encode()
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid trigger credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )


def get_runtime(settings: Annotated[Settings, Depends(get_settings)]) -> Runtime:
    if not settings.database_url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="DATABASE_URL is not configured",
        )
    return Runtime(
        dsn=settings.database_url,
        transport=transport_from(settings),
        tiers=tier_map_from(settings),
        embedder=None,
        tracer=tracer_from(settings),
        # Without a TypeSafe key a run still answers, but writes no facts: no
        # fact enters the brain unjudged (ADR 010).
        systemone=systemone_transport_from(settings) if settings.typesafe_api_key else None,
        services=agent_services(),
    )


@router.post("/runs/{run_id}/advance", response_model=AdvanceResult)
def advance(
    run_id: UUID,
    _: Annotated[None, Depends(require_trigger_secret)],
    runtime: Annotated[Runtime, Depends(get_runtime)],
) -> AdvanceResult:
    """Advance a run as far as it will go within this invocation."""
    try:
        result = advance_run(runtime, run_id, deadline_seconds=ADVANCE_DEADLINE_SECONDS)
    except RunNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such run") from None
    except RunBusy as busy:
        return AdvanceResult(run_id=run_id, status="busy", detail=str(busy))
    return AdvanceResult(
        run_id=run_id,
        status=result.status,
        stop_reason=result.stop_reason,
        steps_taken=result.steps_taken,
        cost_usd=float(result.cost_usd),
    )
