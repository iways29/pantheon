"""Embedding generation for the brain.

Real embeddings come from the gateway (`GatewayEmbedder`, ADR 013), the only
path to a model: the org's embedding model is assigned in the database. The
free `HashingEmbedder` remains for tests: it is deterministic, needs no
network, and -- unlike random vectors -- puts texts that share vocabulary near
each other, so similarity search can be tested for real rather than mocked
away.

Every embedder has a `name`, stored beside each vector. Vectors from two
different models cannot be compared, so search only compares rows embedded by
the model in use.
"""

import hashlib
import math
import re
from typing import TYPE_CHECKING, Protocol, runtime_checkable
from uuid import UUID

if TYPE_CHECKING:
    from app.gateway import Gateway

# Must match the vector(1536) column in the facts table. Changing one without
# the other needs a migration and a re-embed of every stored fact.
EMBEDDING_DIMENSIONS = 1536

_TOKEN = re.compile(r"[a-z0-9']+")

#: What rows embedded by HashingEmbedder record as their model.
HASHING_MODEL = "hashing-v1"


@runtime_checkable
class Embedder(Protocol):
    """The brain's only view of embedding generation.

    Keeping this a protocol is what lets Step 2 swap a gateway-backed embedder
    in without the brain module changing.
    """

    @property
    def dimensions(self) -> int: ...

    @property
    def name(self) -> str:
        """The model behind the vectors, stored with each one."""
        ...

    def embed(self, text: str) -> list[float]: ...


class HashingEmbedder:
    """A bag-of-words embedder hashed into a fixed number of dimensions.

    Crude by design. It is not a semantic model -- it cannot tell that "car"
    and "automobile" are related -- but it is stable across processes and
    machines, costs nothing, and preserves enough signal for tests to assert
    that a relevant fact ranks above an irrelevant one.
    """

    def __init__(self, dimensions: int = EMBEDDING_DIMENSIONS) -> None:
        if dimensions <= 0:
            raise ValueError("dimensions must be positive")
        self._dimensions = dimensions

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def name(self) -> str:
        return HASHING_MODEL

    def embed(self, text: str) -> list[float]:
        vector = [0.0] * self._dimensions

        for token in _TOKEN.findall(text.lower()):
            # blake2b rather than hash(): Python salts str hashing per process,
            # so hash() would give a different vector on every run.
            digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
            index = int.from_bytes(digest, "big") % self._dimensions
            vector[index] += 1.0

        return _normalise(vector)


def _normalise(vector: list[float]) -> list[float]:
    """Scale to unit length so cosine distance depends only on direction.

    An all-zero vector (text with no tokens) is returned unchanged; pgvector
    treats cosine distance against a zero vector as undefined, so callers must
    not store one.
    """
    magnitude = math.sqrt(sum(component * component for component in vector))
    if magnitude == 0.0:
        return vector
    return [component / magnitude for component in vector]


class GatewayEmbedder:
    """Embeds through the gateway, on behalf of one agent (ADR 013).

    Costed to the agent's department and subject to the kill switch and
    budgets like any model call. The model is whatever the org has assigned
    to the `embedding` tier; `name` is only known after the first call.
    """

    def __init__(
        self,
        gateway: "Gateway",
        *,
        agent_id: UUID | str,
        run_id: UUID | str | None = None,
        sensitive: bool = False,
    ) -> None:
        self._gateway = gateway
        self._agent_id = agent_id
        self._run_id = run_id
        self._sensitive = sensitive
        self._name: str | None = None

    @property
    def dimensions(self) -> int:
        return EMBEDDING_DIMENSIONS

    @property
    def name(self) -> str:
        if self._name is None:
            raise RuntimeError("The embedding model is known only after the first embed")
        return self._name

    def embed(self, text: str) -> list[float]:
        return self.embed_many([text])[0]

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        """One request per batch of up to 64 texts."""
        vectors: list[list[float]] = []
        for start in range(0, len(texts), 64):
            response = self._gateway.embed(
                agent_id=self._agent_id,
                texts=texts[start : start + 64],
                run_id=self._run_id,
                sensitive=self._sensitive,
            )
            self._name = response.requested_model or response.model
            vectors.extend(response.vectors)
        return vectors
