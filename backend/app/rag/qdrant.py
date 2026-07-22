"""Qdrant-backed RAG indexing.

The embedding function is deterministic and local so the ingestion contract can
run in private deployments without a second external AI dependency. The Qdrant
adapter boundary is intentionally narrow; replacing ``build_problem_vector``
with a provider-backed embedding later will not change repository or API code.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import blake2b
from importlib import import_module
from typing import Any, Protocol

from app.domain.models import JsonDict


class QdrantClient(Protocol):
    async def collection_exists(self, collection_name: str) -> bool: ...

    async def create_collection(self, collection_name: str, vectors_config: Any) -> Any: ...

    async def upsert(self, collection_name: str, points: list[Any]) -> Any: ...

    async def close(self) -> None: ...


@dataclass(frozen=True)
class QdrantIndexerConfig:
    url: str
    collection: str = "problem_vectors"
    vector_size: int = 64


def build_problem_vector(problem: JsonDict, *, size: int) -> list[float]:
    text = " ".join(
        str(part)
        for part in (
            problem.get("grade_level", ""),
            problem.get("problem_type", ""),
            problem.get("difficulty", ""),
            problem.get("problem_text", ""),
            problem.get("reference_answer", ""),
            " ".join(problem.get("tags", []) or []),
        )
    )
    vector: list[float] = []
    for index in range(size):
        digest = blake2b(f"{index}:{text}".encode(), digest_size=4).digest()
        integer = int.from_bytes(digest, "big")
        vector.append((integer / 0xFFFFFFFF) * 2 - 1)
    return vector


class QdrantIndexer:
    def __init__(self, config: QdrantIndexerConfig, client: QdrantClient | None = None) -> None:
        self.config = config
        if client is None:
            qdrant_client = import_module("qdrant_client")
            client = qdrant_client.AsyncQdrantClient(url=config.url)
        self._client = client

    @property
    def status(self) -> str:
        return "qdrant-configured"

    async def ensure_collection(self) -> None:
        if await self._client.collection_exists(self.config.collection):
            return
        models = import_module("qdrant_client.models")

        await self._client.create_collection(
            collection_name=self.config.collection,
            vectors_config=models.VectorParams(size=self.config.vector_size, distance=models.Distance.COSINE),
        )

    async def upsert_problems(self, tenant_id: str, problems: list[JsonDict]) -> int:
        if not problems:
            return 0
        await self.ensure_collection()
        models = import_module("qdrant_client.models")

        points = [
            models.PointStruct(
                id=str(problem["problem_id"]),
                vector=build_problem_vector(problem, size=self.config.vector_size),
                payload={
                    "tenant_id": tenant_id,
                    "problem_id": str(problem["problem_id"]),
                    "grade_level": problem.get("grade_level"),
                    "problem_type": problem.get("problem_type"),
                    "difficulty": problem.get("difficulty"),
                    "tags": list(problem.get("tags", []) or []),
                },
            )
            for problem in problems
        ]
        await self._client.upsert(collection_name=self.config.collection, points=points)
        return len(points)

    async def close(self) -> None:
        await self._client.close()
