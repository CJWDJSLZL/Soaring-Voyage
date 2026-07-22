from __future__ import annotations

from types import SimpleNamespace

from app.rag import QdrantIndexer, QdrantIndexerConfig, build_problem_vector
from qdrant_client import models


class FakeQdrantClient:
    def __init__(self, *, exists: bool = False) -> None:
        self.exists = exists
        self.created: list[tuple[str, models.VectorParams]] = []
        self.upserts: list[tuple[str, list[models.PointStruct]]] = []
        self.queries: list[tuple[str, dict[str, object]]] = []
        self.closed = False

    async def collection_exists(self, collection_name: str) -> bool:
        return self.exists

    async def create_collection(self, collection_name: str, vectors_config: models.VectorParams) -> None:
        self.created.append((collection_name, vectors_config))
        self.exists = True

    async def upsert(self, collection_name: str, points: list[models.PointStruct]) -> None:
        self.upserts.append((collection_name, points))

    async def query_points(self, collection_name: str, **kwargs: object) -> object:
        self.queries.append((collection_name, kwargs))
        return SimpleNamespace(
            points=[
                SimpleNamespace(
                    payload={
                        "problem_text": "2 + 2 = ___",
                        "reference_answer": "4",
                        "created_by": "user-teacher",
                    }
                )
            ]
        )

    async def close(self) -> None:
        self.closed = True


def test_problem_vector_is_stable_and_sized() -> None:
    problem = {
        "problem_id": "11111111-1111-4111-8111-111111111111",
        "grade_level": 3,
        "problem_type": "arithmetic",
        "difficulty": "easy",
        "problem_text": "1 + 1 = ___",
        "reference_answer": "2",
        "tags": ["addition"],
    }

    first = build_problem_vector(problem, size=8)
    second = build_problem_vector(problem, size=8)

    assert first == second
    assert len(first) == 8
    assert all(-1 <= value <= 1 for value in first)


async def test_qdrant_indexer_creates_collection_and_upserts_problem_payload() -> None:
    fake_client = FakeQdrantClient()
    indexer = QdrantIndexer(
        QdrantIndexerConfig(url="http://qdrant:6333", collection="problems", vector_size=8),
        client=fake_client,
    )

    count = await indexer.upsert_problems(
        "tenant-demo",
        [
            {
                "problem_id": "11111111-1111-4111-8111-111111111111",
                "grade_level": 3,
                "problem_type": "arithmetic",
                "difficulty": "easy",
                "problem_text": "1 + 1 = ___",
                "reference_answer": "2",
                "tags": ["addition"],
                "created_by": "user-teacher",
            }
        ],
    )

    assert count == 1
    assert fake_client.created[0][0] == "problems"
    assert fake_client.created[0][1].size == 8
    collection, points = fake_client.upserts[0]
    assert collection == "problems"
    assert points[0].id == "11111111-1111-4111-8111-111111111111"
    assert len(points[0].vector) == 8
    assert points[0].payload["tenant_id"] == "tenant-demo"
    assert points[0].payload["problem_id"] == "11111111-1111-4111-8111-111111111111"
    assert points[0].payload["grade_level"] == 3
    assert points[0].payload["problem_type"] == "arithmetic"
    assert points[0].payload["difficulty"] == "easy"
    assert points[0].payload["tags"] == ["addition"]
    assert points[0].payload["problem_text"] == "1 + 1 = ___"
    assert points[0].payload["reference_answer"] == "2"

    await indexer.close()
    assert fake_client.closed is True


async def test_qdrant_indexer_searches_similar_problem_context() -> None:
    fake_client = FakeQdrantClient(exists=True)
    indexer = QdrantIndexer(
        QdrantIndexerConfig(url="http://qdrant:6333", collection="problems", vector_size=8),
        client=fake_client,
    )

    results = await indexer.search_similar(
        "tenant-demo",
        {
            "problem_id": "11111111-1111-4111-8111-111111111111",
            "grade_level": 3,
            "problem_type": "arithmetic",
            "difficulty": "easy",
            "problem_text": "1 + 1 = ___",
            "reference_answer": "2",
            "tags": ["addition"],
        },
    )

    assert results == [{"problem_text": "2 + 2 = ___", "reference_answer": "4"}]
    collection, kwargs = fake_client.queries[0]
    assert collection == "problems"
    assert kwargs["limit"] == 2
    assert kwargs["score_threshold"] == 0.85
