"""Embedding generation for the brain.

Real embeddings come from the gateway, which is Step 2 and the only path to a
model. Until it exists, `HashingEmbedder` stands in: it is deterministic, needs
no network, and -- unlike random vectors -- puts texts that share vocabulary
near each other, so similarity search can be tested for real rather than
mocked away.
"""

import hashlib
import math
import re
from typing import Protocol, runtime_checkable

# Must match the vector(1536) column in the facts table. Changing one without
# the other needs a migration and a re-embed of every stored fact.
EMBEDDING_DIMENSIONS = 1536

_TOKEN = re.compile(r"[a-z0-9']+")


@runtime_checkable
class Embedder(Protocol):
    """The brain's only view of embedding generation.

    Keeping this a protocol is what lets Step 2 swap a gateway-backed embedder
    in without the brain module changing.
    """

    @property
    def dimensions(self) -> int: ...

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
