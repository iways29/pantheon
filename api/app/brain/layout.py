"""Where each fact sits on the brain screen (Step 10, ADR 036).

Facts near in meaning sit near each other. The first time, every fact's
embedding is projected to two dimensions (the two strongest directions of the
embeddings, a principal-component projection) and the facts are grouped into
neighbourhoods (k-means by meaning). The projection is kept
(`brain_layouts`), and from then on:

- a new fact is placed beside its nearest placed facts (by meaning) and joins
  their neighbourhood;
- a fact on a new topic (nothing near enough) goes to the rim, in the
  direction the projection gives it, and starts a neighbourhood there;
- a placed fact never moves, so the owner's map stays the owner's map.

No model is called here: positions come from the embeddings already stored.
Neighbourhood names start from the graph's commonest thing or a claim; the
librarian names them properly (one cheap call each, `name_neighbourhood`).
The settings are the `brain_layout` flag (data).
"""

import math
import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import numpy as np
import psycopg

FLAG = "brain_layout"
DEFAULTS: dict[str, Any] = {
    # Placed facts a new fact is averaged from.
    "neighbours": 3,
    # Cosine distance beyond which a fact is a new topic, placed at the rim.
    "new_topic_distance": 0.45,
    # How far a new fact is nudged from its neighbours, so none overlap.
    "jitter": 0.03,
    "max_neighbourhoods": 24,
    "edge_radius": 0.92,
    # Neighbourhoods the librarian names per run, and the tokens each name
    # may use: the cheap tier reasons first, so a small cap returns nothing.
    "names_per_run": 3,
    "name_max_tokens": 200,
}
#: The fitted facts fill this much of the unit disc (95th percentile).
FILL = 0.85
MAX_RADIUS = 0.97


@dataclass(frozen=True)
class Placement:
    fact_id: UUID
    x: float
    y: float
    neighbourhood_id: UUID | None
    placed_by: str


def settings(cursor: psycopg.Cursor, org_id: UUID | str) -> dict[str, Any]:
    cursor.execute(
        "select value from public.system_flags where org_id = %s and key = %s",
        (str(org_id), FLAG),
    )
    row = cursor.fetchone()
    return DEFAULTS | (row["value"] if row and isinstance(row["value"], dict) else {})


# --- The first projection ---------------------------------------------------------


def fit(connection: psycopg.Connection, org_id: UUID | str) -> dict[str, int]:
    """Place every unplaced fact. The first call fits the projection and the
    neighbourhoods; later calls place the stragglers one by one."""
    with connection.cursor() as cursor:
        cursor.execute("select 1 from public.brain_layouts where org_id = %s", (str(org_id),))
        if cursor.fetchone() is not None:
            return {"placed": place_missing(cursor, org_id), "neighbourhoods": 0}
        cursor.execute(
            "select f.id, f.claim, f.embedding, f.embedding_model from public.facts f "
            "where f.org_id = %s and f.embedding is not null "
            "and not exists (select 1 from public.fact_positions p where p.fact_id = f.id) "
            "order by f.created_at, f.id",
            (str(org_id),),
        )
        rows = cursor.fetchall()
        if not rows:
            return {"placed": 0, "neighbourhoods": 0}
        conf = settings(cursor, org_id)

        vectors = _unit(np.array([_array(r["embedding"]) for r in rows]))
        mean = vectors.mean(axis=0)
        axes = _axes(vectors - mean)
        projected = (vectors - mean) @ axes.T
        radii = np.linalg.norm(projected, axis=1)
        top = float(np.percentile(radii, 95)) if len(rows) > 1 else 0.0
        spread = top / FILL if top > 1e-9 else 1.0
        points = [_clamp(p / spread) for p in projected]

        cursor.execute(
            "insert into public.brain_layouts "
            "(org_id, mean, axis_x, axis_y, spread, embedding_model, fact_count) "
            "values (%s, %s, %s, %s, %s, %s, %s)",
            (
                str(org_id),
                mean.tolist(),
                axes[0].tolist(),
                axes[1].tolist(),
                spread,
                rows[0]["embedding_model"],
                len(rows),
            ),
        )

        k = max(1, min(int(conf["max_neighbourhoods"]), round(math.sqrt(len(rows) / 2))))
        groups = _kmeans(vectors, k)
        neighbourhoods: dict[int, UUID] = {}
        for group in sorted(set(groups)):
            members = [points[i] for i, g in enumerate(groups) if g == group]
            cx = sum(p[0] for p in members) / len(members)
            cy = sum(p[1] for p in members) / len(members)
            cursor.execute(
                "insert into public.brain_neighbourhoods (org_id, x, y) values (%s, %s, %s) "
                "returning id",
                (str(org_id), cx, cy),
            )
            neighbourhoods[group] = cursor.fetchone()["id"]

        for row, point, group in zip(rows, points, groups, strict=True):
            _insert(
                cursor,
                org_id,
                Placement(row["id"], point[0], point[1], neighbourhoods[group], "fit"),
            )
        for neighbourhood_id in neighbourhoods.values():
            relabel(cursor, neighbourhood_id)
        return {"placed": len(rows), "neighbourhoods": len(neighbourhoods)}


def reset(connection: psycopg.Connection, org_id: UUID | str) -> None:
    """Forget every position and the projection (the owner's choice only: the
    whole map is redrawn on the next `fit`)."""
    with connection.cursor() as cursor:
        cursor.execute("delete from public.fact_positions where org_id = %s", (str(org_id),))
        cursor.execute("delete from public.brain_neighbourhoods where org_id = %s", (str(org_id),))
        cursor.execute("delete from public.brain_layouts where org_id = %s", (str(org_id),))


# --- One fact at a time -------------------------------------------------------------


def place(
    cursor: psycopg.Cursor,
    org_id: UUID | str,
    fact_id: UUID,
    embedding: Any,  # noqa: ANN401
) -> Placement | None:
    """Place one new fact. None when there is no projection yet (the first
    `fit` places it) or the fact is already placed."""
    cursor.execute("select * from public.brain_layouts where org_id = %s", (str(org_id),))
    layout = cursor.fetchone()
    if layout is None:
        return None
    cursor.execute("select 1 from public.fact_positions where fact_id = %s", (str(fact_id),))
    if cursor.fetchone() is not None:
        return None
    conf = settings(cursor, org_id)
    vector = _array(embedding)
    cursor.execute(
        "select p.x, p.y, p.neighbourhood_id, (f.embedding <=> %s::vector) as distance "
        "from public.fact_positions p join public.facts f on f.id = p.fact_id "
        "where p.org_id = %s and f.embedding is not null and f.id <> %s "
        "order by f.embedding <=> %s::vector limit %s",
        (vector.tolist(), str(org_id), str(fact_id), vector.tolist(), int(conf["neighbours"])),
    )
    near = cursor.fetchall()
    jx, jy = _jitter(fact_id, float(conf["jitter"]))

    if near and float(near[0]["distance"]) <= float(conf["new_topic_distance"]):
        weights = [1.0 / (float(n["distance"]) + 0.05) for n in near]
        total = sum(weights)
        x = sum(w * n["x"] for w, n in zip(weights, near, strict=True)) / total
        y = sum(w * n["y"] for w, n in zip(weights, near, strict=True)) / total
        placement = Placement(
            fact_id, *_clamp(np.array([x + jx, y + jy])), near[0]["neighbourhood_id"], "neighbours"
        )
        _insert(cursor, org_id, placement)
        return placement

    # A new topic: at the rim, in the direction the projection points.
    unit = vector / (np.linalg.norm(vector) or 1.0)
    centred = unit - _array(layout["mean"])
    direction = np.array(
        [float(centred @ _array(layout["axis_x"])), float(centred @ _array(layout["axis_y"]))]
    )
    length = float(np.linalg.norm(direction))
    if length < 1e-9:
        angle = (fact_id.int % 3600) / 3600 * 2 * math.pi
        direction, length = np.array([math.cos(angle), math.sin(angle)]), 1.0
    radius = float(conf["edge_radius"])
    x, y = _clamp(direction / length * radius + np.array([jx, jy]))
    neighbourhood_id = None
    cursor.execute(
        "select count(*) as n from public.brain_neighbourhoods where org_id = %s", (str(org_id),)
    )
    if cursor.fetchone()["n"] < int(conf["max_neighbourhoods"]):
        cursor.execute(
            "insert into public.brain_neighbourhoods (org_id, x, y) values (%s, %s, %s) "
            "returning id",
            (str(org_id), x, y),
        )
        neighbourhood_id = cursor.fetchone()["id"]
    elif near:
        neighbourhood_id = near[0]["neighbourhood_id"]
    placement = Placement(fact_id, x, y, neighbourhood_id, "edge")
    _insert(cursor, org_id, placement)
    if neighbourhood_id is not None:
        relabel(cursor, neighbourhood_id)
    return placement


def place_quietly(
    connection: psycopg.Connection,
    org_id: UUID | str,
    fact_id: UUID,
    embedding: Any,  # noqa: ANN401
) -> Placement | None:
    """Place a fact as it is stored, never failing the write: a fact that
    cannot be placed now is placed by the next `fit` (`place_missing`)."""
    try:
        with connection.transaction(), connection.cursor() as cursor:
            return place(cursor, org_id, fact_id, embedding)
    except psycopg.Error:
        return None


def place_missing(cursor: psycopg.Cursor, org_id: UUID | str, *, limit: int = 500) -> int:
    cursor.execute(
        "select f.id, f.embedding from public.facts f where f.org_id = %s "
        "and f.embedding is not null "
        "and not exists (select 1 from public.fact_positions p where p.fact_id = f.id) "
        "order by f.created_at, f.id limit %s",
        (str(org_id), limit),
    )
    placed = 0
    for row in cursor.fetchall():
        if place(cursor, org_id, row["id"], row["embedding"]) is not None:
            placed += 1
    return placed


# --- Names ------------------------------------------------------------------------


def relabel(cursor: psycopg.Cursor, neighbourhood_id: UUID) -> str:
    """A starting name, unless the librarian has named it: the thing most of
    its facts are about, else the start of its first claim."""
    cursor.execute(
        "select label, label_source from public.brain_neighbourhoods where id = %s",
        (str(neighbourhood_id),),
    )
    current = cursor.fetchone()
    if current is None or current["label_source"] == "model":
        return current["label"] if current else ""
    cursor.execute(
        "select e.name, count(*) as n from public.fact_positions p "
        "join public.fact_entities fe on fe.fact_id = p.fact_id "
        "join public.entities e on e.id = fe.entity_id and e.status = 'active' "
        "where p.neighbourhood_id = %s group by e.name order by n desc, e.name limit 1",
        (str(neighbourhood_id),),
    )
    top = cursor.fetchone()
    if top is not None:
        label, source = top["name"][:60], "entity"
    else:
        cursor.execute(
            "select f.claim from public.fact_positions p join public.facts f on f.id = p.fact_id "
            "where p.neighbourhood_id = %s order by f.created_at, f.id limit 1",
            (str(neighbourhood_id),),
        )
        first = cursor.fetchone()
        label, source = (claim_label(first["claim"]) if first else ""), "claim"
    cursor.execute(
        "update public.brain_neighbourhoods set label = %s, label_source = %s where id = %s",
        (label, source, str(neighbourhood_id)),
    )
    return label


#: Words a name never ends on ("Product features and").
SMALL_WORDS = frozenset(
    {"a", "an", "the", "of", "and", "or", "to", "in", "on", "for", "with", "is", "are", "was", "&"}
)


def claim_label(claim: str, words: int = 3) -> str:
    """The first few words of a claim, without the small ones at the ends."""
    small = SMALL_WORDS
    tokens = re.findall(r"[\w'\u2019-]+", claim)
    picked = [t for t in tokens if t.lower() not in small][:words]
    return " ".join(picked)[:60]


def unnamed(cursor: psycopg.Cursor, org_id: UUID | str, limit: int) -> list[dict[str, Any]]:
    """Neighbourhoods the librarian has not named yet, the biggest first, with
    up to five of their claims."""
    cursor.execute(
        "select n.id, count(p.fact_id) as size from public.brain_neighbourhoods n "
        "join public.fact_positions p on p.neighbourhood_id = n.id "
        "where n.org_id = %s and n.label_source <> 'model' "
        "group by n.id order by size desc, n.id limit %s",
        (str(org_id), limit),
    )
    out = []
    for row in cursor.fetchall():
        cursor.execute(
            "select f.claim from public.fact_positions p join public.facts f on f.id = p.fact_id "
            "where p.neighbourhood_id = %s and f.status <> 'superseded' "
            "order by f.created_at desc limit 5",
            (str(row["id"]),),
        )
        out.append({"id": row["id"], "claims": [r["claim"] for r in cursor.fetchall()]})
    return out


def name(cursor: psycopg.Cursor, neighbourhood_id: UUID, label: str) -> str | None:
    """Keep the librarian's name: one to three plain words."""
    cleaned = re.sub(r"[\"'`*#.:]", "", label).strip()
    kept = cleaned.split()[:3]
    while kept and kept[-1].lower() in SMALL_WORDS:
        kept.pop()
    cleaned = " ".join(kept)[:40]
    if not cleaned:
        return None
    cursor.execute(
        "update public.brain_neighbourhoods set label = %s, label_source = 'model' where id = %s",
        (cleaned, str(neighbourhood_id)),
    )
    return cleaned


# --- Arithmetic -------------------------------------------------------------------


def _array(value: Any) -> np.ndarray:  # noqa: ANN401 - a pgvector Vector, list or array
    if hasattr(value, "to_numpy"):
        return np.asarray(value.to_numpy(), dtype=float)
    if hasattr(value, "to_list"):
        return np.asarray(value.to_list(), dtype=float)
    return np.asarray(value, dtype=float)


def _unit(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    return matrix / np.where(norms == 0, 1.0, norms)


def _axes(centred: np.ndarray) -> np.ndarray:
    """The two strongest directions, each with a fixed sign so a refit of the
    same facts draws the same map. Fewer than two facts: any two directions."""
    dims = centred.shape[1]
    axes: list[np.ndarray] = []
    if centred.shape[0] >= 2 and np.any(centred):
        _, _, vt = np.linalg.svd(centred, full_matrices=False)
        axes = [v for v in vt[:2] if np.linalg.norm(v) > 0]
    for i in range(dims):
        if len(axes) == 2:
            break
        basis = np.zeros(dims)
        basis[i] = 1.0
        for a in axes:
            basis -= (basis @ a) * a
        if np.linalg.norm(basis) > 1e-6:
            axes.append(basis / np.linalg.norm(basis))
    fixed = [a if a[int(np.argmax(np.abs(a)))] >= 0 else -a for a in axes]
    return np.vstack(fixed)


def _kmeans(vectors: np.ndarray, k: int, rounds: int = 25) -> list[int]:
    """Groups by meaning (cosine), seeded by the farthest-point rule from the
    oldest fact, so the same facts always make the same groups."""
    k = min(k, len(vectors))
    centres = [vectors[0]]
    while len(centres) < k:
        similarity = np.max(vectors @ np.vstack(centres).T, axis=1)
        centres.append(vectors[int(np.argmin(similarity))])
    matrix = np.vstack(centres)
    groups = np.zeros(len(vectors), dtype=int)
    for _ in range(rounds):
        new = np.argmax(vectors @ matrix.T, axis=1)
        if _ and np.array_equal(new, groups):
            break
        groups = new
        for g in range(k):
            members = vectors[groups == g]
            if len(members):
                centre = members.mean(axis=0)
                matrix[g] = centre / (np.linalg.norm(centre) or 1.0)
    # Renumber in order of first appearance: stable ids for the same input.
    order: dict[int, int] = {}
    return [order.setdefault(int(g), len(order)) for g in groups]


def _clamp(point: np.ndarray) -> tuple[float, float]:
    radius = float(np.linalg.norm(point))
    if radius > MAX_RADIUS:
        point = point / radius * MAX_RADIUS
    return float(point[0]), float(point[1])


def _jitter(fact_id: UUID, size: float) -> tuple[float, float]:
    angle = (fact_id.int % 3600) / 3600 * 2 * math.pi
    radius = size * ((fact_id.int >> 12) % 1000) / 1000
    return radius * math.cos(angle), radius * math.sin(angle)


def _insert(cursor: psycopg.Cursor, org_id: UUID | str, placement: Placement) -> None:
    cursor.execute(
        "insert into public.fact_positions (fact_id, org_id, x, y, neighbourhood_id, placed_by) "
        "values (%s, %s, %s, %s, %s, %s) on conflict (fact_id) do nothing",
        (
            str(placement.fact_id),
            str(org_id),
            placement.x,
            placement.y,
            str(placement.neighbourhood_id) if placement.neighbourhood_id else None,
            placement.placed_by,
        ),
    )
