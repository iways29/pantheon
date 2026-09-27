"""Where facts sit on the brain screen (Step 10, ADR 036).

Real pgvector, real projection; only the embedder is the bag-of-words stand-in,
so "near in meaning" here means "shares words".
"""

import math
from uuid import UUID

import psycopg
import pytest

from app.brain import Brain, HashingEmbedder, layout
from app.db import acting_as
from tests.conftest_db import Tenants, admit

TOPICS = {
    "pricing": [
        "The studio pricing plan costs forty dollars a month",
        "The studio pricing plan has an annual discount",
        "The studio pricing plan includes founder office hours",
    ],
    "voice": [
        "The brand voice is calm restrained and composed",
        "The brand voice avoids hype and exclamation marks",
        "The brand voice uses plain verbs and short sentences",
    ],
    "research": [
        "Research scouts early AI startups every morning",
        "Research scouts founders from Show HN and BetaList",
        "Research scouts seed rounds under two million",
    ],
}


def _store(brain: Brain, db: psycopg.Connection, org: UUID, claim: str) -> UUID:
    return brain.insert_fact(org_id=org, claim=claim, admission=admit(db, org)).id


def _positions(db: psycopg.Connection) -> dict[UUID, tuple[float, float, UUID | None, str]]:
    with db.cursor() as cursor:
        cursor.execute("select fact_id, x, y, neighbourhood_id, placed_by from fact_positions")
        return {
            r["fact_id"]: (r["x"], r["y"], r["neighbourhood_id"], r["placed_by"])
            for r in cursor.fetchall()
        }


@pytest.fixture
def seeded(db: psycopg.Connection, tenants: Tenants) -> dict[str, list[UUID]]:
    with acting_as(db, user_id=str(tenants.user_a)) as conn:
        brain = Brain(conn, HashingEmbedder())
        ids = {t: [_store(brain, db, tenants.org_a, c) for c in cs] for t, cs in TOPICS.items()}
        assert _positions(conn) == {}, "no projection yet: nothing is placed on write"
        result = layout.fit(conn, tenants.org_a)
    assert result == {"placed": 9, "neighbourhoods": 2}  # round(sqrt(9 / 2))
    return ids


def test_the_first_fit_places_every_fact_inside_the_disc(
    db: psycopg.Connection, seeded: dict[str, list[UUID]]
) -> None:
    placed = _positions(db)
    assert set(placed) == {i for ids in seeded.values() for i in ids}
    assert all(math.hypot(x, y) <= layout.MAX_RADIUS + 1e-6 for x, y, _, _ in placed.values())
    assert {p[3] for p in placed.values()} == {"fit"}
    # One topic stays in one neighbourhood.
    for ids in seeded.values():
        assert len({placed[i][2] for i in ids}) == 1


def test_neighbourhoods_start_with_a_name(db: psycopg.Connection, seeded: object) -> None:
    with db.cursor() as cursor:
        cursor.execute("select label, label_source from brain_neighbourhoods")
        rows = cursor.fetchall()
    assert all(r["label"] and r["label_source"] == "claim" for r in rows)


def test_a_new_fact_sits_beside_its_neighbours_and_old_ones_never_move(
    db: psycopg.Connection, tenants: Tenants, seeded: dict[str, list[UUID]]
) -> None:
    before = _positions(db)
    with acting_as(db, user_id=str(tenants.user_a)) as conn:
        brain = Brain(conn, HashingEmbedder())
        new = _store(brain, db, tenants.org_a, "The brand voice is calm and never uses hype")
    after = _positions(db)

    assert {k: after[k] for k in before} == before, "placed facts never move"
    x, y, hood, how = after[new]
    assert how == "neighbours"
    voice = seeded["voice"]
    assert hood == before[voice[0]][2]
    cx = sum(before[i][0] for i in voice) / 3
    cy = sum(before[i][1] for i in voice) / 3
    others = [i for t, ids in seeded.items() if t != "voice" for i in ids]
    nearest_other = min(math.hypot(x - before[i][0], y - before[i][1]) for i in others)
    assert math.hypot(x - cx, y - cy) < nearest_other


def test_a_new_topic_starts_at_the_rim_with_its_own_neighbourhood(
    db: psycopg.Connection, tenants: Tenants, seeded: object
) -> None:
    with acting_as(db, user_id=str(tenants.user_a)) as conn:
        brain = Brain(conn, HashingEmbedder())
        new = _store(brain, db, tenants.org_a, "Quantum telescopes orbit Jupiter's moons")
    x, y, hood, how = _positions(db)[new]
    assert how == "edge"
    assert math.hypot(x, y) == pytest.approx(0.92, abs=0.05)
    with db.cursor() as cursor:
        cursor.execute("select label from brain_neighbourhoods where id = %s", (hood,))
        assert cursor.fetchone()["label"] == "Quantum telescopes orbit"


def test_the_librarian_name_sticks(
    db: psycopg.Connection, tenants: Tenants, seeded: object
) -> None:
    with acting_as(db, user_id=str(tenants.user_a)) as conn, conn.cursor() as cursor:
        todo = layout.unnamed(cursor, tenants.org_a, 5)
        assert len(todo) == 2 and all(len(t["claims"]) in (3, 5) for t in todo)
        assert layout.name(cursor, todo[0]["id"], '"Brand voice."') == "Brand voice"
        assert layout.relabel(cursor, todo[0]["id"]) == "Brand voice", "a model name is kept"
        assert [t["id"] for t in layout.unnamed(cursor, tenants.org_a, 5)] == [todo[1]["id"]]


def test_another_org_sees_no_positions(
    db: psycopg.Connection, tenants: Tenants, seeded: object
) -> None:
    with acting_as(db, user_id=str(tenants.user_b)) as conn, conn.cursor() as cursor:
        for table in ("fact_positions", "brain_neighbourhoods", "brain_layouts"):
            cursor.execute(f"select count(*) as n from {table}")
            assert cursor.fetchone()["n"] == 0, table


def test_claim_label_drops_small_words() -> None:
    assert layout.claim_label("The studio's pricing is simple") == "studio's pricing simple"


def test_a_cut_name_does_not_end_on_a_small_word(
    db: psycopg.Connection, tenants: Tenants, seeded: object
) -> None:
    with acting_as(db, user_id=str(tenants.user_a)) as conn, conn.cursor() as cursor:
        first = layout.unnamed(cursor, tenants.org_a, 1)[0]["id"]
        assert layout.name(cursor, first, "Product features and pricing") == "Product features"
        assert layout.name(cursor, first, "The and of") is None
