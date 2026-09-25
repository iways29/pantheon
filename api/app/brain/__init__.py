"""The brain: the only path to facts.

Agents read and write claims through `Brain`. Nothing else touches the facts
table directly, so provenance and embeddings cannot be skipped by a caller in
a hurry.
"""

from app.brain.embeddings import (
    EMBEDDING_DIMENSIONS,
    HASHING_MODEL,
    Embedder,
    GatewayEmbedder,
    HashingEmbedder,
)
from app.brain.store import Admission, Brain, BrainError, Fact, FactMatch

__all__ = [
    "EMBEDDING_DIMENSIONS",
    "HASHING_MODEL",
    "Admission",
    "Brain",
    "BrainError",
    "Embedder",
    "Fact",
    "FactMatch",
    "GatewayEmbedder",
    "HashingEmbedder",
]
