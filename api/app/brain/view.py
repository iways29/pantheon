"""What the brain screen reads about facts (Step 10, ADR 036).

Read-only. Every query runs on a connection already acting as the owner
(`db.acting_as`), so RLS decides what is visible, as everywhere else.
"""

from typing import Any
from uuid import UUID

import psycopg

#: Characters of a claim sent with the map; the detail has the whole claim.
SHORT_CLAIM = 160


def facts_on_the_map(cursor: psycopg.Cursor) -> list[dict[str, Any]]:
    """Every fact with its place, in the order it was stored.

    Keys are short: this list is the largest thing the screen loads (about a
    megabyte at 10,000 facts). A fact with no place yet has x and y null.
    """
    cursor.execute(
        f"""
        select f.id, left(f.claim, {SHORT_CLAIM}) as claim, f.status, f.kind, f.visibility,
               f.created_at, p.x, p.y, p.z, p.neighbourhood_id, r.agent_id
          from public.facts f
          left join public.fact_positions p on p.fact_id = f.id
          left join public.runs r on r.id = f.created_by_run_id
         where f.status <> 'retired'
         order by f.created_at, f.id
        """
    )
    return [
        {
            "id": str(r["id"]),
            "claim": r["claim"],
            "status": r["status"],
            "kind": r["kind"],
            "public": r["visibility"] == "public",
            "at": r["created_at"].isoformat(),
            "x": None if r["x"] is None else round(r["x"], 4),
            "y": None if r["y"] is None else round(r["y"], 4),
            "z": None if r["x"] is None else round(r["z"] or 0.0, 4),
            "n": str(r["neighbourhood_id"]) if r["neighbourhood_id"] else None,
            "agent": str(r["agent_id"]) if r["agent_id"] else None,
        }
        for r in cursor.fetchall()
    ]


def neighbourhoods(cursor: psycopg.Cursor) -> list[dict[str, Any]]:
    cursor.execute(
        """
        select n.id, n.x, n.y, n.z, n.label, n.label_source, count(p.fact_id) as size
          from public.brain_neighbourhoods n
          left join public.fact_positions p on p.neighbourhood_id = n.id
         group by n.id order by size desc, n.id
        """
    )
    return [
        {
            "id": str(r["id"]),
            "x": round(r["x"], 4),
            "y": round(r["y"], 4),
            "z": round(r["z"], 4),
            "label": r["label"],
            "named_by": r["label_source"],
            "size": r["size"],
        }
        for r in cursor.fetchall()
    ]


def held_claims(cursor: psycopg.Cursor) -> list[dict[str, Any]]:
    """Claims waiting for the owner: held by the write gate, not yet facts."""
    cursor.execute(
        """
        select a.id, a.payload, a.recommendation, a.created_at, a.agent_id
          from public.approvals a
         where a.status = 'pending' and a.action_type = 'fact_write'
         order by a.created_at
        """
    )
    return [
        {
            "approval_id": str(r["id"]),
            "claim": str((r["payload"] or {}).get("claim", ""))[:SHORT_CLAIM],
            "recommendation": r["recommendation"],
            "at": r["created_at"].isoformat(),
            "agent": str(r["agent_id"]) if r["agent_id"] else None,
        }
        for r in cursor.fetchall()
    ]


def fact_detail(cursor: psycopg.Cursor, fact_id: UUID) -> dict[str, Any] | None:
    """One fact: its claim and source, who found it and when, Jev's check,
    what it replaced or was replaced by, what it contradicts, the things it is
    about, and its nearest facts by meaning."""
    cursor.execute(
        """
        select f.id, f.claim, f.source, f.source_ref, f.quote, f.status, f.kind, f.visibility,
               f.confidence, f.created_at, f.review_after, f.superseded_by, f.admitted_by,
               f.document_id, d.title as document_title,
               r.id as run_id, a.id as agent_id, a.name as agent, dep.name as department,
               p.x, p.y, p.z, p.neighbourhood_id, n.label as neighbourhood
          from public.facts f
          left join public.documents d on d.id = f.document_id
          left join public.runs r on r.id = f.created_by_run_id
          left join public.agents a on a.id = r.agent_id
          left join public.departments dep on dep.id = a.department_id
          left join public.fact_positions p on p.fact_id = f.id
          left join public.brain_neighbourhoods n on n.id = p.neighbourhood_id
         where f.id = %s
        """,
        (str(fact_id),),
    )
    fact = cursor.fetchone()
    if fact is None:
        return None

    check = None
    if fact["admitted_by"] is not None:
        cursor.execute(
            "select gate, question_id, question_version, output, model, created_at "
            "from public.judgments where request_id = %s order by created_at limit 1",
            (str(fact["admitted_by"]),),
        )
        row = cursor.fetchone()
        if row is not None:
            check = {
                "gate": row["gate"],
                "question": f"{row['question_id']} v{row['question_version']}",
                "output": row["output"],
                "model": row["model"],
                "at": row["created_at"].isoformat(),
            }

    cursor.execute(
        "select id, claim, created_at from public.facts where superseded_by = %s "
        "order by created_at",
        (str(fact_id),),
    )
    replaces = [_ref(r) for r in cursor.fetchall()]
    replaced_by = None
    if fact["superseded_by"] is not None:
        cursor.execute(
            "select id, claim, created_at from public.facts where id = %s",
            (str(fact["superseded_by"]),),
        )
        row = cursor.fetchone()
        replaced_by = _ref(row) if row else None

    # A disputed fact names the fact it clashed with in its write event.
    cursor.execute(
        """
        select distinct f.id, f.claim, f.created_at
          from public.events e
          join public.facts f
            on f.id = case when e.payload ->> 'fact_id' = %s
                           then (e.payload ->> 'related_fact_id')::uuid
                           else (e.payload ->> 'fact_id')::uuid end
         where e.type = 'fact_write_decided' and e.payload ->> 'outcome' = 'disputed'
           and (e.payload ->> 'fact_id' = %s or e.payload ->> 'related_fact_id' = %s)
        """,
        (str(fact_id), str(fact_id), str(fact_id)),
    )
    contradicts = [_ref(r) for r in cursor.fetchall()]

    cursor.execute(
        "select e.id, e.kind, e.name from public.fact_entities fe "
        "join public.entities e on e.id = fe.entity_id where fe.fact_id = %s order by e.name",
        (str(fact_id),),
    )
    things = [{"id": str(r["id"]), "kind": r["kind"], "name": r["name"]} for r in cursor]

    cursor.execute(
        """
        select o.id, o.claim, o.created_at, (o.embedding <=> f.embedding) as distance
          from public.facts f join public.facts o on o.id <> f.id and o.embedding is not null
         where f.id = %s and f.embedding is not null
         order by o.embedding <=> f.embedding limit 5
        """,
        (str(fact_id),),
    )
    near = [_ref(r) | {"similarity": round(1 - float(r["distance"]), 3)} for r in cursor]

    hood = fact["neighbourhood_id"]
    return {
        "id": str(fact["id"]),
        "claim": fact["claim"],
        "status": fact["status"],
        "kind": fact["kind"],
        "public": fact["visibility"] == "public",
        "source": fact["source"],
        "source_ref": fact["source_ref"],
        "quote": fact["quote"],
        "document": (
            {"id": str(fact["document_id"]), "title": fact["document_title"]}
            if fact["document_id"]
            else None
        ),
        "confidence": None if fact["confidence"] is None else float(fact["confidence"]),
        "at": fact["created_at"].isoformat(),
        "review_after": fact["review_after"].isoformat() if fact["review_after"] else None,
        "found_by": (
            {
                "agent_id": str(fact["agent_id"]),
                "agent": fact["agent"],
                "department": fact["department"],
            }
            if fact["agent_id"]
            else None
        ),
        "run_id": str(fact["run_id"]) if fact["run_id"] else None,
        "check": check,
        "replaces": replaces,
        "replaced_by": replaced_by,
        "contradicts": contradicts,
        "things": things,
        "neighbours": near,
        "place": (
            None
            if fact["x"] is None
            else {
                "x": fact["x"],
                "y": fact["y"],
                "z": fact["z"] or 0.0,
                "neighbourhood_id": str(hood) if hood else None,
                "neighbourhood": fact["neighbourhood"],
            }
        ),
    }


def _ref(row: dict[str, Any]) -> dict[str, Any]:
    return {"id": str(row["id"]), "claim": row["claim"], "at": row["created_at"].isoformat()}
